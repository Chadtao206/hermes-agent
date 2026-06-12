#!/usr/bin/env python3
"""Tests for the backend-aware kanban access layer used by the proposal pipeline."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import kanban_access as ka


def _init_sqlite_kanban(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT,
                status TEXT,
                created_at INTEGER,
                completed_at TEXT,
                consecutive_failures INTEGER,
                result TEXT,
                last_heartbeat_at INTEGER,
                started_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                kind TEXT,
                created_at INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _seed_sqlite_task(path: Path, *, task_id: str, status: str, idempotency_key: str | None = None,
                      created_at: int = 1, completed_at: str | None = None,
                      consecutive_failures: int = 0) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO tasks(id, idempotency_key, status, created_at, completed_at, consecutive_failures)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, idempotency_key, status, created_at, completed_at, consecutive_failures),
        )
        conn.commit()
    finally:
        conn.close()


def _stub_task(task_id: str, *, status: str = "done", completed_at: int | None = 1779737928,
               consecutive_failures: int = 0, result: str | None = None,
               idempotency_key: str | None = None, created_at: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        status=status,
        completed_at=completed_at,
        consecutive_failures=consecutive_failures,
        result=result,
        idempotency_key=idempotency_key,
        created_at=created_at,
    )


class _StubStore:
    def __init__(self, tasks: list[SimpleNamespace]):
        self.tasks = {task.id: task for task in tasks}
        self.get_task_calls: list[str] = []

    def get_task(self, task_id: str):
        self.get_task_calls.append(task_id)
        return self.tasks.get(task_id)

    def list_tasks(self, **kwargs):
        return [task for task in self.tasks.values() if task.status != "archived"]

    def close(self) -> None:
        pass


def case_explicit_db_path_resolves_sqlite_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "kanban.db"
        _init_sqlite_kanban(db)
        access = ka.resolve_kanban_access(str(db), hermes_home=Path(tmp))
        assert access.backend == "sqlite", access.backend
        assert access.describe() == {"backend": "sqlite", "kanban_db": str(db.resolve())}, access.describe()


def case_sqlite_task_state_and_idempotency_lookup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "kanban.db"
        _init_sqlite_kanban(db)
        _seed_sqlite_task(db, task_id="t_old", status="archived", idempotency_key="key-1", created_at=1)
        _seed_sqlite_task(db, task_id="t_new", status="done", idempotency_key="key-1", created_at=2,
                          completed_at="1779737928", consecutive_failures=0)
        access = ka.resolve_kanban_access(str(db), hermes_home=Path(tmp))

        state = access.task_state("t_new")
        assert state is not None and state["status"] == "done", state
        assert state["completed_at"] == "1779737928", state
        assert access.task_state("t_missing") is None

        match = access.task_by_idempotency_key("key-1")
        assert match is not None and match["id"] == "t_new", match
        assert access.task_by_idempotency_key("key-absent") is None


def case_sqlite_backup_and_health() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "kanban.db"
        _init_sqlite_kanban(db)
        access = ka.resolve_kanban_access(str(db), hermes_home=Path(tmp))
        access.verify_health(stage="preflight")
        run_dir = Path(tmp) / "run"
        run_dir.mkdir()
        backup = access.backup_to(run_dir)
        assert backup is not None and backup.exists(), backup
        assert backup.name == "kanban.db.bak", backup


def case_store_mode_task_state_maps_task() -> None:
    store = _StubStore([_stub_task("t_pg1", status="done", completed_at=1779737928, result="ok")])
    access = ka.KanbanAccess(backend="postgres", store=store)
    state = access.task_state("t_pg1")
    assert state == {
        "id": "t_pg1",
        "status": "done",
        "completed_at": 1779737928,
        "consecutive_failures": 0,
        "result": "ok",
    }, state
    assert access.task_state("t_missing") is None


def case_store_mode_idempotency_lookup_prefers_newest_non_archived() -> None:
    store = _StubStore(
        [
            _stub_task("t_a", status="archived", idempotency_key="key-2", created_at=5),
            _stub_task("t_b", status="ready", idempotency_key="key-2", created_at=2, completed_at=None),
            _stub_task("t_c", status="done", idempotency_key="key-2", created_at=4),
            _stub_task("t_d", status="done", idempotency_key="other", created_at=9),
        ]
    )
    access = ka.KanbanAccess(backend="postgres", store=store)
    match = access.task_by_idempotency_key("key-2")
    assert match is not None and match["id"] == "t_c", match
    assert access.task_by_idempotency_key("key-absent") is None


def case_store_mode_backup_none_and_describe_postgres() -> None:
    access = ka.KanbanAccess(backend="postgres", store=_StubStore([]))
    with tempfile.TemporaryDirectory() as tmp:
        assert access.backup_to(Path(tmp)) is None
    assert access.describe() == {"backend": "postgres", "kanban_db": None}, access.describe()


def case_store_mode_health_probe_round_trips() -> None:
    store = _StubStore([])
    access = ka.KanbanAccess(backend="postgres", store=store)
    access.verify_health(stage="preflight")
    assert store.get_task_calls, "health probe should round-trip through the store"

    class _BrokenStore(_StubStore):
        def get_task(self, task_id: str):
            raise RuntimeError("connection refused")

    broken = ka.KanbanAccess(backend="postgres", store=_BrokenStore([]))
    try:
        broken.verify_health(stage="preflight")
    except ValueError as exc:
        assert "preflight" in str(exc), exc
    else:
        raise AssertionError("expected verify_health to fail closed on store errors")




def case_sqlite_done_blocked_tasks_and_blocked_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "kanban.db"
        _init_sqlite_kanban(db)
        _seed_sqlite_task(db, task_id="t_done", status="done", created_at=1, completed_at="100")
        _seed_sqlite_task(db, task_id="t_blocked", status="blocked", created_at=2, consecutive_failures=1)
        _seed_sqlite_task(db, task_id="t_ready", status="ready", created_at=3)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO task_events(task_id, kind, created_at) VALUES('t_blocked', 'blocked', 555)")
        conn.execute("INSERT INTO task_events(task_id, kind, created_at) VALUES('t_blocked', 'blocked', 999)")
        conn.execute("INSERT INTO task_events(task_id, kind, created_at) VALUES('t_blocked', 'created', 2000)")
        conn.commit()
        conn.close()

        access = ka.resolve_kanban_access(str(db), hermes_home=Path(tmp))
        rows = access.done_blocked_tasks()
        assert [row["id"] for row in rows] == ["t_blocked", "t_done"], rows
        assert rows[0]["consecutive_failures"] == 1, rows[0]
        assert access.latest_blocked_event_epoch("t_blocked") == 999
        assert access.latest_blocked_event_epoch("t_done") is None

        missing = ka.KanbanAccess(backend="sqlite", db_path=Path(tmp) / "absent.db")
        assert missing.done_blocked_tasks() is None


def case_store_mode_done_blocked_tasks_and_blocked_epoch() -> None:
    class _EventStore(_StubStore):
        def __init__(self, tasks, events):
            super().__init__(tasks)
            self.events = events

        def list_tasks(self, **kwargs):
            status = kwargs.get("status")
            return [task for task in self.tasks.values() if task.status == status]

        def list_events(self, task_id):
            return self.events.get(task_id, [])

    tasks = [
        _stub_task("t_pg_done", status="done", completed_at=100),
        _stub_task("t_pg_blocked", status="blocked", completed_at=None, consecutive_failures=2),
        _stub_task("t_pg_ready", status="ready", completed_at=None),
    ]
    events = {
        "t_pg_blocked": [
            SimpleNamespace(kind="created", created_at=10),
            SimpleNamespace(kind="blocked", created_at=42),
            SimpleNamespace(kind="blocked", created_at=77),
        ]
    }
    access = ka.KanbanAccess(backend="postgres", store=_EventStore(tasks, events))
    rows = access.done_blocked_tasks()
    assert sorted(row["id"] for row in rows) == ["t_pg_blocked", "t_pg_done"], rows
    blocked = next(row for row in rows if row["id"] == "t_pg_blocked")
    assert blocked["consecutive_failures"] == 2, blocked
    assert access.latest_blocked_event_epoch("t_pg_blocked") == 77
    assert access.latest_blocked_event_epoch("t_pg_done") is None

    class _BrokenListStore(_StubStore):
        def list_tasks(self, **kwargs):
            raise RuntimeError("connection refused")

    broken = ka.KanbanAccess(backend="postgres", store=_BrokenListStore([]))
    assert broken.done_blocked_tasks() is None


def main() -> int:
    case_explicit_db_path_resolves_sqlite_mode()
    case_sqlite_task_state_and_idempotency_lookup()
    case_sqlite_backup_and_health()
    case_store_mode_task_state_maps_task()
    case_store_mode_idempotency_lookup_prefers_newest_non_archived()
    case_store_mode_backup_none_and_describe_postgres()
    case_store_mode_health_probe_round_trips()
    case_sqlite_done_blocked_tasks_and_blocked_epoch()
    case_store_mode_done_blocked_tasks_and_blocked_epoch()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
