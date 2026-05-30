# Kanban Postgres Migration — Phase 1: KanbanStore interface + SqliteKanbanStore

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a backend-agnostic `KanbanStore` interface and a `SqliteKanbanStore` that delegates to the existing `hermes_cli/kanban_db.py`, then route the kanban data call sites (agent tools, dashboard, CLI) through it — with **zero behavior change** on the current SQLite backend.

**Architecture:** A new fork-owned `hermes_cli/kanban/` package defines a `KanbanStore` Protocol (the stable interface, no `conn` argument), a `SqliteKanbanStore` adapter that owns connection lifecycle and delegates each operation to the upstream `kanban_db` functions, and a `kanban_store(board=...)` factory selected by config `kanban.backend`. A backend-parametrized conformance suite is the test backbone. Board resolution, worker-log filesystem ops, and the gateway dispatcher/notifier are **out of scope for Phase 1** (gateway glue is Phase 3; the Postgres backend + board model is Phase 2).

**Tech Stack:** Python 3.11, `pytest`, existing `hermes_cli.kanban_db` (sqlite3), `hermes_cli.config`.

**Reference spec:** `plans/kanban-postgres-migration/design.md`

---

## File structure

- Create: `hermes_cli/kanban/__init__.py` — package marker; re-exports `KanbanStore`, `kanban_store`.
- Create: `hermes_cli/kanban/store.py` — `KanbanStore` Protocol + `kanban_store()` factory + `Backend` enum.
- Create: `hermes_cli/kanban/store_sqlite.py` — `SqliteKanbanStore` adapter (delegates to `kanban_db`).
- Create: `tests/hermes_cli/kanban/conftest.py` — `kanban_store` fixture parametrized over backends (sqlite now; postgres added in Phase 2).
- Create: `tests/hermes_cli/kanban/test_store_conformance.py` — behavioral conformance suite.
- Modify: `hermes_cli/config.py` — add `kanban.backend` default.
- Modify: `tools/kanban_tools.py` — route read/write handlers through the store.
- Modify: `plugins/kanban/dashboard/plugin_api.py` — route handlers through the store.
- Modify: `hermes_cli/kanban.py` — route CLI write/read commands through the store.

**Not touched in Phase 1:** `gateway/run.py` (Phase 3), board-management functions, worker-log filesystem helpers, `kanban_db.py` internals (kept as the SQLite backend).

---

## Interface scope (Phase 1 data operations)

The `KanbanStore` exposes exactly these operations (each maps 1:1 to an existing `kanban_db` function; `board` is captured at store construction, not per call). Connection plumbing (`connect`, `connect_closing`, `write_session`, `snapshot_connect`) is replaced by these methods; board resolution and worker-log FS helpers remain free functions on `kanban_db` for now.

| Store method | Delegates to `kanban_db.` | Read/Write |
|---|---|---|
| `create_task(**kw) -> str` | `create_task(conn, **kw)` | W |
| `get_task(task_id) -> Task\|None` | `get_task(conn, task_id)` | R |
| `list_tasks(**kw) -> list[Task]` | `list_tasks(conn, **kw)` | R |
| `complete_task(task_id, *, result, summary, metadata, created_cards=None) -> bool` | `complete_task(conn, ...)` | W |
| `block_task(task_id, *, reason=None, expected_run_id=None) -> bool` | `block_task(conn, ...)` | W |
| `unblock_task(task_id) -> bool` | `unblock_task(conn, task_id)` | W |
| `schedule_task(task_id, *, reason=None) -> bool` | `schedule_task(conn, ...)` | W |
| `archive_task(task_id) -> bool` | `archive_task(conn, task_id)` | W |
| `assign_task(task_id, profile) -> bool` | `assign_task(conn, task_id, profile)` | W |
| `reassign_task(task_id, profile, *, reclaim_first=False, reason=None) -> bool` | `reassign_task(conn, ...)` | W |
| `reclaim_task(task_id, *, reason=None) -> bool` | `reclaim_task(conn, ...)` | W |
| `set_status_direct(task_id, new_status) -> bool` | `set_status_direct(conn, ...)` | W |
| `set_task_priority(task_id, priority) -> bool` | `set_task_priority(conn, ...)` | W |
| `edit_task_fields(task_id, *, title=None, body=None) -> bool` | `edit_task_fields(conn, ...)` | W |
| `edit_completed_task_result(task_id, **kw) -> bool` | `edit_completed_task_result(conn, ...)` | W |
| `delete_task(task_id) -> bool` | `delete_task(conn, task_id)` | W |
| `promote_task(task_id) -> bool` | `promote_task(conn, task_id)` | W |
| `set_workspace_path(task_id, path) -> bool` | `set_workspace_path(conn, ...)` | W |
| `link_tasks(parent_id, child_id, *, relation_type=...) -> None` | `link_tasks(conn, ...)` | W |
| `unlink_tasks(parent_id, child_id, *, relation_type=...) -> bool` | `unlink_tasks(conn, ...)` | W |
| `parent_ids(task_id) -> list[str]` | `parent_ids(conn, task_id)` | R |
| `child_ids(task_id) -> list[str]` | `child_ids(conn, task_id)` | R |
| `add_comment(task_id, *, author, body) -> int` | `add_comment(conn, task_id, author, body)` | W |
| `list_comments(task_id) -> list[...]` | `list_comments(conn, task_id)` | R |
| `list_events(task_id, **kw) -> list[...]` | `list_events(conn, ...)` | R |
| `gc_events(**kw) -> int` | `gc_events(conn, ...)` | W |
| `list_runs(task_id) -> list[...]` | `list_runs(conn, task_id)` | R |
| `get_run(run_id) -> Run\|None` | `get_run(conn, run_id)` | R |
| `latest_run(task_id) -> Run\|None` | `latest_run(conn, task_id)` | R |
| `latest_summary(task_id) -> str\|None` | `latest_summary(conn, task_id)` | R |
| `latest_summaries(task_ids) -> dict` | `latest_summaries(conn, task_ids)` | R |
| `add_notify_sub(**kw) -> int` | `add_notify_sub(conn, **kw)` | W |
| `remove_notify_sub(**kw) -> bool` | `remove_notify_sub(conn, **kw)` | W |
| `list_notify_subs() -> list[...]` | `list_notify_subs(conn)` | R |
| `claim_unseen_events_for_sub(**kw) -> tuple` | `claim_unseen_events_for_sub(conn, **kw)` | W |
| `add_profile_event_sub(**kw)` | `add_profile_event_sub(conn, **kw)` | W |
| `remove_profile_event_sub(**kw) -> bool` | `remove_profile_event_sub(conn, **kw)` | W |
| `list_profile_event_subs(**kw) -> list[...]` | `list_profile_event_subs(conn, **kw)` | R |
| `claim_unseen_events_for_profile_sub(**kw) -> tuple` | `claim_unseen_events_for_profile_sub(conn, **kw)` | W |
| `list_profile_wake_events(**kw) -> list[...]` | `list_profile_wake_events(conn, **kw)` | R |
| `record_notifier_heartbeat(**kw)` | `record_notifier_heartbeat(conn, **kw)` | W |
| `list_notifier_heartbeats(**kw) -> list[...]` | `list_notifier_heartbeats(conn, **kw)` | R |
| `heartbeat_worker(**kw) -> bool` | `heartbeat_worker(conn, **kw)` | W |
| `recompute_ready() -> int` | `recompute_ready(conn)` | W |
| `has_spawnable_ready() -> bool` | `has_spawnable_ready(conn)` | R |
| `board_stats() -> dict` | `board_stats(conn)` | R |
| `known_assignees() -> list[str]` | `known_assignees(conn)` | R |

`dispatch_once` is **not** a store method (it spawns processes and is dispatcher glue — handled in Phase 3). `task_age` is a pure function (stays on `kanban_db`). Board management and worker-log helpers stay as free functions.

---

### Task 1: Config flag `kanban.backend`

**Files:**
- Modify: `hermes_cli/config.py`
- Test: `tests/hermes_cli/kanban/test_store_factory.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_store_factory.py
import hermes_cli.config as cfg


def test_kanban_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {})
    from hermes_cli.kanban.store import resolve_backend
    assert resolve_backend() == "sqlite"


def test_kanban_backend_reads_config(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban.store import resolve_backend
    assert resolve_backend() == "postgres"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_factory.py -q`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.kanban`

- [ ] **Step 3: Create the package + `resolve_backend`**

```python
# hermes_cli/kanban/__init__.py
"""Fork-owned kanban store package: backend-agnostic interface over the board DB."""
from .store import KanbanStore, kanban_store, resolve_backend  # noqa: F401
```

```python
# hermes_cli/kanban/store.py  (partial — factory + backend resolution)
from __future__ import annotations
from typing import Optional

_VALID_BACKENDS = {"sqlite", "postgres"}


def resolve_backend() -> str:
    """Return the configured kanban backend ('sqlite' default). Reads config
    defensively; any failure falls back to 'sqlite' so default deployments and
    upstream are unaffected."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
        backend = str(kanban_cfg.get("backend") or "sqlite").strip().lower()
    except Exception:
        return "sqlite"
    return backend if backend in _VALID_BACKENDS else "sqlite"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_factory.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Add the config default**

In `hermes_cli/config.py`, add `"backend": "sqlite"` to the `kanban` section of the default-config dict (search for the existing `"kanban": {` defaults block; add the key alongside `single_writer_daemon`).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban/__init__.py hermes_cli/kanban/store.py hermes_cli/config.py tests/hermes_cli/kanban/test_store_factory.py
git commit -m "feat(kanban): add kanban.backend config + store package skeleton"
```

---

### Task 2: `KanbanStore` Protocol

**Files:**
- Modify: `hermes_cli/kanban/store.py`
- Test: `tests/hermes_cli/kanban/test_store_protocol.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_store_protocol.py
from hermes_cli.kanban.store import KanbanStore


def test_protocol_lists_core_methods():
    # The interface must expose the data operations callers depend on.
    for name in (
        "create_task", "get_task", "list_tasks", "complete_task", "block_task",
        "unblock_task", "schedule_task", "archive_task", "assign_task",
        "reassign_task", "reclaim_task", "set_status_direct", "set_task_priority",
        "edit_task_fields", "delete_task", "link_tasks", "unlink_tasks",
        "add_comment", "add_notify_sub", "remove_notify_sub", "list_notify_subs",
        "claim_unseen_events_for_sub", "add_profile_event_sub",
        "remove_profile_event_sub", "list_profile_event_subs",
        "recompute_ready", "has_spawnable_ready", "close",
    ):
        assert hasattr(KanbanStore, name), name
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_protocol.py -q`
Expected: FAIL — `KanbanStore` has no such attributes (only factory exists).

- [ ] **Step 3: Define the Protocol**

Add to `hermes_cli/kanban/store.py` a `typing.Protocol` named `KanbanStore` declaring every method in the **Interface scope** table above with the listed signatures, plus `close(self) -> None`. Use `...` bodies (Protocols declare, not implement). Example shape (declare ALL table rows, not just these):

```python
from typing import Any, Optional, Protocol, runtime_checkable
from hermes_cli.kanban_db import Task  # dataclass reused as the return type


@runtime_checkable
class KanbanStore(Protocol):
    board: Optional[str]

    def create_task(self, **kwargs: Any) -> str: ...
    def get_task(self, task_id: str) -> Optional[Task]: ...
    def list_tasks(self, **kwargs: Any) -> list[Task]: ...
    def complete_task(self, task_id: str, *, result: Optional[str] = None,
                      summary: Optional[str] = None, metadata: Optional[dict] = None,
                      created_cards: Optional[list[str]] = None) -> bool: ...
    def block_task(self, task_id: str, *, reason: Optional[str] = None,
                   expected_run_id: Optional[int] = None) -> bool: ...
    def unblock_task(self, task_id: str) -> bool: ...
    def schedule_task(self, task_id: str, *, reason: Optional[str] = None) -> bool: ...
    def archive_task(self, task_id: str) -> bool: ...
    def assign_task(self, task_id: str, profile: Optional[str]) -> bool: ...
    def reassign_task(self, task_id: str, profile: Optional[str], *,
                      reclaim_first: bool = False, reason: Optional[str] = None) -> bool: ...
    def reclaim_task(self, task_id: str, *, reason: Optional[str] = None) -> bool: ...
    def set_status_direct(self, task_id: str, new_status: str) -> bool: ...
    def set_task_priority(self, task_id: str, priority: int) -> bool: ...
    def edit_task_fields(self, task_id: str, *, title: Optional[str] = None,
                         body: Optional[str] = None) -> bool: ...
    def delete_task(self, task_id: str) -> bool: ...
    def link_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> None: ...
    def unlink_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> bool: ...
    def add_comment(self, task_id: str, *, author: str, body: str) -> int: ...
    def add_notify_sub(self, **kwargs: Any) -> int: ...
    def remove_notify_sub(self, **kwargs: Any) -> bool: ...
    def list_notify_subs(self) -> list[Any]: ...
    def claim_unseen_events_for_sub(self, **kwargs: Any) -> tuple: ...
    def add_profile_event_sub(self, **kwargs: Any) -> Any: ...
    def remove_profile_event_sub(self, **kwargs: Any) -> bool: ...
    def list_profile_event_subs(self, **kwargs: Any) -> list[Any]: ...
    def recompute_ready(self) -> int: ...
    def has_spawnable_ready(self) -> bool: ...
    # ... (declare the remaining rows from the Interface scope table) ...
    def close(self) -> None: ...
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_protocol.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store.py tests/hermes_cli/kanban/test_store_protocol.py
git commit -m "feat(kanban): define KanbanStore protocol (data-operation surface)"
```

---

### Task 3: Conformance test harness (the safety net)

**Files:**
- Create: `tests/hermes_cli/kanban/conftest.py`
- Create: `tests/hermes_cli/kanban/test_store_conformance.py`

- [ ] **Step 1: Write the parametrized fixture**

```python
# tests/hermes_cli/kanban/conftest.py
import pytest

# Backends under test. Postgres is added in Phase 2 (an env-gated param).
_BACKENDS = ["sqlite"]


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path, monkeypatch):
    """Yield a fresh KanbanStore for each backend. SQLite uses an isolated
    tmp board DB; the store owns connection lifecycle."""
    backend = request.param
    if backend == "sqlite":
        db = tmp_path / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
        from hermes_cli import kanban_db as kb
        kb.connect(db_path=db, readonly=False, _bootstrap=True).close()
        from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
        s = SqliteKanbanStore(board=None)
        try:
            yield s
        finally:
            s.close()
    else:
        pytest.skip(f"backend {backend} not available in Phase 1")
```

- [ ] **Step 2: Write the first conformance test (create → get round-trip)**

```python
# tests/hermes_cli/kanban/test_store_conformance.py
def test_create_then_get(store):
    tid = store.create_task(title="hello", assignee="engineer")
    t = store.get_task(tid)
    assert t is not None and t.id == tid and t.title == "hello"


def test_get_missing_returns_none(store):
    assert store.get_task("t_does_not_exist") is None
```

- [ ] **Step 3: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -q`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.kanban.store_sqlite` (implemented in Task 4).

- [ ] **Step 4: Commit (harness only; impl follows)**

```bash
git add tests/hermes_cli/kanban/conftest.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "test(kanban): backend-parametrized store conformance harness"
```

---

### Task 4: `SqliteKanbanStore` — task lifecycle

**Files:**
- Create: `hermes_cli/kanban/store_sqlite.py`
- Test: `tests/hermes_cli/kanban/test_store_conformance.py` (extend)

- [ ] **Step 1: Implement the adapter (lifecycle subset)**

`SqliteKanbanStore` owns connection lifecycle and delegates. Reads use a snapshot connection (read-after-write safe, per `design.md`); writes route through `kb.write_session` when single-writer is on, else a writable conn. Implement a private `_read(fn)` and `_write(op, **kw)` helper to keep delegation DRY:

```python
# hermes_cli/kanban/store_sqlite.py
from __future__ import annotations
from typing import Any, Optional
from hermes_cli import kanban_db as kb


class SqliteKanbanStore:
    """KanbanStore backed by the upstream kanban_db (sqlite3). Owns connection
    lifecycle; callers never pass a conn. Behavior is identical to calling the
    kanban_db functions directly — this is a delegating adapter."""

    def __init__(self, board: Optional[str] = None):
        self.board = board

    def close(self) -> None:  # no persistent conn held; nothing to close
        return None

    # --- helpers ---------------------------------------------------------
    def _read(self, fn):
        """Run a read closure on a fresh read connection (snapshot under
        single-writer, else writable). fn receives the conn."""
        kb2, conn = _read_conn(self.board)
        try:
            return fn(conn)
        finally:
            conn.close()

    def _write(self, op: str, **kwargs):
        """Run a single write op via write_session (daemon under single-writer,
        else a local writable conn)."""
        with kb.write_session(board=self.board) as w:
            return getattr(w, op)(**kwargs)

    # --- task lifecycle --------------------------------------------------
    def create_task(self, **kwargs: Any) -> str:
        return self._write("create_task", **kwargs)

    def get_task(self, task_id: str):
        return self._read(lambda c: kb.get_task(c, task_id))

    def list_tasks(self, **kwargs: Any):
        return self._read(lambda c: kb.list_tasks(c, **kwargs))

    def complete_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("complete_task", task_id=task_id, **kwargs)

    def block_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("block_task", task_id=task_id, **kwargs)

    def unblock_task(self, task_id: str) -> bool:
        return self._write("unblock_task", task_id=task_id)

    def schedule_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("schedule_task", task_id=task_id, **kwargs)

    def archive_task(self, task_id: str) -> bool:
        return self._write("archive_task", task_id=task_id)

    def assign_task(self, task_id: str, profile: Optional[str]) -> bool:
        return self._write("assign_task", task_id=task_id, profile=profile)

    def reassign_task(self, task_id: str, profile: Optional[str], **kwargs: Any) -> bool:
        return self._write("reassign_task", task_id=task_id, profile=profile, **kwargs)

    def reclaim_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("reclaim_task", task_id=task_id, **kwargs)

    def set_status_direct(self, task_id: str, new_status: str) -> bool:
        return self._write("set_status_direct", task_id=task_id, new_status=new_status)

    def set_task_priority(self, task_id: str, priority: int) -> bool:
        return self._write("set_task_priority", task_id=task_id, priority=priority)

    def edit_task_fields(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("edit_task_fields", task_id=task_id, **kwargs)

    def delete_task(self, task_id: str) -> bool:
        return self._write("delete_task", task_id=task_id)


def _read_conn(board):
    """Mirror tools.kanban_tools._connect read policy: snapshot under
    single-writer, else writable connect."""
    if kb.single_writer_enabled():
        from tools.kanban_tools import _SnapshotReadConn  # reuse the wrapper
        return kb, _SnapshotReadConn(kb.snapshot_connect(board=board))
    return kb, kb.connect(board=board)
```

> Note: every write op named here (`create_task`, `complete_task`, `block_task`, `unblock_task`, `schedule_task`, `archive_task`, `assign_task`, `reassign_task`, `reclaim_task`, `set_status_direct`, `set_task_priority`, `edit_task_fields`, `delete_task`) is already in `OP_ALLOWLIST` (verified in the single-writer work), so `write_session`/RemoteWriter routing works.

- [ ] **Step 2: Extend conformance tests for lifecycle**

```python
def test_block_unblock_roundtrip(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.block_task(tid, reason="need input") is True
    assert store.get_task(tid).status == "blocked"
    assert store.unblock_task(tid) is True
    assert store.get_task(tid).status == "ready"


def test_priority_and_edit_and_status_direct(store):
    tid = store.create_task(title="orig", assignee="engineer")
    assert store.set_task_priority(tid, 7) is True
    assert store.get_task(tid).priority == 7
    assert store.edit_task_fields(tid, title="renamed") is True
    assert store.get_task(tid).title == "renamed"
    assert store.set_status_direct(tid, "todo") is True
    assert store.get_task(tid).status == "todo"


def test_reassign_and_delete(store):
    tid = store.create_task(title="x", assignee="engineer")
    assert store.reassign_task(tid, "reviewer") is True
    assert store.get_task(tid).assignee == "reviewer"
    assert store.delete_task(tid) is True
    assert store.get_task(tid) is None
```

- [ ] **Step 3: Run conformance to verify pass**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -q`
Expected: PASS (all lifecycle tests, sqlite backend)

- [ ] **Step 4: Commit**

```bash
git add hermes_cli/kanban/store_sqlite.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban): SqliteKanbanStore task-lifecycle ops + conformance tests"
```

---

### Task 5: `SqliteKanbanStore` — links, comments, events, runs

**Files:**
- Modify: `hermes_cli/kanban/store_sqlite.py`
- Test: `tests/hermes_cli/kanban/test_store_conformance.py` (extend)

- [ ] **Step 1: Add the methods**

Append to `SqliteKanbanStore`:

```python
    def link_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> None:
        return self._write("link_tasks", parent_id=parent_id, child_id=child_id, **kwargs)

    def unlink_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> bool:
        return self._write("unlink_tasks", parent_id=parent_id, child_id=child_id, **kwargs)

    def parent_ids(self, task_id: str) -> list[str]:
        return self._read(lambda c: kb.parent_ids(c, task_id))

    def child_ids(self, task_id: str) -> list[str]:
        return self._read(lambda c: kb.child_ids(c, task_id))

    def add_comment(self, task_id: str, *, author: str, body: str) -> int:
        return self._write("add_comment", task_id=task_id, author=author, body=body)

    def list_comments(self, task_id: str):
        return self._read(lambda c: kb.list_comments(c, task_id))

    def list_events(self, task_id: str, **kwargs: Any):
        return self._read(lambda c: kb.list_events(c, task_id, **kwargs))

    def list_runs(self, task_id: str):
        return self._read(lambda c: kb.list_runs(c, task_id))

    def get_run(self, run_id: int):
        return self._read(lambda c: kb.get_run(c, run_id))

    def latest_run(self, task_id: str):
        return self._read(lambda c: kb.latest_run(c, task_id))

    def latest_summary(self, task_id: str):
        return self._read(lambda c: kb.latest_summary(c, task_id))

    def latest_summaries(self, task_ids):
        return self._read(lambda c: kb.latest_summaries(c, task_ids))

    def edit_completed_task_result(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("edit_completed_task_result", task_id=task_id, **kwargs)
```

> `link_tasks`/`unlink_tasks`/`add_comment` are allowlisted; `edit_completed_task_result` is NOT yet in `OP_ALLOWLIST` — add `"edit_completed_task_result"` to `OP_ALLOWLIST` in `hermes_cli/kanban_writer_daemon.py` in this task (it's a dashboard recovery write).

- [ ] **Step 2: Conformance tests**

```python
def test_link_unlink_and_parents_children(store):
    p = store.create_task(title="parent", assignee="engineer")
    c = store.create_task(title="child", assignee="engineer")
    store.link_tasks(p, c)
    assert c in store.child_ids(p)
    assert p in store.parent_ids(c)
    assert store.unlink_tasks(p, c) is True
    assert store.child_ids(p) == []


def test_comment_roundtrip(store):
    tid = store.create_task(title="x", assignee="engineer")
    cid = store.add_comment(tid, author="ops", body="note")
    assert isinstance(cid, int)
    bodies = [c["body"] if isinstance(c, dict) else c.body for c in store.list_comments(tid)]
    assert "note" in bodies
```

- [ ] **Step 3: Run conformance**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add hermes_cli/kanban/store_sqlite.py hermes_cli/kanban_writer_daemon.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban): SqliteKanbanStore links/comments/events/runs"
```

---

### Task 6: `SqliteKanbanStore` — notify subs + event claiming

**Files:**
- Modify: `hermes_cli/kanban/store_sqlite.py`
- Test: `tests/hermes_cli/kanban/test_store_conformance.py` (extend)

- [ ] **Step 1: Add the methods**

```python
    def add_notify_sub(self, **kwargs: Any) -> int:
        return self._write("add_notify_sub", **kwargs)

    def remove_notify_sub(self, **kwargs: Any) -> bool:
        return self._write("remove_notify_sub", **kwargs)

    def list_notify_subs(self):
        return self._read(lambda c: kb.list_notify_subs(c))

    def claim_unseen_events_for_sub(self, **kwargs: Any) -> tuple:
        return self._write("claim_unseen_events_for_sub", **kwargs)
```

> `claim_unseen_events_for_sub` IS already allowlisted (notifier uses it). `add_notify_sub`/`remove_notify_sub` are allowlisted (added during the dashboard work).

- [ ] **Step 2: Conformance test**

```python
def test_notify_sub_add_list_remove(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    subs = store.list_notify_subs()
    assert any((s["task_id"] if isinstance(s, dict) else s.task_id) == tid for s in subs)
    assert store.remove_notify_sub(task_id=tid, platform="telegram", chat_id="c1") is True
```

- [ ] **Step 3: Run + Commit**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -q` → PASS

```bash
git add hermes_cli/kanban/store_sqlite.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban): SqliteKanbanStore notify subs + event claiming"
```

---

### Task 7: `SqliteKanbanStore` — profile-event subs/wake + heartbeats + dispatch reads

**Files:**
- Modify: `hermes_cli/kanban/store_sqlite.py`
- Test: `tests/hermes_cli/kanban/test_store_conformance.py` (extend)

- [ ] **Step 1: Add the methods**

```python
    def add_profile_event_sub(self, **kwargs: Any):
        return self._write("add_profile_event_sub", **kwargs)

    def remove_profile_event_sub(self, **kwargs: Any) -> bool:
        return self._write("remove_profile_event_sub", **kwargs)

    def list_profile_event_subs(self, **kwargs: Any):
        return self._read(lambda c: kb.list_profile_event_subs(c, **kwargs))

    def claim_unseen_events_for_profile_sub(self, **kwargs: Any) -> tuple:
        return self._write("claim_unseen_events_for_profile_sub", **kwargs)

    def list_profile_wake_events(self, **kwargs: Any):
        return self._read(lambda c: kb.list_profile_wake_events(c, **kwargs))

    def record_notifier_heartbeat(self, **kwargs: Any):
        return self._write("record_notifier_heartbeat", **kwargs)

    def list_notifier_heartbeats(self, **kwargs: Any):
        return self._read(lambda c: kb.list_notifier_heartbeats(c, **kwargs))

    def heartbeat_worker(self, **kwargs: Any) -> bool:
        return self._write("heartbeat_worker", **kwargs)

    def recompute_ready(self) -> int:
        return self._write("recompute_ready")

    def has_spawnable_ready(self) -> bool:
        return self._read(lambda c: kb.has_spawnable_ready(c))

    def board_stats(self):
        return self._read(lambda c: kb.board_stats(c))

    def known_assignees(self):
        return self._read(lambda c: kb.known_assignees(c))
```

> Add to `OP_ALLOWLIST` any of these write ops not already present: `claim_unseen_events_for_profile_sub`, `record_notifier_heartbeat` (verify against the current allowlist; `heartbeat_worker`, `add_profile_event_sub`, `remove_profile_event_sub`, `recompute_ready` are already in it).

- [ ] **Step 2: Conformance test**

```python
def test_profile_sub_and_recompute(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    subs = store.list_profile_event_subs(task_id=tid, profile="engineer", enabled_only=False)
    assert subs
    assert isinstance(store.recompute_ready(), int)
    assert isinstance(store.has_spawnable_ready(), bool)
    assert store.remove_profile_event_sub(task_id=tid, profile="engineer", name="") is True
```

- [ ] **Step 3: Run + Commit**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -q` → PASS

```bash
git add hermes_cli/kanban/store_sqlite.py hermes_cli/kanban_writer_daemon.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban): SqliteKanbanStore profile-events/heartbeats/dispatch-reads (interface complete)"
```

---

### Task 8: `kanban_store()` factory

**Files:**
- Modify: `hermes_cli/kanban/store.py`
- Test: `tests/hermes_cli/kanban/test_store_factory.py` (extend)

- [ ] **Step 1: Failing test**

```python
def test_factory_returns_sqlite_store_by_default(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "sqlite"}})
    from hermes_cli.kanban.store import kanban_store
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    s = kanban_store(board=None)
    try:
        assert isinstance(s, SqliteKanbanStore)
    finally:
        s.close()


def test_factory_postgres_not_available_phase1(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban.store import kanban_store
    import pytest
    with pytest.raises(NotImplementedError):
        kanban_store(board=None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_factory.py -q`
Expected: FAIL — `kanban_store` not defined.

- [ ] **Step 3: Implement the factory**

```python
# hermes_cli/kanban/store.py  (append)
def kanban_store(board: Optional[str] = None) -> "KanbanStore":
    """Return the configured KanbanStore for a board. Postgres backend lands in
    Phase 2; until then selecting it raises NotImplementedError loudly rather
    than silently falling back."""
    backend = resolve_backend()
    if backend == "sqlite":
        from .store_sqlite import SqliteKanbanStore
        return SqliteKanbanStore(board=board)
    raise NotImplementedError(f"kanban backend '{backend}' not available yet")
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_factory.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store.py tests/hermes_cli/kanban/test_store_factory.py
git commit -m "feat(kanban): kanban_store() backend factory"
```

---

### Task 9: Route agent tools through the store

**Files:**
- Modify: `tools/kanban_tools.py`
- Test: existing `tests/hermes_cli/test_kanban_tools_write_session.py` + `tests/hermes_cli/test_kanban_notify.py` must stay green.

- [ ] **Step 1: Add a store accessor next to `_connect`**

```python
# tools/kanban_tools.py
def _store(board=None):
    from hermes_cli.kanban.store import kanban_store
    return kanban_store(board=board)
```

- [ ] **Step 2: Convert ONE read handler to the store, run its test**

Convert `_handle_show` to use the store instead of `_connect` + `kb.get_task`:

```python
def _handle_show(args, **kw):
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required")
    store = _store(args.get("board"))
    try:
        task = store.get_task(tid)
    finally:
        store.close()
    if task is None:
        return tool_error(f"task {tid} not found")
    # ... existing serialization unchanged ...
```

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_tools_write_session.py -q`
Expected: PASS (the read-after-write test now goes through the store).

- [ ] **Step 3: Convert remaining handlers (one commit per group), run tests after each**

Convert in groups, each followed by `pytest tests/hermes_cli/test_kanban_tools_write_session.py tests/hermes_cli/test_kanban_notify.py -q`:
1. create/complete/block/unblock → `store.create_task/...`
2. comment/link/assign/reclaim/reassign → `store....`
3. list/show/runs reads → `store.list_tasks/get_task/list_runs`

Each handler drops its `with kb.write_session(...) as w:` / `_connect()` boilerplate in favor of `store = _store(board); try: store.OP(...) finally: store.close()`.

- [ ] **Step 4: Full kanban-tools regression**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_tools_write_session.py tests/hermes_cli/test_kanban_notify.py -q`
Expected: PASS (no behavior change — sqlite backend delegates to the same code).

- [ ] **Step 5: Commit**

```bash
git add tools/kanban_tools.py
git commit -m "refactor(kanban): route agent tool handlers through KanbanStore"
```

---

### Task 10: Route dashboard endpoints through the store

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Test: `tests/plugins/test_kanban_dashboard_write_session.py`, `tests/plugins/test_kanban_dashboard_plugin_api.py` stay green.

- [ ] **Step 1: Add a store helper mirroring `_conn`**

```python
def _store(board=None):
    from hermes_cli.kanban.store import kanban_store
    return kanban_store(board=board)
```

- [ ] **Step 2: Convert read endpoints (get_board diagnostics, list_profile_subs, get_task_log, stats, assignees) to `store` reads; run the suite**

Replace `conn = _conn(board=board, readonly=True); ...; conn.close()` with `store = _store(board); try: ...store reads... finally: store.close()`. For the board snapshot read path, `store.list_tasks()` / `store.board_stats()` replace direct conn queries where a store method exists; raw `SELECT *` diagnostics queries that have no store method may keep a read-only conn via the store's own connection (expose `store._read(...)` is internal — instead add a `KanbanStore.diagnostics_rows()` method only if needed; otherwise keep those few raw reads on `_conn(readonly=True)` for Phase 1).

Run: `venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_api.py -q` → PASS

- [ ] **Step 3: Convert write endpoints to `store` writes; run the suite**

Replace each `with kanban_db.write_session(board=board) as w: w.OP(...)` with `store = _store(board); try: store.OP(...) finally: store.close()`.

Run: `venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_write_session.py -q` → PASS

- [ ] **Step 4: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py
git commit -m "refactor(kanban): route dashboard endpoints through KanbanStore"
```

---

### Task 11: Route CLI commands through the store

**Files:**
- Modify: `hermes_cli/kanban.py`
- Test: add `tests/hermes_cli/test_kanban_cli_store.py`

- [ ] **Step 1: Failing test (CLI create routes through store)**

```python
# tests/hermes_cli/test_kanban_cli_store.py
def test_cli_create_uses_store(monkeypatch, tmp_path):
    import hermes_cli.kanban as cli
    captured = {}
    class FakeStore:
        board = None
        def create_task(self, **kw): captured.update(kw); return "t_fake"
        def close(self): pass
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: FakeStore(), raising=False)
    # invoke the create command handler with parsed args (see existing CLI tests for arg shape)
    # assert captured["title"] == "demo"
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_cli_store.py -q`
Expected: FAIL (CLI still uses `connect_closing`).

- [ ] **Step 3: Convert CLI write commands (`create`, `complete`, `block`, `unblock`, `schedule`, `assign`, `reassign`, `reclaim`, `comment`, `link`, `unlink`, `archive`, `delete`, `edit`) and read commands (`show`, `list`, `runs`) to `store = kanban_store(board); try: store.OP(...) finally: store.close()`.**

Import `from hermes_cli.kanban.store import kanban_store` at the top of `hermes_cli/kanban.py`.

- [ ] **Step 4: Run CLI + full kanban regression**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_cli_store.py tests/hermes_cli/ tests/plugins/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban.py tests/hermes_cli/test_kanban_cli_store.py
git commit -m "refactor(kanban): route CLI commands through KanbanStore"
```

---

### Task 12: Phase-1 acceptance gate

**Files:** none (verification only).

- [ ] **Step 1: Full kanban + plugin regression on the sqlite backend**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_tools_write_session.py tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/kanban/ tests/plugins/ tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_single_writer.py -q`
Expected: PASS (zero behavior change)

- [ ] **Step 2: Confirm the seam**

Run: `grep -rn "connect_closing\|write_session\|snapshot_connect" tools/kanban_tools.py hermes_cli/kanban.py plugins/kanban/dashboard/plugin_api.py | grep -v "_store\|def "`
Expected: only the dashboard's few raw-diagnostics read-conns remain (documented in Task 10); all data CRUD goes through the store.

- [ ] **Step 3: Commit a phase marker**

```bash
git commit --allow-empty -m "chore(kanban): Phase 1 complete — all kanban data callers route through KanbanStore (sqlite backend, zero behavior change)"
```

---

## Subsequent phases (separate plans, written when reached)

- **Phase 2** — `pg_schema.sql`, `PostgresKanbanStore` (psycopg + Supabase pool), add `postgres` to the conformance `_BACKENDS` (CI Postgres via docker/testcontainers), resolve the **board model in Postgres** (board_id column vs schema-per-board). Gate: conformance suite green against Postgres.
- **Phase 3** — extract `gateway/run.py` dispatcher/notifier kanban bodies into `hermes_cli/kanban_glue.py` (`run_dispatch_tick(store)` / `run_notifier_tick(store, adapters)`); make dispatch/notify backend-agnostic.
- **Phase 4** — `migrate_sqlite_to_pg.py` + dry-run + verification.
- **Phase 5** — maintenance-window cutover + rollback window.
- **Phase 6** — retire SQLite-only life-support under `backend=postgres`; web-dashboard phase (Supabase Auth/RLS/Realtime).
