"""Sidecar SQLite storage for kanban notifier heartbeat telemetry.

Runtime heartbeat telemetry is intentionally decoupled from the main kanban
board DB so notifier liveness writes/repairs can never DDL/DML board tables.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

# Keep in sync with legacy kanban_db exports.
NOTIFIER_HEARTBEAT_ACTIVE_WINDOW_SECONDS = 30
NOTIFIER_HEARTBEAT_RETENTION_SECONDS = 14 * 24 * 3600
NOTIFIER_HEARTBEAT_LIST_LIMIT = 50

_DEFAULT_BOARD_SLUG = "default"
_WARNING_RATE_LIMIT_SECONDS = 300
_last_warning_at: dict[str, float] = {}


def _rate_limited_warning(key: str, message: str, *args: Any) -> None:
    """Emit at most one warning per key per rate-limit window."""
    now = time.monotonic()
    last = _last_warning_at.get(key, 0.0)
    if now - last >= _WARNING_RATE_LIMIT_SECONDS:
        _last_warning_at[key] = now
        log.warning(message, *args)
    else:
        log.debug(message, *args)


def notifier_heartbeat_sidecar_path() -> Path:
    """Return the profile-aware sidecar DB path for notifier heartbeats."""
    return get_hermes_home() / "kanban_notifier_heartbeats.db"


def _canonical_db_path(db_path: str) -> str:
    """Normalize DB path identity to the resolved absolute path when possible."""
    raw = str(db_path or "")
    if not raw:
        return raw
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return raw


@contextlib.contextmanager
def _write_txn(conn: sqlite3.Connection):
    """Small IMMEDIATE transaction helper local to the sidecar DB."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kanban_notifier_heartbeats (
            notifier_id      TEXT NOT NULL,
            board_slug       TEXT NOT NULL,
            db_path          TEXT NOT NULL,
            notifier_profile TEXT,
            host             TEXT NOT NULL,
            pid              INTEGER NOT NULL,
            started_at       INTEGER NOT NULL,
            last_seen_at     INTEGER NOT NULL,
            PRIMARY KEY (notifier_id, board_slug, db_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifier_heartbeats_board "
        "ON kanban_notifier_heartbeats(board_slug, db_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifier_heartbeats_seen "
        "ON kanban_notifier_heartbeats(last_seen_at)"
    )


def _recreate_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_notifier_heartbeats_board")
    conn.execute("DROP INDEX IF EXISTS idx_notifier_heartbeats_seen")
    conn.execute("DROP TABLE IF EXISTS kanban_notifier_heartbeats")
    _ensure_schema(conn)


def _connect_sidecar(*, readonly: bool = False) -> sqlite3.Connection:
    path = notifier_heartbeat_sidecar_path()
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _remove_sidecar_files() -> None:
    """Remove the heartbeat sidecar and stale SQLite journal sidecars."""
    path = notifier_heartbeat_sidecar_path()
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


def reset_notifier_heartbeats() -> None:
    """Public repair hook for the heartbeat sidecar table."""
    with contextlib.closing(_connect_sidecar()) as conn:
        with _write_txn(conn):
            _recreate_schema(conn)


def record_notifier_heartbeat(
    *,
    notifier_id: str,
    board_slug: str,
    db_path: str,
    notifier_profile: Optional[str],
    host: str,
    pid: int,
    started_at: int,
    now: Optional[int] = None,
    retention_seconds: int = NOTIFIER_HEARTBEAT_RETENTION_SECONDS,
) -> None:
    """Upsert one notifier heartbeat row in the sidecar DB."""
    when = int(now if now is not None else time.time())
    normalized_db_path = _canonical_db_path(db_path)

    def _write(conn: sqlite3.Connection) -> None:
        with _write_txn(conn):
            conn.execute(
                """
                INSERT INTO kanban_notifier_heartbeats (
                    notifier_id, board_slug, db_path, notifier_profile,
                    host, pid, started_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(notifier_id, board_slug, db_path) DO UPDATE SET
                    notifier_profile = excluded.notifier_profile,
                    host             = excluded.host,
                    pid              = excluded.pid,
                    started_at       = excluded.started_at,
                    last_seen_at     = excluded.last_seen_at
                """,
                (
                    str(notifier_id),
                    str(board_slug or _DEFAULT_BOARD_SLUG),
                    normalized_db_path,
                    notifier_profile,
                    str(host or "unknown"),
                    int(pid),
                    int(started_at),
                    when,
                ),
            )
            conn.execute(
                "DELETE FROM kanban_notifier_heartbeats WHERE last_seen_at < ?",
                (when - max(1, int(retention_seconds)),),
            )

    try:
        with contextlib.closing(_connect_sidecar()) as conn:
            try:
                _write(conn)
            except sqlite3.DatabaseError as exc:
                _rate_limited_warning(
                    "sidecar-reset-retry",
                    "kanban notifier heartbeat sidecar unhealthy; resetting sidecar "
                    "telemetry table and retrying once: %s",
                    exc,
                )
                _recreate_schema(conn)
                _write(conn)
    except sqlite3.DatabaseError as exc:
        # Opening a corrupt sidecar can fail before schema repair can run.
        # Heartbeats are derived diagnostics, so replacing only the sidecar is
        # safe and keeps the main board DB out of the repair path.
        _rate_limited_warning(
            "sidecar-replace-retry",
            "kanban notifier heartbeat sidecar unreadable; replacing sidecar "
            "and retrying once: %s",
            exc,
        )
        _remove_sidecar_files()
        with contextlib.closing(_connect_sidecar()) as conn:
            _write(conn)


def list_notifier_heartbeats(
    *,
    board_slug: Optional[str] = None,
    db_path: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    now: Optional[int] = None,
    active_window_seconds: int = NOTIFIER_HEARTBEAT_ACTIVE_WINDOW_SECONDS,
    min_last_seen_at: Optional[int] = None,
    limit: Optional[int] = NOTIFIER_HEARTBEAT_LIST_LIMIT,
) -> list[dict]:
    """List notifier heartbeat rows with active/stale classification."""
    sidecar_path = notifier_heartbeat_sidecar_path()
    if not sidecar_path.exists():
        return []

    as_of = int(now if now is not None else time.time())
    window = max(1, int(active_window_seconds))
    where: list[str] = []
    params: list[Any] = []

    if board_slug is not None:
        where.append("board_slug = ?")
        params.append(str(board_slug or _DEFAULT_BOARD_SLUG))
    if db_path is not None:
        where.append("db_path = ?")
        params.append(_canonical_db_path(db_path))
    if notifier_profile is not None:
        where.append("notifier_profile = ?")
        params.append(str(notifier_profile))
    if min_last_seen_at is not None:
        where.append("last_seen_at >= ?")
        params.append(int(min_last_seen_at))

    sql = (
        "SELECT notifier_id, board_slug, db_path, notifier_profile, "
        "       host, pid, started_at, last_seen_at "
        "FROM kanban_notifier_heartbeats"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen_at DESC, notifier_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))

    out: list[dict] = []
    with contextlib.closing(_connect_sidecar(readonly=True)) as conn:
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            last_seen = int(item.get("last_seen_at") or 0)
            age = max(0, as_of - last_seen) if last_seen else None
            item["age_seconds"] = age
            item["active"] = bool(last_seen and age is not None and age <= window)
            out.append(item)
    return out


def prune_notifier_heartbeats(
    *,
    older_than_seconds: int = NOTIFIER_HEARTBEAT_RETENTION_SECONDS,
    now: Optional[int] = None,
) -> int:
    """Delete stale notifier heartbeat rows older than the retention window."""
    sidecar_path = notifier_heartbeat_sidecar_path()
    if not sidecar_path.exists():
        return 0

    cutoff = int(now if now is not None else time.time()) - max(1, int(older_than_seconds))
    with contextlib.closing(_connect_sidecar()) as conn:
        with _write_txn(conn):
            cur = conn.execute(
                "DELETE FROM kanban_notifier_heartbeats WHERE last_seen_at < ?",
                (cutoff,),
            )
    return int(cur.rowcount or 0)
