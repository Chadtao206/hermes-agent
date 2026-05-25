"""Control Center state helpers: read-only aggregation + SQLite control-plane store.

Read-only helpers gather live state from SessionDB, gateway status, process
registry, and spawn-trees.  All are best-effort (return empty/default on error).

The ControlCenterDB class provides a shared SQLite control-plane store for
cross-process live state and commands.  It is concurrency-safe (WAL mode,
BEGIN IMMEDIATE, jitter retry) and holds no authoritative in-memory state.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


# ---------------------------------------------------------------------------
# ControlCenterDB — SQLite control-plane store
# ---------------------------------------------------------------------------

_CC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS live_sessions (
    session_id    TEXT PRIMARY KEY,
    owner_kind    TEXT,
    owner_id      TEXT,
    profile       TEXT,
    source        TEXT,
    title         TEXT,
    model         TEXT,
    running       INTEGER NOT NULL DEFAULT 0,
    awaiting_input INTEGER NOT NULL DEFAULT 0,
    started_at    REAL NOT NULL DEFAULT 0,
    last_seen_at  REAL NOT NULL DEFAULT 0,
    payload_json  TEXT
);

CREATE TABLE IF NOT EXISTS pending_requests (
    request_id    TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    prompt_preview TEXT,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    payload_json  TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    target_session_id TEXT,
    action            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    payload_json      TEXT,
    created_at        REAL NOT NULL,
    claimed_at        REAL,
    completed_at      REAL,
    result_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_ls_last_seen ON live_sessions(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_pr_session   ON pending_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_pr_status    ON pending_requests(status);
CREATE INDEX IF NOT EXISTS idx_cmd_session  ON commands(target_session_id, status);
CREATE INDEX IF NOT EXISTS idx_cmd_status   ON commands(status, created_at);
"""


class ControlCenterDB:
    """SQLite control-plane store for cross-process live state and commands.

    Concurrency model:
    - WAL journal mode (falls back to DELETE on NFS/SMB via hermes_state helper)
    - BEGIN IMMEDIATE on every write to acquire the write lock eagerly
    - Application-level jitter retry on lock contention
    - No in-memory authoritative state — every read hits the DB
    """

    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020
    _WRITE_RETRY_MAX_S = 0.150

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or (_hermes_home() / "control_center.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_wal()
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ── Connection setup ──

    def _apply_wal(self) -> None:
        try:
            from hermes_state import apply_wal_with_fallback
            apply_wal_with_fallback(self._conn, db_label="control_center.db")
        except Exception:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_CC_SCHEMA_SQL)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Write helper with retry ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                return result
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        time.sleep(random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S))
                        continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    # ── Live sessions ──

    def upsert_live_session(
        self,
        session_id: str,
        *,
        owner_kind: Optional[str] = None,
        owner_id: Optional[str] = None,
        profile: Optional[str] = None,
        source: Optional[str] = None,
        title: Optional[str] = None,
        model: Optional[str] = None,
        running: bool = True,
        awaiting_input: bool = False,
        started_at: Optional[float] = None,
        last_seen_at: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        payload_json = json.dumps(payload) if payload is not None else None

        def _fn(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO live_sessions
                    (session_id, owner_kind, owner_id, profile, source, title,
                     model, running, awaiting_input, started_at, last_seen_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    owner_kind      = COALESCE(excluded.owner_kind,    live_sessions.owner_kind),
                    owner_id        = COALESCE(excluded.owner_id,      live_sessions.owner_id),
                    profile         = COALESCE(excluded.profile,       live_sessions.profile),
                    source          = COALESCE(excluded.source,        live_sessions.source),
                    title           = COALESCE(excluded.title,         live_sessions.title),
                    model           = COALESCE(excluded.model,         live_sessions.model),
                    running         = excluded.running,
                    awaiting_input  = excluded.awaiting_input,
                    started_at      = CASE WHEN excluded.started_at > 0 THEN excluded.started_at
                                          ELSE live_sessions.started_at END,
                    last_seen_at    = excluded.last_seen_at,
                    payload_json    = COALESCE(excluded.payload_json, live_sessions.payload_json)
                """,
                (
                    session_id, owner_kind, owner_id, profile, source, title,
                    model, int(running), int(awaiting_input),
                    started_at or now, last_seen_at or now, payload_json,
                ),
            )

        self._execute_write(_fn)

    def clear_live_session(self, session_id: str) -> None:
        def _fn(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM live_sessions WHERE session_id = ?", (session_id,))

        self._execute_write(_fn)

    def clear_owner_sessions(
        self,
        *,
        owner_kind: Optional[str] = None,
        owner_id: Optional[str] = None,
        stale_after_seconds: Optional[float] = None,
    ) -> int:
        """Remove live_sessions rows for a given owner, optionally only stale ones.

        At least one filter must be provided. Returns the number of rows deleted.
        Stale-owner cleanup: an owner process that crashed or restarted can call
        this to evict its own (or a specific) ownership claim from the store without
        knowing individual session_ids.
        """
        if owner_kind is None and owner_id is None and stale_after_seconds is None:
            raise ValueError(
                "At least one of owner_kind, owner_id, or stale_after_seconds is required"
            )

        now = time.time()
        deleted: int = 0

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal deleted
            conditions: list = []
            params: list = []
            if owner_kind is not None:
                conditions.append("owner_kind = ?")
                params.append(owner_kind)
            if owner_id is not None:
                conditions.append("owner_id = ?")
                params.append(owner_id)
            if stale_after_seconds is not None:
                conditions.append("last_seen_at < ?")
                params.append(now - stale_after_seconds)
            where = " AND ".join(conditions)
            cur = conn.execute(
                f"DELETE FROM live_sessions WHERE {where}",
                params,
            )
            deleted = cur.rowcount

        self._execute_write(_fn)
        return deleted

    def list_live_sessions(
        self,
        running_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM live_sessions"
        params: list = []
        if running_only:
            query += " WHERE running = 1"
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["running"] = bool(d.get("running", 0))
            d["awaiting_input"] = bool(d.get("awaiting_input", 0))
            if d.get("payload_json"):
                try:
                    d["payload"] = json.loads(d["payload_json"])
                except Exception:
                    d["payload"] = None
            else:
                d["payload"] = None
            result.append(d)
        return result

    # ── Pending requests ──

    def create_pending_request(
        self,
        request_id: str,
        session_id: str,
        kind: str,
        *,
        prompt_preview: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        payload_json = json.dumps(payload) if payload is not None else None

        def _fn(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO pending_requests
                    (request_id, session_id, kind, prompt_preview, created_at, status, payload_json)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (request_id, session_id, kind, prompt_preview, now, payload_json),
            )

        self._execute_write(_fn)

    def resolve_pending_request(self, request_id: str, status: str = "resolved") -> bool:
        resolved = False

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal resolved
            cur = conn.execute(
                "UPDATE pending_requests SET status = ? WHERE request_id = ? AND status = 'pending'",
                (status, request_id),
            )
            resolved = cur.rowcount > 0

        self._execute_write(_fn)
        return resolved

    def list_pending_requests(
        self,
        session_id: Optional[str] = None,
        status: str = "pending",
    ) -> List[Dict[str, Any]]:
        if session_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM pending_requests WHERE session_id = ? AND status = ? ORDER BY created_at",
                (session_id, status),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM pending_requests WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("payload_json"):
                try:
                    d["payload"] = json.loads(d["payload_json"])
                except Exception:
                    d["payload"] = None
            else:
                d["payload"] = None
            result.append(d)
        return result

    # ── Commands ──

    def enqueue_command(
        self,
        action: str,
        *,
        target_session_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = time.time()
        payload_json = json.dumps(payload) if payload is not None else None
        row_id: int = 0

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal row_id
            cur = conn.execute(
                """
                INSERT INTO commands (target_session_id, action, status, payload_json, created_at)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (target_session_id, action, payload_json, now),
            )
            row_id = cur.lastrowid or 0

        self._execute_write(_fn)
        return row_id

    def claim_next_command(
        self,
        *,
        target_session_id: Optional[str] = None,
        owner_kind: Optional[str] = None,
        owner_id: Optional[str] = None,
        action: Optional[str] = None,
        stale_after_seconds: float = 300.0,
    ) -> Optional[Dict[str, Any]]:
        """Claim one pending (or stale-claimed) command atomically.

        Returns the claimed row as a dict, or None if nothing is available.

        Two claim paths are supported and may be combined:
        - target_session_id: claim a command for a specific session (existing behaviour).
        - owner_kind / owner_id: claim the next command for any session currently
          owned by this owner process (JOIN with live_sessions).

        Stale claimed commands (claimed_at older than stale_after_seconds) are
        recovered and re-claimed so a crashed owner does not permanently block work.
        """
        now = time.time()
        stale_cutoff = now - stale_after_seconds
        claimed_row: Optional[Dict[str, Any]] = None
        use_owner_join = owner_kind is not None or owner_id is not None

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal claimed_row
            if use_owner_join:
                # JOIN live_sessions to filter by owner; prefix columns with "c."
                conditions = [
                    "(c.status = 'pending' OR (c.status = 'claimed' AND c.claimed_at < ?))"
                ]
                params: list = [stale_cutoff]
                if owner_kind is not None:
                    conditions.append("ls.owner_kind = ?")
                    params.append(owner_kind)
                if owner_id is not None:
                    conditions.append("ls.owner_id = ?")
                    params.append(owner_id)
                if target_session_id is not None:
                    conditions.append("c.target_session_id = ?")
                    params.append(target_session_id)
                if action is not None:
                    conditions.append("c.action = ?")
                    params.append(action)
                where = " AND ".join(conditions)
                sql = (
                    f"SELECT c.* FROM commands c"
                    f" JOIN live_sessions ls ON c.target_session_id = ls.session_id"
                    f" WHERE {where} ORDER BY c.created_at LIMIT 1"
                )
            else:
                conditions = [
                    "(status = 'pending' OR (status = 'claimed' AND claimed_at < ?))"
                ]
                params = [stale_cutoff]
                if target_session_id is not None:
                    conditions.append("target_session_id = ?")
                    params.append(target_session_id)
                if action is not None:
                    conditions.append("action = ?")
                    params.append(action)
                where = " AND ".join(conditions)
                sql = f"SELECT * FROM commands WHERE {where} ORDER BY created_at LIMIT 1"

            row = conn.execute(sql, params).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE commands SET status = 'claimed', claimed_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            claimed_row = dict(row)
            claimed_row["status"] = "claimed"
            claimed_row["claimed_at"] = now
            if claimed_row.get("payload_json"):
                try:
                    claimed_row["payload"] = json.loads(claimed_row["payload_json"])
                except Exception:
                    claimed_row["payload"] = None
            else:
                claimed_row["payload"] = None

        self._execute_write(_fn)
        return claimed_row

    def complete_command(
        self,
        command_id: int,
        *,
        status: str = "completed",
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        now = time.time()
        result_json = json.dumps(result) if result is not None else None
        updated = False

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal updated
            cur = conn.execute(
                """
                UPDATE commands
                SET status = ?, completed_at = ?, result_json = ?
                WHERE id = ? AND status = 'claimed'
                """,
                (status, now, result_json, command_id),
            )
            updated = cur.rowcount > 0

        self._execute_write(_fn)
        return updated

    def list_commands(
        self,
        *,
        target_session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM commands"
        params: list[Any] = []
        if target_session_id is not None:
            query += " WHERE target_session_id = ?"
            params.append(target_session_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("payload_json"):
                try:
                    item["payload"] = json.loads(item["payload_json"])
                except Exception:
                    item["payload"] = None
            else:
                item["payload"] = None
            if item.get("result_json"):
                try:
                    item["result"] = json.loads(item["result_json"])
                except Exception:
                    item["result"] = None
            else:
                item["result"] = None
            result.append(item)
        return result

    def get_pending_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("payload_json"):
            try:
                item["payload"] = json.loads(item["payload_json"])
            except Exception:
                item["payload"] = None
        else:
            item["payload"] = None
        return item

    def expire_unreachable_commands(self, *, live_session_ttl: float = 180.0) -> int:
        """Mark queued/claimed commands expired when their owner session vanished."""
        now = time.time()
        expired = 0
        result_json = json.dumps({"error": "target session is no longer live"})

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal expired
            cur = conn.execute(
                """
                UPDATE commands
                   SET status = 'expired',
                       completed_at = ?,
                       result_json = ?
                 WHERE status IN ('pending', 'claimed')
                   AND target_session_id IS NOT NULL
                   AND target_session_id NOT IN (
                       SELECT session_id
                         FROM live_sessions
                        WHERE last_seen_at >= ?
                   )
                """,
                (now, result_json, now - live_session_ttl),
            )
            expired = cur.rowcount

        self._execute_write(_fn)
        return expired

    def expire_stale_commands(self, *, command_ttl: float = 900.0) -> int:
        """Mark old pending/claimed commands expired so the queue cannot silently stall."""
        now = time.time()
        cutoff = now - command_ttl
        expired = 0
        result_json = json.dumps({
            "error": "command expired before a runtime completed it",
            "expired_after_seconds": command_ttl,
        })

        def _fn(conn: sqlite3.Connection) -> None:
            nonlocal expired
            cur = conn.execute(
                """
                UPDATE commands
                   SET status = 'expired',
                       completed_at = ?,
                       result_json = ?
                 WHERE (status = 'pending' AND created_at < ?)
                    OR (status = 'claimed' AND COALESCE(claimed_at, created_at) < ?)
                """,
                (now, result_json, cutoff, cutoff),
            )
            expired = cur.rowcount

        self._execute_write(_fn)
        return expired

    # ── Pruning ──

    def prune_stale_rows(
        self,
        *,
        live_session_ttl: float = 600.0,
        command_ttl: float = 86400.0,
        request_ttl: float = 86400.0,
    ) -> Dict[str, int]:
        """Remove stale rows from all tables. Returns counts of deleted rows."""
        now = time.time()
        deleted: Dict[str, int] = {"live_sessions": 0, "commands": 0, "pending_requests": 0}

        def _fn(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "DELETE FROM live_sessions WHERE last_seen_at < ?",
                (now - live_session_ttl,),
            )
            deleted["live_sessions"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM commands WHERE status IN ('completed', 'failed', 'expired') AND completed_at < ?",
                (now - command_ttl,),
            )
            deleted["commands"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM pending_requests WHERE status != 'pending' AND created_at < ?",
                (now - request_ttl,),
            )
            deleted["pending_requests"] = cur.rowcount

        self._execute_write(_fn)
        return deleted


# Module-level singleton helpers (open a fresh connection per call — keeps the
# interface simple and avoids multi-process connection sharing issues).

def _open_cc_db() -> ControlCenterDB:
    return ControlCenterDB()


def cc_upsert_live_session(session_id: str, **kwargs: Any) -> None:
    db = _open_cc_db()
    try:
        db.upsert_live_session(session_id, **kwargs)
    finally:
        db.close()


def cc_clear_live_session(session_id: str) -> None:
    db = _open_cc_db()
    try:
        db.clear_live_session(session_id)
    finally:
        db.close()


def cc_clear_owner_sessions(**kwargs: Any) -> int:
    db = _open_cc_db()
    try:
        return db.clear_owner_sessions(**kwargs)
    finally:
        db.close()


def cc_list_live_sessions(running_only: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
    db = _open_cc_db()
    try:
        return db.list_live_sessions(running_only=running_only, limit=limit)
    finally:
        db.close()


def cc_enqueue_command(action: str, **kwargs: Any) -> int:
    db = _open_cc_db()
    try:
        return db.enqueue_command(action, **kwargs)
    finally:
        db.close()


def cc_claim_next_command(**kwargs: Any) -> Optional[Dict[str, Any]]:
    db = _open_cc_db()
    try:
        return db.claim_next_command(**kwargs)
    finally:
        db.close()


def cc_complete_command(command_id: int, **kwargs: Any) -> bool:
    db = _open_cc_db()
    try:
        return db.complete_command(command_id, **kwargs)
    finally:
        db.close()


def cc_list_commands(*, target_session_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    db = _open_cc_db()
    try:
        return db.list_commands(target_session_id=target_session_id, limit=limit)
    finally:
        db.close()


def cc_expire_unreachable_commands(**kwargs: Any) -> int:
    db = _open_cc_db()
    try:
        return db.expire_unreachable_commands(**kwargs)
    finally:
        db.close()


def cc_expire_stale_commands(**kwargs: Any) -> int:
    db = _open_cc_db()
    try:
        return db.expire_stale_commands(**kwargs)
    finally:
        db.close()


def cc_create_pending_request(request_id: str, session_id: str, kind: str, **kwargs: Any) -> None:
    db = _open_cc_db()
    try:
        db.create_pending_request(request_id, session_id, kind, **kwargs)
    finally:
        db.close()


def cc_resolve_pending_request(request_id: str, status: str = "resolved") -> bool:
    db = _open_cc_db()
    try:
        return db.resolve_pending_request(request_id, status)
    finally:
        db.close()


def cc_list_pending_requests(
    session_id: Optional[str] = None,
    status: str = "pending",
) -> List[Dict[str, Any]]:
    db = _open_cc_db()
    try:
        return db.list_pending_requests(session_id=session_id, status=status)
    finally:
        db.close()


def cc_get_pending_request(request_id: str) -> Optional[Dict[str, Any]]:
    db = _open_cc_db()
    try:
        return db.get_pending_request(request_id)
    finally:
        db.close()


def cc_prune_stale_rows(**kwargs: Any) -> Dict[str, int]:
    db = _open_cc_db()
    try:
        return db.prune_stale_rows(**kwargs)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def read_sessions(limit: int = 20, running_only: bool = False) -> List[Dict[str, Any]]:
    """Return live control-plane sessions, enriched with SessionDB metadata when available."""
    rows = cc_list_live_sessions(running_only=running_only, limit=limit)
    if not rows:
        rows = []

    session_meta: Dict[str, Dict[str, Any]] = {}
    meta_rows: List[Dict[str, Any]] = []
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            meta_rows = db.list_sessions_rich(limit=max(limit * 5, 100), order_by_last_active=True)
        finally:
            db.close()
        session_meta = {str(row.get("id", "")): row for row in meta_rows if row.get("id")}
    except Exception:
        session_meta = {}
        meta_rows = []

    if not rows and meta_rows:
        cutoff = time.time() - 300
        fallback_rows: List[Dict[str, Any]] = []
        for meta in meta_rows[:limit]:
            last_seen = meta.get("last_active") or meta.get("started_at") or 0
            ended_at = meta.get("ended_at")
            is_running = ended_at is None and (not running_only or float(last_seen or 0) >= cutoff)
            if running_only and not is_running:
                continue
            fallback_rows.append(
                {
                    "session_id": meta.get("id", ""),
                    "title": meta.get("title") or "",
                    "source": meta.get("source"),
                    "model": meta.get("model"),
                    "profile": meta.get("profile"),
                    "owner_kind": "sessiondb",
                    "running": is_running,
                    "awaiting_input": False,
                    "started_at": meta.get("started_at") or 0,
                    "last_seen_at": last_seen,
                    "payload": {"last_preview": meta.get("preview") or ""},
                }
            )
        rows = fallback_rows

    result = []
    for item in rows:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        pending_kinds = payload.get("pending_request_kinds") or []
        meta = session_meta.get(item.get("session_id", ""), {})
        started_at = item.get("started_at") or meta.get("started_at") or 0
        last_seen_at = item.get("last_seen_at") or meta.get("last_active") or 0
        try:
            duration_seconds = max(0.0, float(last_seen_at or 0) - float(started_at or 0))
        except Exception:
            duration_seconds = 0.0
        last_preview = payload.get("last_preview") if isinstance(payload, dict) else None
        preview_lower = str(last_preview or "").lower()
        external_wait_hint = any(
            token in preview_lower
            for token in (
                "refreshing checks status",
                "in_progress",
                "pending",
                "waiting on ci",
                "waiting for checks",
                "deploy in progress",
            )
        )
        result.append(
            {
                "session_id": item.get("session_id", ""),
                "title": item.get("title") or meta.get("title") or "",
                "source": item.get("source") or meta.get("source"),
                "model": item.get("model") or meta.get("model"),
                "profile": item.get("profile") or meta.get("profile"),
                "owner_kind": item.get("owner_kind") or "unknown",
                "running": bool(item.get("running", False)),
                "awaiting_input": bool(item.get("awaiting_input", False)),
                "pending_request_kinds": list(pending_kinds) if isinstance(pending_kinds, list) else [],
                "started_at": started_at,
                "last_seen_at": last_seen_at,
                "last_preview": last_preview,
                "activity": {
                    "api_call_count": int(meta.get("api_call_count") or 0),
                    "tool_call_count": int(meta.get("tool_call_count") or 0),
                    "input_tokens": int(meta.get("input_tokens") or 0),
                    "output_tokens": int(meta.get("output_tokens") or 0),
                    "reasoning_tokens": int(meta.get("reasoning_tokens") or 0),
                    "duration_seconds": duration_seconds,
                    "external_wait_hint": external_wait_hint,
                },
            }
        )
    return result


def read_pending_requests() -> List[Dict[str, Any]]:
    requests = cc_list_pending_requests()
    session_titles = {
        session.get("session_id", ""): session.get("title") or ""
        for session in read_sessions(limit=200, running_only=False)
    }
    result = []
    for item in requests:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        choices = payload.get("choices") if isinstance(payload, dict) else None
        result.append(
            {
                "request_id": item.get("request_id", ""),
                "session_id": item.get("session_id", ""),
                "kind": item.get("kind") or "clarify",
                "prompt_preview": item.get("prompt_preview") or "",
                "created_at": item.get("created_at") or 0,
                "session_title": session_titles.get(item.get("session_id", "")) or None,
                "choices": choices if isinstance(choices, list) else None,
            }
        )
    return result


def read_commands(limit: int = 50) -> List[Dict[str, Any]]:
    cc_expire_unreachable_commands()
    cc_expire_stale_commands()
    commands = cc_list_commands(limit=limit)
    result = []
    for item in commands:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        result.append(
            {
                "id": item.get("id"),
                "target_session_id": item.get("target_session_id"),
                "action": item.get("action"),
                "status": item.get("status"),
                "created_at": item.get("created_at") or 0,
                "claimed_at": item.get("claimed_at"),
                "completed_at": item.get("completed_at"),
                "payload": payload,
                "result": item.get("result") if isinstance(item.get("result"), dict) else None,
            }
        )
    return result


def count_active_sessions() -> int:
    """Count top-level sessions active within the last 5 minutes, without a list-size cap.

    Applies the same child-session exclusion as list_sessions_rich(include_children=False)
    so the headline count matches the Live Sessions pane even during delegated/subagent
    child-session activity.
    """
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            cutoff = time.time() - 300
            # Mirror list_sessions_rich include_children=False: exclude child sessions
            # unless their parent ended with end_reason='branched' before them.
            row = db._conn.execute(
                "SELECT COUNT(*) FROM sessions"
                " WHERE ended_at IS NULL"
                " AND ("
                "   parent_session_id IS NULL"
                "   OR EXISTS ("
                "     SELECT 1 FROM sessions p"
                "     WHERE p.id = sessions.parent_session_id"
                "     AND p.end_reason = 'branched'"
                "     AND sessions.started_at >= p.ended_at"
                "   )"
                " )"
                " AND COALESCE("
                "   (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = sessions.id),"
                "   sessions.started_at,"
                "   0"
                " ) >= ?",
                (cutoff,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            db.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

def read_gateway_status() -> Dict[str, Any]:
    """Return {running, state} from gateway runtime PID + state file."""
    try:
        from gateway.status import get_running_pid, read_runtime_status
        pid = get_running_pid(cleanup_stale=False)
        running = pid is not None
        runtime = read_runtime_status() or {}
        state: Optional[str] = runtime.get("gateway_state")
        if not running and state not in {"stopped", "startup_failed"}:
            state = "stopped" if state is not None else None
        return {"running": running, "state": state}
    except Exception:
        return {"running": False, "state": None}


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def _read_checkpoint() -> List[Dict[str, Any]]:
    """Read processes.json crash-recovery checkpoint."""
    path = _hermes_home() / "processes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _parse_process_started_at(value: Any) -> tuple[float, Optional[int]]:
    """Return (epoch_seconds, uptime_seconds_or_none) from registry/checkpoint values."""
    if isinstance(value, (int, float)):
        started = float(value)
        return started, max(0, int(time.time() - started)) if started > 0 else None
    if isinstance(value, str) and value:
        try:
            import datetime as _dt

            started = _dt.datetime.fromisoformat(value).timestamp()
            return started, max(0, int(time.time() - started)) if started > 0 else None
        except (ValueError, AttributeError):
            return 0.0, None
    return 0.0, None


def read_processes() -> List[Dict[str, Any]]:
    """Return running/recent managed background processes.

    The primary source is the in-memory process registry for this runtime.  When
    the dashboard is in a different process, fall back to the crash-recovery
    checkpoint written by the registry.  Keep this limited to Hermes-managed
    background process sessions; arbitrary OS processes are exposed separately by
    read_system_processes().
    """
    sessions: List[Dict[str, Any]] = []
    from_registry = False
    try:
        from tools.process_registry import process_registry
        sessions = process_registry.list_sessions()
        if not sessions:
            try:
                process_registry.recover_from_checkpoint()
                sessions = process_registry.list_sessions()
            except Exception:
                pass
        from_registry = bool(sessions)
    except Exception:
        pass

    if not sessions:
        sessions = _read_checkpoint()
        from_registry = False

    result = []
    for s in sessions:
        started_at, derived_uptime = _parse_process_started_at(s.get("started_at"))
        status = str(s.get("status") or "").strip().lower()
        if not status:
            status = "exited" if s.get("exited") else "running"
        exited = status not in ("running", "starting")
        uptime = s.get("uptime_seconds")
        if not isinstance(uptime, int):
            uptime = derived_uptime

        command = str(s.get("command") or "")
        result.append(
            {
                "session_id": s.get("session_id", ""),
                "pid": s.get("pid"),
                "command": command[:240],
                "cwd": s.get("cwd") or None,
                "started_at": started_at,
                "uptime_seconds": uptime,
                "status": status,
                "exited": exited,
                "exit_code": s.get("exit_code"),
                "notify_on_complete": bool(s.get("notify_on_complete", False)),
                "session_key": s.get("session_key") or None,
                "detached": bool(s.get("detached", False)),
                "output_preview": str(s.get("output_preview") or "")[-500:] or None,
                "controllable": from_registry,
            }
        )
    return result


_HERMES_PROCESS_KEYWORDS = (
    "hermes",
    ".hermes/hermes-agent",
    "run_agent.py",
    "hermes_cli",
    "gateway/run.py",
    "tui_gateway",
    "control_center_store.py",
    "claude_print.py",
)


def _classify_system_process(command: str) -> str:
    lower = command.lower()
    if "dashboard" in lower or "web_server" in lower or "uvicorn" in lower:
        return "dashboard"
    if "gateway" in lower:
        return "gateway"
    if "cron" in lower:
        return "cron"
    if "tui_gateway" in lower or " --tui" in lower:
        return "tui"
    if "claude_print.py" in lower or " claude " in lower:
        return "worker"
    if "/lsp/" in lower or "language-server" in lower or "langserver" in lower:
        return "lsp"
    if "mcp" in lower:
        return "mcp"
    return "hermes"


def read_system_processes(limit: int = 100) -> List[Dict[str, Any]]:
    """Return read-only OS process rows that look Hermes-related.

    This is deliberately visibility-only.  Control actions for arbitrary OS PIDs
    are not exposed here; the dashboard may only kill managed background process
    sessions through process_registry.
    """
    if sys.platform.startswith("win"):
        return []

    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except Exception:
        return []

    managed_pids = {
        int(p.get("pid"))
        for p in read_processes()
        if isinstance(p.get("pid"), int) or str(p.get("pid") or "").isdigit()
    }
    current_pid = None
    try:
        import os

        current_pid = os.getpid()
    except Exception:
        current_pid = None

    rows: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for line in raw.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_raw, ppid_raw, elapsed, command = parts
        try:
            pid = int(pid_raw)
            ppid = int(ppid_raw)
        except ValueError:
            continue
        if pid in seen:
            continue
        lower = command.lower()
        looks_hermes = (
            pid in managed_pids
            or pid == current_pid
            or any(keyword in lower for keyword in _HERMES_PROCESS_KEYWORDS)
        )
        if not looks_hermes:
            continue
        # Avoid listing the transient ps command itself if a parent shell happens
        # to include Hermes paths in its argv.
        if command.startswith("ps -axo"):
            continue
        seen.add(pid)
        rows.append(
            {
                "pid": pid,
                "ppid": ppid,
                "elapsed": elapsed,
                "kind": _classify_system_process(command),
                "command": command[:500],
                "command_preview": command[:180],
                "managed": pid in managed_pids,
            }
        )
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# Runtime health / safe runtime actions
# ---------------------------------------------------------------------------

def _extract_dashboard_port(command: str) -> Optional[int]:
    parts = command.split()
    for idx, part in enumerate(parts):
        if part == "--port" and idx + 1 < len(parts):
            try:
                return int(parts[idx + 1])
            except ValueError:
                return None
        if part.startswith("--port="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _runtime_action(action: str, label: str, available: bool, reason: Optional[str] = None, destructive: bool = False) -> Dict[str, Any]:
    return {
        "id": action,
        "label": label,
        "available": bool(available),
        "reason": reason,
        "destructive": bool(destructive),
    }


def _runtime_card(
    runtime_id: str,
    name: str,
    status: str,
    *,
    running: bool = False,
    state: Optional[str] = None,
    primary_pid: Optional[int] = None,
    pids: Optional[List[int]] = None,
    source: str = "best-effort",
    details: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[str]] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "id": runtime_id,
        "name": name,
        "status": status,
        "running": bool(running),
        "state": state,
        "primary_pid": primary_pid,
        "pids": pids or ([] if primary_pid is None else [primary_pid]),
        "source": source,
        "details": details or {},
        "warnings": warnings or [],
        "actions": actions or [],
    }


def read_runtime_health() -> Dict[str, Any]:
    """Return operator-facing runtime cards for dashboard, gateway, and cron.

    This is intentionally best-effort and separates read-only OS visibility from
    control actions.  Arbitrary PID control is never exposed here.
    """
    last_checked = time.time()
    system_rows = read_system_processes(limit=200)

    dashboard_rows = [row for row in system_rows if row.get("kind") == "dashboard"]
    dashboard_pids = sorted({int(row["pid"]) for row in dashboard_rows if isinstance(row.get("pid"), int)})
    current_pid = None
    try:
        import os
        current_pid = os.getpid()
    except Exception:
        current_pid = None
    dashboard_ports = sorted({
        port
        for row in dashboard_rows
        for port in [_extract_dashboard_port(str(row.get("command") or ""))]
        if port is not None
    })
    dashboard_warnings: List[str] = []
    if len(dashboard_pids) > 1:
        dashboard_warnings.append(f"{len(dashboard_pids)} dashboard-related OS processes are visible; verify there are no stale dashboard servers.")
    dashboard_url = f"http://127.0.0.1:{dashboard_ports[0]}" if dashboard_ports else None
    dashboard = _runtime_card(
        "dashboard",
        "Dashboard",
        "running",
        running=True,
        state="serving",
        primary_pid=current_pid,
        pids=dashboard_pids or ([current_pid] if current_pid else []),
        source="current web server + process scan",
        details={
            "responsive": True,
            "url": dashboard_url,
            "process_count": len(dashboard_pids),
            "ports": dashboard_ports,
        },
        warnings=dashboard_warnings,
        actions=[
            _runtime_action("refresh", "Refresh", True),
            _runtime_action("stop", "Stop", False, "Stopping this runtime would terminate the dashboard UI.", True),
            _runtime_action("restart", "Restart", False, "Restarting this runtime from inside its own UI is intentionally disabled.", True),
        ],
    )

    gateway_pid: Optional[int] = None
    gateway_runtime: Dict[str, Any] = {}
    try:
        from gateway.status import get_running_pid, read_runtime_status
        gateway_pid = get_running_pid(cleanup_stale=False)
        gateway_runtime = read_runtime_status() or {}
    except Exception:
        gateway_pid = None
        gateway_runtime = {}
    gateway_running = gateway_pid is not None
    gateway_state = gateway_runtime.get("gateway_state")
    gateway_warnings: List[str] = []
    if gateway_runtime.get("exit_reason"):
        gateway_warnings.append(f"Last exit reason: {gateway_runtime.get('exit_reason')}")
    if not gateway_running and gateway_state not in {None, "stopped", "startup_failed"}:
        gateway_warnings.append("Gateway status file exists but no live PID was found.")
    gateway = _runtime_card(
        "gateway",
        "Gateway",
        "running" if gateway_running else "stopped",
        running=gateway_running,
        state=gateway_state,
        primary_pid=gateway_pid,
        pids=[gateway_pid] if gateway_pid else [],
        source="gateway.pid + gateway_state.json",
        details={
            "active_agents": gateway_runtime.get("active_agents"),
            "updated_at": gateway_runtime.get("updated_at"),
            "platforms": gateway_runtime.get("platforms") or {},
        },
        warnings=gateway_warnings,
        actions=[
            _runtime_action("refresh", "Refresh", True),
            _runtime_action("start", "Start", not gateway_running, "Gateway is already running." if gateway_running else None),
            _runtime_action("stop", "Stop", gateway_running, "Gateway is not running." if not gateway_running else None, True),
            _runtime_action("restart", "Restart", True, destructive=True),
        ],
    )

    cron_rows = [row for row in system_rows if row.get("kind") == "cron"]
    cron_pids = sorted({int(row["pid"]) for row in cron_rows if isinstance(row.get("pid"), int)})
    active_jobs = 0
    next_run_at = None
    health_summary: Dict[str, int] = {"scheduler": 0, "manual_only": 0, "missing": 0, "stale": 0}
    cron_warnings: List[str] = []
    try:
        from cron.jobs import get_jobs_health, list_jobs
        jobs = list_jobs(include_disabled=False)
        active_jobs = len(jobs)
        next_runs = [j.get("next_run_at") for j in jobs if j.get("next_run_at")]
        next_run_at = min(next_runs) if next_runs else None
        health_rows = get_jobs_health(include_disabled=False)
        for row in health_rows:
            proof = row.get("proof")
            if proof == "scheduler":
                health_summary["scheduler"] += 1
            elif proof == "manual-only":
                health_summary["manual_only"] += 1
            elif proof == "missing":
                health_summary["missing"] += 1
            if "stale" in (row.get("flags") or []):
                health_summary["stale"] += 1
        if active_jobs and not gateway_running:
            cron_warnings.append("Active cron jobs exist but the gateway is not running, so scheduled jobs will not fire automatically.")
        if health_summary["missing"]:
            cron_warnings.append(f"{health_summary['missing']} active cron job(s) have no recent scheduler evidence.")
        if health_summary["stale"]:
            cron_warnings.append(f"{health_summary['stale']} cron job(s) have stale scheduler evidence.")
    except Exception as exc:
        cron_warnings.append(f"Cron health unavailable: {exc}")
    cron = _runtime_card(
        "cron",
        "Cron Scheduler",
        "active" if active_jobs else "idle",
        running=bool(gateway_running),
        state="gateway-driven" if gateway_running else "waiting_for_gateway",
        primary_pid=cron_pids[0] if cron_pids else None,
        pids=cron_pids,
        source="cron jobs + gateway scheduler health",
        details={
            "active_jobs": active_jobs,
            "next_run_at": next_run_at,
            "health": health_summary,
        },
        warnings=cron_warnings,
        actions=[
            _runtime_action("refresh", "Refresh", True),
            _runtime_action("tick", "Run scheduler tick", False, "Manual cron ticks remain CLI-only for now.", True),
        ],
    )

    runtimes = [dashboard, gateway, cron]
    alerts = [
        {"level": "warning", "runtime": card["id"], "message": warning}
        for card in runtimes
        for warning in card.get("warnings", [])
    ]
    return {"last_checked": last_checked, "runtimes": runtimes, "alerts": alerts}


def execute_runtime_action(runtime_id: str, action: str) -> Dict[str, Any]:
    """Execute a narrowly allowlisted runtime action.

    Only established Hermes CLI gateway controls are exposed. Dashboard self-stop
    and arbitrary PID actions are intentionally unavailable.
    """
    runtime_id = str(runtime_id or "").strip().lower()
    action = str(action or "").strip().lower()
    if action == "refresh":
        card = next((r for r in read_runtime_health().get("runtimes", []) if r.get("id") == runtime_id), None)
        return {"status": "ok", "runtime_id": runtime_id, "action": action, "runtime": card}
    if runtime_id == "dashboard":
        return {"status": "unavailable", "runtime_id": runtime_id, "action": action, "error": "Dashboard self-stop/restart is intentionally disabled because it would terminate this UI."}
    if runtime_id == "cron":
        return {"status": "unavailable", "runtime_id": runtime_id, "action": action, "error": "Cron runtime actions are read-only in this phase; use the Cron page or CLI for job controls."}
    if runtime_id != "gateway":
        return {"status": "not_found", "runtime_id": runtime_id, "action": action, "error": "runtime not found"}
    if action not in {"start", "stop", "restart", "status"}:
        return {"status": "unavailable", "runtime_id": runtime_id, "action": action, "error": "unsupported gateway action"}
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "gateway", action],
            cwd=str(Path(__file__).resolve().parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
        )
        return {
            "status": "ok" if completed.returncode == 0 else "failed",
            "runtime_id": runtime_id,
            "action": action,
            "exit_code": completed.returncode,
            "output": (completed.stdout or "")[-4000:],
        }
    except Exception as exc:
        return {"status": "error", "runtime_id": runtime_id, "action": action, "error": str(exc)}


def count_running_processes() -> int:
    """Count processes that have not yet exited."""
    try:
        return sum(1 for p in read_processes() if not p.get("exited"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Delegation / spawn-trees
# ---------------------------------------------------------------------------

def _read_index_file(index_path: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        with index_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def read_spawn_tree_metadata(limit: int = 50) -> List[Dict[str, Any]]:
    """Scan spawn-trees directory for recent subagent delegation entries."""
    root = _hermes_home() / "spawn-trees"
    if not root.exists():
        return []

    try:
        session_dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []

    entries: List[Dict[str, Any]] = []
    for sd in session_dirs:
        index_path = sd / "_index.jsonl"
        if index_path.exists():
            entries.extend(_read_index_file(index_path))
        else:
            try:
                for f in sorted(sd.glob("*.json"), reverse=True)[:5]:
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        entries.append(
                            {
                                "session_id": data.get("session_id", sd.name),
                                "started_at": data.get("started_at"),
                                "finished_at": data.get("finished_at"),
                                "label": data.get("label", ""),
                                "count": len(data.get("subagents", [])),
                            }
                        )
                    except Exception:
                        continue
            except OSError:
                pass

    entries.sort(key=lambda e: e.get("finished_at") or 0, reverse=True)
    return entries[:limit]


def read_delegation_subagents(limit: int = 20) -> List[Dict[str, Any]]:
    """Return delegation summary records derived from spawn-tree snapshots."""
    entries = read_spawn_tree_metadata(limit=limit)
    result = []
    for e in entries:
        session_id = e.get("session_id", "unknown")
        finished_at = e.get("finished_at")
        started_at = e.get("started_at")
        subagent_id = f"{session_id}@{finished_at or 0}"
        result.append(
            {
                "session_id": session_id,
                "subagent_id": subagent_id,
                "status": "completed",
                "profile": e.get("profile") or None,
                "started_at": started_at,
                "finished_at": finished_at,
                "parent_subagent_id": None,
            }
        )
    return result



# ---------------------------------------------------------------------------
# Control Center operator capabilities
# ---------------------------------------------------------------------------

def read_action_capabilities() -> Dict[str, Any]:
    """Return operator-action mode and safe/destructive action groups.

    Phase 2C exposes pending-request responses as non-destructive operator
    actions once the base action gate is enabled. Phase 2D keeps interrupt,
    kill, and runtime lifecycle controls behind a second explicit opt-in so a
    dashboard can safely render them only when both backend gates agree.
    """
    raw = os.environ.get("HERMES_CONTROL_CENTER_ACTIONS", "")
    enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
    destructive_raw = os.environ.get("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", "")
    destructive_enabled = enabled and destructive_raw.strip().lower() in {"1", "true", "yes", "on"}
    safe_session_actions = ["steer", "submit"]
    safe_process_actions = ["poll", "log", "wait"]
    pending_request_actions = ["respond_pending"]
    destructive_actions = ["interrupt", "kill", "runtime_start", "runtime_stop", "runtime_restart"]
    safe_actions = safe_session_actions + safe_process_actions + pending_request_actions
    return {
        "actions_enabled": enabled,
        "mode": "operator_actions_enabled" if enabled else "read_only",
        "label": "Operator actions enabled" if enabled else "Read-only mode",
        "reason": None if enabled else "Set HERMES_CONTROL_CENTER_ACTIONS=1 to enable safe operator controls.",
        "env_var": "HERMES_CONTROL_CENTER_ACTIONS",
        "safe_session_actions": safe_session_actions,
        "safe_process_actions": safe_process_actions,
        "pending_request_actions": pending_request_actions,
        "safe_actions": safe_actions,
        "destructive_actions": destructive_actions,
        "deferred_actions": [],
        "destructive_controls_enabled": destructive_enabled,
        "destructive_controls_env_var": "HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS",
        "destructive_controls_reason": None if destructive_enabled else "Set HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS=1 in addition to HERMES_CONTROL_CENTER_ACTIONS=1 to enable interrupt, kill, and runtime lifecycle controls.",
    }


# ---------------------------------------------------------------------------
# Phase 1 read-only status domains
# ---------------------------------------------------------------------------

def read_kanban_status() -> Dict[str, Any]:
    """Return a best-effort summary of the local kanban board."""
    db_path = _hermes_home() / "kanban.db"
    status: Dict[str, Any] = {
        "status": "missing",
        "available": False,
        "db_path": str(db_path),
        "total_tasks": 0,
        "by_status": {},
        "by_assignee": {},
        "open_tasks": 0,
        "blocked_tasks": 0,
        "running_tasks": 0,
        "updated_at": None,
        "error": None,
    }
    if not db_path.exists():
        return status
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT status, assignee, COUNT(*) AS count FROM tasks GROUP BY status, assignee").fetchall()
        by_status: Dict[str, int] = {}
        by_assignee: Dict[str, int] = {}
        total = 0
        for row in rows:
            count = int(row["count"] or 0)
            task_status = str(row["status"] or "unknown")
            assignee = str(row["assignee"] or "unassigned")
            by_status[task_status] = by_status.get(task_status, 0) + count
            by_assignee[assignee] = by_assignee.get(assignee, 0) + count
            total += count
        open_count = sum(count for name, count in by_status.items() if name not in {"done", "completed", "cancelled", "archived"})
        status.update({
            "status": "ok",
            "available": True,
            "total_tasks": total,
            "by_status": by_status,
            "by_assignee": by_assignee,
            "open_tasks": open_count,
            "blocked_tasks": by_status.get("blocked", 0),
            "running_tasks": by_status.get("in_progress", 0) + by_status.get("running", 0),
            "updated_at": db_path.stat().st_mtime,
        })
    except Exception as exc:
        status.update({"status": "unavailable", "error": str(exc)})
    return status


def _count_table(conn: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return None


def read_memory_status() -> Dict[str, Any]:
    """Return best-effort local Holographic memory status for the active profile."""
    db_path = _hermes_home() / "memory_store.db"
    status: Dict[str, Any] = {
        "status": "missing",
        "available": False,
        "provider": None,
        "db_path": str(db_path),
        "facts": None,
        "entities": None,
        "banks": None,
        "updated_at": None,
        "error": None,
    }
    try:
        from hermes_cli.config import cfg_get
        status["provider"] = cfg_get("memory.provider", None)
    except Exception:
        status["provider"] = None
    if not db_path.exists():
        return status
    try:
        with sqlite3.connect(str(db_path)) as conn:
            facts = _count_table(conn, "facts")
            entities = _count_table(conn, "entities")
            banks = _count_table(conn, "memory_banks")
        status.update({
            "status": "ok",
            "available": True,
            "facts": facts,
            "entities": entities,
            "banks": banks,
            "updated_at": db_path.stat().st_mtime,
        })
    except Exception as exc:
        status.update({"status": "unavailable", "error": str(exc)})
    return status


def _git_status_summary(repo_path: Path) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "path": str(repo_path),
        "status": "missing",
        "is_repo": False,
        "branch": None,
        "dirty": False,
        "ahead": None,
        "behind": None,
        "changed_files": 0,
        "last_commit": None,
        "error": None,
    }
    if not repo_path.exists():
        return summary
    if not (repo_path / ".git").exists():
        summary["status"] = "not_repo"
        return summary
    try:
        branch = subprocess.run(["git", "-C", str(repo_path), "branch", "--show-current"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        status = subprocess.run(["git", "-C", str(repo_path), "status", "--porcelain=v1", "--branch"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        commit = subprocess.run(["git", "-C", str(repo_path), "log", "--oneline", "-1"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        lines = [line for line in (status.stdout or "").splitlines() if line]
        branch_line = lines[0] if lines and lines[0].startswith("##") else ""
        changed = [line for line in lines if not line.startswith("##")]
        ahead = behind = None
        if "ahead " in branch_line:
            try:
                ahead = int(branch_line.split("ahead ", 1)[1].split(",", 1)[0].split("]", 1)[0])
            except Exception:
                ahead = None
        if "behind " in branch_line:
            try:
                behind = int(branch_line.split("behind ", 1)[1].split(",", 1)[0].split("]", 1)[0])
            except Exception:
                behind = None
        summary.update({
            "status": "ok",
            "is_repo": True,
            "branch": (branch.stdout or "").strip() or None,
            "dirty": bool(changed),
            "ahead": ahead,
            "behind": behind,
            "changed_files": len(changed),
            "last_commit": (commit.stdout or "").strip() or None,
        })
    except Exception as exc:
        summary.update({"status": "unavailable", "is_repo": True, "error": str(exc)})
    return summary


def read_repo_status() -> Dict[str, Any]:
    """Return best-effort git summaries for Hermes source and control-plane repo."""
    hermes_source = Path(__file__).resolve().parent
    control_plane = Path.home() / "code" / "hermes-control-plane"
    return {
        "status": "ok",
        "hermes_source": _git_status_summary(hermes_source),
        "control_plane": _git_status_summary(control_plane),
    }

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def read_profiles() -> List[Dict[str, Any]]:
    """Return a lightweight profile list from gateway agents and live sessions.

    Gateway runtime status is optional: TUI/CLI/dashboard sessions can still be
    visible in the shared control-center store when the gateway is down.
    """
    active_agents = None
    try:
        from gateway.status import get_running_pid, read_runtime_status
        if get_running_pid(cleanup_stale=False) is not None:
            runtime = read_runtime_status() or {}
            active_agents = runtime.get("active_agents")
    except Exception:
        active_agents = None

    sessions: List[Dict[str, Any]] = []
    try:
        sessions = read_sessions(limit=200, running_only=True)
    except Exception:
        sessions = []

    by_profile: Dict[str, List[Dict[str, Any]]] = {}
    for session in sessions:
        name = str(session.get("profile") or "default")
        by_profile.setdefault(name, []).append(session)

    result_by_name: Dict[str, Dict[str, Any]] = {}
    if isinstance(active_agents, dict):
        for name, info in active_agents.items():
            profile_name = str(name)
            session_rows = by_profile.get(profile_name, [])
            result_by_name[profile_name] = {
                "name": profile_name,
                "is_online": True,
                "last_seen": max((s.get("last_seen_at") for s in session_rows), default=None),
                "active_sessions": max(1, len(session_rows)),
                "model": (info.get("model") if isinstance(info, dict) else None) or (session_rows[0].get("model") if session_rows else None),
            }

    for name, session_rows in by_profile.items():
        if name in result_by_name:
            result_by_name[name]["active_sessions"] = len(session_rows)
            result_by_name[name]["last_seen"] = max((s.get("last_seen_at") for s in session_rows), default=None)
            if not result_by_name[name].get("model") and session_rows:
                result_by_name[name]["model"] = session_rows[0].get("model")
            continue
        result_by_name[name] = {
            "name": name,
            "is_online": True,
            "last_seen": max((s.get("last_seen_at") for s in session_rows), default=None),
            "active_sessions": len(session_rows),
            "model": session_rows[0].get("model") if session_rows else None,
        }

    return sorted(result_by_name.values(), key=lambda row: str(row.get("name") or ""))


def count_profiles_online() -> int:
    try:
        return len(read_profiles())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Proposals (read-only dashboard queue)
# ---------------------------------------------------------------------------

def _proposal_packets_dir() -> Path:
    return _hermes_home() / "telemetry" / "proposals"


def _load_json_object(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _proposal_sort_key(row: Dict[str, Any], source_path: Path) -> tuple[str, float]:
    updated_at = row.get("updated_at") or row.get("created_at") or ""
    try:
        mtime = source_path.stat().st_mtime
    except Exception:
        mtime = 0.0
    return str(updated_at), float(mtime)


def _normalize_proposal_row(row: Dict[str, Any], source_path: Path) -> Dict[str, Any]:
    proposal_id = str(row.get("proposal_id") or source_path.stem.replace(".row", ""))
    confidence_basis = row.get("confidence_basis")
    if not isinstance(confidence_basis, dict):
        confidence_basis = {}

    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        evidence = []

    base_path = source_path
    if base_path.name.endswith(".row.json"):
        base_path = base_path.with_name(base_path.name[: -len(".row.json")])
    elif base_path.name.endswith(".json"):
        base_path = base_path.with_suffix("")
    markdown_path = base_path.with_suffix(".md")

    sources = [str(source_path)]
    if markdown_path.exists():
        sources.append(str(markdown_path))

    return {
        "proposal_id": proposal_id,
        "title": row.get("title") or proposal_id,
        "status": row.get("status") or "proposed",
        "decision_requested": row.get("decision_requested") or "approve",
        "owner": row.get("owner_profile") or row.get("owner") or None,
        "tl_dr": row.get("tl_dr") or row.get("problem_statement") or "",
        "confidence": {
            "score": row.get("confidence_score"),
            "band": row.get("confidence_label"),
            "basis": confidence_basis,
        },
        "risk": {
            "level": row.get("risk_level") or "unknown",
            "notes": row.get("risk_notes") or "",
        },
        "rollback": row.get("rollback_plan") or row.get("rollback") or "",
        "verification": row.get("verification_plan") or row.get("verification") or "",
        "evidence": evidence,
        "approve_deny_discuss": row.get("approve_deny_discuss") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "provenance": {
            "source_paths": sources,
            "source_file": str(source_path),
        },
    }


def read_proposals(limit: int = 200, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return read-only self-improvement proposals for dashboard queue rendering.

    Source of truth is telemetry proposal artifacts under
    ``$HERMES_HOME/telemetry/proposals``. Prefer ``*.row.json`` files (contain
    status/risk/confidence fields used by the queue). If none exist, fall back
    to ``*.json`` packet files.
    """

    root = _proposal_packets_dir()
    if not root.exists():
        return []

    rows: List[tuple[Dict[str, Any], Path]] = []

    candidates = sorted(root.glob("*.row.json"))
    if not candidates:
        candidates = [p for p in sorted(root.glob("*.json")) if not p.name.endswith(".row.json")]

    for candidate in candidates:
        data = _load_json_object(candidate)
        if not data:
            continue
        normalized = _normalize_proposal_row(data, candidate)
        if status and str(normalized.get("status") or "").lower() != status.lower():
            continue
        rows.append((normalized, candidate))

    rows.sort(key=lambda pair: _proposal_sort_key(pair[0], pair[1]), reverse=True)
    return [row for row, _ in rows[: max(1, min(limit, 500))]]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _build_alerts(gateway: Dict[str, Any]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    if gateway.get("state") == "startup_failed":
        alerts.append({"level": "error", "message": "Gateway startup failed."})
    try:
        for alert in read_runtime_health().get("alerts", []):
            alerts.append({
                "level": alert.get("level", "warning"),
                "message": alert.get("message", "Runtime warning"),
                "runtime": alert.get("runtime"),
            })
    except Exception:
        pass
    return alerts


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def read_overview() -> Dict[str, Any]:
    """Aggregate gateway status, counts, and alerts from all sources."""
    gateway = read_gateway_status()

    active_sessions = 0
    try:
        active_sessions = len(read_sessions(limit=500, running_only=True))
    except Exception:
        pass

    pending_requests = 0
    try:
        pending_requests = len(read_pending_requests())
    except Exception:
        pass

    running_processes = 0
    try:
        running_processes = count_running_processes()
    except Exception:
        pass

    profiles_online = 0
    try:
        profiles_online = count_profiles_online()
    except Exception:
        pass

    alerts: List[Dict[str, Any]] = []
    try:
        alerts = _build_alerts(gateway)
    except Exception:
        pass

    kanban = read_kanban_status()
    memory = read_memory_status()
    repos = read_repo_status()

    return {
        "gateway": gateway,
        "counts": {
            "active_sessions": active_sessions,
            "pending_requests": pending_requests,
            "running_processes": running_processes,
            "profiles_online": profiles_online,
        },
        "kanban": kanban,
        "memory": memory,
        "repos": repos,
        "control_center": read_action_capabilities(),
        "alerts": alerts,
    }
