#!/usr/bin/env python3
"""Backend-aware kanban task access for the proposal pipeline scripts.

The kanban board migrated from the legacy SQLite ``~/.hermes/kanban.db`` to a
Postgres-backed store (``kanban.backend: postgres`` in config.yaml). The apply
and reconcile scripts must read task state from whichever backend is actually
authoritative, so this module wraps both:

- sqlite mode: direct reads of an explicit ``--kanban-db`` file (tests,
  archaeology against frozen DBs) or the default ``$HERMES_HOME/kanban.db``
  when the configured backend is sqlite.
- postgres mode: reads through ``hermes_cli.kanban.store.kanban_store()``.

Fail-closed rule: if the configured backend is postgres but the store cannot
be imported/constructed, raise instead of silently falling back to the frozen
SQLite file — that silent fallback is exactly the bug this module removes.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

_HEALTH_PROBE_TASK_ID = "__kanban-access-health-probe__"


class KanbanAccess:
    """Uniform task-state reads over the sqlite file or the kanban store."""

    def __init__(self, *, backend: str, db_path: Path | None = None, store: Any = None):
        if backend not in ("sqlite", "postgres"):
            raise ValueError(f"Unsupported kanban backend: {backend!r}")
        if backend == "sqlite" and db_path is None:
            raise ValueError("sqlite mode requires db_path")
        if backend == "postgres" and store is None:
            raise ValueError("postgres mode requires a store")
        self.backend = backend
        self.db_path = Path(db_path) if db_path is not None else None
        self._store = store

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def task_state(self, task_id: str) -> dict[str, Any] | None:
        """Return {id, status, completed_at, consecutive_failures, result} or None."""
        if not task_id:
            return None
        if self.backend == "sqlite":
            return self._sqlite_task_state(task_id)
        task = self._store.get_task(task_id)
        return self._task_to_state(task) if task is not None else None

    def task_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """Return the newest non-archived task carrying the idempotency key."""
        if not idempotency_key:
            return None
        if self.backend == "sqlite":
            return self._sqlite_task_by_idempotency_key(idempotency_key)
        matches = [
            task
            for task in self._store.list_tasks()
            if str(getattr(task, "idempotency_key", "") or "") == idempotency_key
        ]
        if not matches:
            return None
        newest = max(matches, key=lambda task: getattr(task, "created_at", 0) or 0)
        return self._task_to_state(newest)

    def done_blocked_tasks(self) -> list[dict[str, Any]] | None:
        """Return done/blocked tasks for drift checks, or None if unavailable.

        Each dict carries the fields the readiness doctor's drift gate reads:
        id, status, completed_at, consecutive_failures, last_heartbeat_at,
        started_at, created_at.
        """
        if self.backend == "sqlite":
            if not self.db_path.exists():
                return None
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status IN ('done', 'blocked') ORDER BY id"
                ).fetchall()
            except sqlite3.Error:
                return None
            finally:
                conn.close()
            return [self._drift_row(dict(row)) for row in rows]
        try:
            tasks = list(self._store.list_tasks(status="done")) + list(
                self._store.list_tasks(status="blocked")
            )
        except Exception:
            return None
        tasks.sort(key=lambda task: str(task.id))
        return [self._drift_row(vars(task) if hasattr(task, "__dict__") else task.__dict__) for task in tasks]

    def latest_blocked_event_epoch(self, task_id: str) -> int | None:
        """Return the newest 'blocked' event created_at epoch for a task, if any."""
        if not task_id:
            return None
        if self.backend == "sqlite":
            if not self.db_path.exists():
                return None
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = 'blocked'",
                    (task_id,),
                ).fetchone()
            except sqlite3.Error:
                return None
            finally:
                conn.close()
            return int(row[0]) if row and row[0] is not None else None
        try:
            events = self._store.list_events(task_id)
        except Exception:
            return None
        epochs = [
            int(event.created_at)
            for event in events
            if str(getattr(event, "kind", "")) == "blocked" and getattr(event, "created_at", None) is not None
        ]
        return max(epochs) if epochs else None

    @staticmethod
    def _drift_row(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(raw.get("id")),
            "status": str(raw.get("status") or ""),
            "completed_at": raw.get("completed_at"),
            "consecutive_failures": int(raw.get("consecutive_failures") or 0),
            "last_heartbeat_at": raw.get("last_heartbeat_at"),
            "started_at": raw.get("started_at"),
            "created_at": raw.get("created_at"),
        }

    # ------------------------------------------------------------------
    # Health / backup
    # ------------------------------------------------------------------

    def verify_health(self, *, stage: str) -> None:
        """Raise ValueError if the kanban backend is not reachable/sound."""
        if self.backend == "sqlite":
            conn = sqlite3.connect(self.db_path)
            try:
                rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            finally:
                conn.close()
            if rows != ["ok"]:
                detail = "; ".join(rows) if rows else "no result"
                raise ValueError(f"kanban.db quick_check failed at {stage}: {detail}")
            return
        try:
            self._store.get_task(_HEALTH_PROBE_TASK_ID)
        except Exception as exc:
            raise ValueError(f"kanban store health probe failed at {stage}: {exc}") from exc

    def backup_to(self, run_dir: Path) -> Path | None:
        """sqlite mode: snapshot the DB file into run_dir. postgres mode: None.

        Postgres-backed boards are not file-backupable from here; rollback for
        store mutations is per-task (archive the created task id recorded in
        the manifest/audit row).
        """
        if self.backend != "sqlite":
            return None
        backup_path = Path(run_dir) / "kanban.db.bak"
        src = sqlite3.connect(self.db_path)
        try:
            dst = sqlite3.connect(backup_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return backup_path

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "kanban_db": str(self.db_path) if self.db_path is not None else None,
        }

    def close(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _task_to_state(task: Any) -> dict[str, Any]:
        return {
            "id": str(task.id),
            "status": str(task.status or ""),
            "completed_at": task.completed_at,
            "consecutive_failures": int(getattr(task, "consecutive_failures", 0) or 0),
            "result": getattr(task, "result", None),
        }

    def _sqlite_task_state(self, task_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, status, completed_at, consecutive_failures, result FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_state(row) if row else None

    def _sqlite_task_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT id, status, completed_at, consecutive_failures, result
                FROM tasks
                WHERE idempotency_key = ? AND status != 'archived'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (idempotency_key,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_state(row) if row else None

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "status": str(row["status"] or ""),
            "completed_at": row["completed_at"],
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "result": row["result"],
        }


def _resolve_backend(hermes_home: Path) -> str:
    """Resolve the configured kanban backend without requiring repo imports."""
    env_backend = (os.environ.get("HERMES_KANBAN_BACKEND") or "").strip().lower()
    if env_backend in ("sqlite", "postgres"):
        return env_backend
    config_path = hermes_home / "config.yaml"
    try:
        import yaml

        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        backend = str(((config.get("kanban") or {}).get("backend") or "")).strip().lower()
        if backend in ("sqlite", "postgres"):
            return backend
    except Exception:
        pass
    return "sqlite"


def _make_store(hermes_home: Path) -> Any:
    """Import and construct the kanban store; fail closed on import errors."""
    try:
        from hermes_cli.kanban.store import kanban_store
    except ImportError:
        repo = hermes_home / "hermes-agent"
        if (repo / "hermes_cli").exists() and str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        try:
            from hermes_cli.kanban.store import kanban_store
        except ImportError as exc:
            raise RuntimeError(
                "kanban backend is postgres but hermes_cli.kanban.store is not importable "
                f"(tried sys.path bootstrap via {repo}); refusing to fall back to the frozen "
                "SQLite kanban.db"
            ) from exc
    return kanban_store(board=None)


def resolve_kanban_access(explicit_db: str | Path | None = None, *, hermes_home: Path) -> KanbanAccess:
    """Build the right KanbanAccess for this invocation.

    An explicit ``--kanban-db`` always means direct SQLite reads of that file
    (tests, archaeology against frozen/backup DBs). Otherwise the configured
    backend decides: postgres goes through the store, sqlite reads the default
    ``$HERMES_HOME/kanban.db``.
    """
    if explicit_db:
        return KanbanAccess(backend="sqlite", db_path=Path(explicit_db).expanduser().resolve())
    hermes_home = Path(hermes_home).expanduser().resolve()
    backend = _resolve_backend(hermes_home)
    if backend == "postgres":
        return KanbanAccess(backend="postgres", store=_make_store(hermes_home))
    return KanbanAccess(backend="sqlite", db_path=hermes_home / "kanban.db")
