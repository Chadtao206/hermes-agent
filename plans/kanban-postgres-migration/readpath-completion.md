# Kanban Postgres read-path completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `kanban.backend=postgres`, the worker in-agent kanban tools and the `hermes kanban` board-management CLI read and resolve the **live Postgres** board, closing the split-brain that auto-blocks workers.

**Architecture:** Three layers — (1) propagation: `resolve_backend()` honors a `HERMES_KANBAN_BACKEND` env override and the gateway exports backend+DSN into its process env so spawned workers inherit it; (2) worker reads in `tools/kanban_tools.py` branch on backend → store under Postgres; (3) the CLI `init_db` sqlite preamble is skipped under Postgres and the three still-sqlite-coupled read handlers (`show`/`liveness`/`context`) route through the store. The crux is a new `KanbanStore.build_worker_context(task_id)` — sqlite delegates to the upstream function (byte-identical), Postgres reassembles the identical string from store primitives, pinned by a cross-backend parity test.

**Tech Stack:** Python, psycopg3 + psycopg_pool, pytest. `kanban_db.py` is upstream and **must not be edited**. Test interpreter (only this venv has psycopg+pytest): `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Postgres tests use the docker `postgres:16-alpine` fixture (`_pg_dsn` / `store` in `tests/hermes_cli/kanban/conftest.py`); they auto-skip if docker is unavailable.

**Hard boundaries (apply to EVERY task):**
- `hermes_cli/kanban_db.py` is **not edited**. Its module-level helpers/constants may be **imported** and reused, never modified.
- The **sqlite path stays byte-identical**. Every change is `if resolve_backend()=="postgres": <new> else: <existing>` (or an additive optional kwarg that defaults to current behavior).
- **No secret leakage**: the DSN lives only in the root config + the gateway process env; never log its value.

---

### Task 1: `resolve_backend()` honors `HERMES_KANBAN_BACKEND` env override

**Files:**
- Modify: `hermes_cli/kanban/store.py` (`resolve_backend()`, currently ~line 268-280; `_VALID_BACKENDS` at line 11)
- Test: `tests/hermes_cli/kanban/test_store_factory.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/hermes_cli/kanban/test_store_factory.py`:

```python
def test_resolve_backend_env_override_wins(monkeypatch):
    from hermes_cli.kanban import store as store_mod
    # Config says sqlite (default), env says postgres → env wins.
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    assert store_mod.resolve_backend() == "postgres"


def test_resolve_backend_env_invalid_falls_through(monkeypatch):
    from hermes_cli.kanban import store as store_mod
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "mariadb")
    # Invalid env value is ignored → falls through to config (sqlite default).
    assert store_mod.resolve_backend() == "sqlite"


def test_resolve_backend_no_env_unchanged(monkeypatch):
    from hermes_cli.kanban import store as store_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    # No env → existing config behavior (sqlite default in test env).
    assert store_mod.resolve_backend() == "sqlite"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_factory.py -k resolve_backend_env -v`
Expected: `test_resolve_backend_env_override_wins` FAILS (returns "sqlite").

- [ ] **Step 3: Implement the env override**

In `hermes_cli/kanban/store.py`, replace the body of `resolve_backend()` with:

```python
def resolve_backend() -> str:
    """Return the configured kanban backend ('sqlite' default).

    Precedence: the ``HERMES_KANBAN_BACKEND`` env override (if it names a valid
    backend) wins, so the gateway can propagate the live backend to spawned
    workers whose profile-scoped config does not carry it. Otherwise read
    config defensively; any failure falls back to 'sqlite' so default
    deployments and upstream are unaffected."""
    import os
    env_backend = (os.environ.get("HERMES_KANBAN_BACKEND") or "").strip().lower()
    if env_backend in _VALID_BACKENDS:
        return env_backend
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

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_factory.py -v`
Expected: PASS (all, including pre-existing factory tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store.py tests/hermes_cli/kanban/test_store_factory.py
git commit -m "feat(kanban-pg): resolve_backend() honors HERMES_KANBAN_BACKEND env override"
```

---

### Task 2: Add `build_worker_context` to the store protocol + sqlite impl (byte-identical delegate); widen `parent_ids`/`child_ids` protocol for rollups

**Files:**
- Modify: `hermes_cli/kanban/store.py` (protocol; add method decl near `latest_summary`, ~line 185; update `parent_ids`/`child_ids` decls at 159-161)
- Modify: `hermes_cli/kanban/store_sqlite.py` (add `build_worker_context`; widen `parent_ids`/`child_ids` at ~172-176)
- Test: `tests/hermes_cli/kanban/test_store_conformance.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/hermes_cli/kanban/test_store_conformance.py`:

```python
def test_build_worker_context_basic(store):
    tid = store.create_task(title="ctx task", assignee="engineer",
                            body="do the thing")
    text = store.build_worker_context(tid)
    assert f"# Kanban task {tid}: ctx task" in text
    assert "## Closeout requirement (do not skip)" in text
    assert "## Body" in text and "do the thing" in text
    assert text.endswith("\n")


def test_build_worker_context_unknown_raises(store):
    import pytest
    with pytest.raises(ValueError):
        store.build_worker_context("t_nope")


def test_parent_ids_rollup_relation(store):
    from hermes_cli.kanban_db import LINK_RELATION_ROLLUP
    p = store.create_task(title="p", assignee="engineer")
    c = store.create_task(title="c", assignee="engineer")
    store.link_tasks(p, c, relation_type=LINK_RELATION_ROLLUP)
    assert store.parent_ids(c, relation_type=LINK_RELATION_ROLLUP) == [p]
    assert store.parent_ids(c) == []  # default dependency relation only
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -k "build_worker_context or parent_ids_rollup" -v`
Expected: FAIL — `AttributeError: 'SqliteKanbanStore' object has no attribute 'build_worker_context'` and the rollup test fails on `parent_ids(... relation_type=...)`.

- [ ] **Step 3: Add the protocol declaration**

In `hermes_cli/kanban/store.py`, change the `parent_ids`/`child_ids` declarations (lines 159-161) to:

```python
    def parent_ids(self, task_id: str, *, relation_type: Optional[str] = "dependency") -> list[str]: ...

    def child_ids(self, task_id: str, *, relation_type: Optional[str] = "dependency") -> list[str]: ...
```

And add, just after the `latest_summary` declaration (~line 185):

```python
    def build_worker_context(self, task_id: str) -> str: ...
```

- [ ] **Step 4: Implement the sqlite delegate + widen sqlite parent_ids/child_ids**

In `hermes_cli/kanban/store_sqlite.py`, change `parent_ids`/`child_ids` (~172-176) to forward the relation_type:

```python
    def parent_ids(self, task_id: str, *, relation_type: str = "dependency") -> list[str]:
        return self._read(lambda c: kb.parent_ids(c, task_id, relation_type=relation_type))

    def child_ids(self, task_id: str, *, relation_type: str = "dependency") -> list[str]:
        return self._read(lambda c: kb.child_ids(c, task_id, relation_type=relation_type))
```

And add a `build_worker_context` method (place it near `latest_summary` in the class):

```python
    def build_worker_context(self, task_id: str) -> str:
        # Byte-identical to the upstream worker-context builder. The sqlite
        # store reads through the same snapshot/connect lifecycle as every
        # other read here.
        return self._read(lambda c: kb.build_worker_context(c, task_id))
```

- [ ] **Step 5: Run tests to verify they pass (and sqlite conformance stays green)**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -v`
Expected: PASS for the sqlite param (the postgres param for `build_worker_context` will fail until Task 3 — if docker is present, use `-k "not postgres"` here and re-run fully after Task 3; if docker is absent the postgres params auto-skip).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban/store.py hermes_cli/kanban/store_sqlite.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban-pg): add KanbanStore.build_worker_context (sqlite delegate) + rollup parent/child_ids"
```

---

### Task 3: Postgres `build_worker_context` + cross-backend parity test

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py` (add `build_worker_context`; place near `latest_summary` ~line 2308)
- Test: `tests/hermes_cli/kanban/test_build_worker_context_parity.py` (create)

**Context for the implementer:** Read the upstream function `hermes_cli/kanban_db.py:8582-8828` (`build_worker_context`) in full — your Postgres method must produce a **byte-identical** string for identical data. Reuse the upstream backend-agnostic helpers/constants by **importing** them (do not reimplement, do not edit `kanban_db.py`): `_lane_type_for_assignee`, `_worker_terminal_timeout_env`, `_extract_pr_head_sha`, `_CTX_MAX_FIELD_BYTES`, `_CTX_MAX_BODY_BYTES`, `_CTX_MAX_PRIOR_ATTEMPTS`, `_CTX_MAX_COMMENTS`, `_CTX_MAX_COMMENT_BYTES`, `LINK_RELATION_DEPENDENCY`. Fetch the section data with store methods you already have (`get_task`, `list_runs`, `parent_ids`, `list_comments`); the only thing not covered by a store method is the "Recent work by @assignee" role-history (kanban_db.py:8779) — run that as one board-scoped Postgres query inside the method using `self._pool`. The `_cap` truncation helper is a nested function upstream — reproduce it locally (it's pure string logic). `_expected_parent_pr_head_sha` (kanban_db.py:3866) is reproduced from primitives: iterate `parent_ids(... dependency)`, for each `pid` take `list_runs(pid)` filtered to `outcome=='completed'` (your `Run.metadata` is already a dict), call `_extract_pr_head_sha(run.metadata)`; fall back to the parent task's `result` (only when `status=='done'`).

- [ ] **Step 1: Write the failing parity test**

Create `tests/hermes_cli/kanban/test_build_worker_context_parity.py`:

```python
"""build_worker_context must be byte-identical across sqlite and postgres."""
import time
import pytest

from hermes_cli.kanban.store_sqlite import SqliteKanbanStore


def _seed(store):
    """Seed an identical task graph with FIXED timestamps so the two backends
    render identical strftime output. Returns the child task id."""
    fixed = 1_700_000_000  # fixed epoch so timestamps render identically
    parent = store.create_task(title="parent impl", assignee="engineer",
                               body="parent body")
    # Give the parent a completed run carrying a PR-head metadata + summary.
    store.set_status_direct(parent, "running")
    rid = store.create_run(parent, profile="engineer") if hasattr(store, "create_run") else None
    # Complete the parent with structured result.
    store.complete_task(parent, summary="parent done",
                        metadata={"pull_request_head_sha": "abc123",
                                  "pr_url": "http://x", "branch_name": "b"})
    child = store.create_task(title="review child", assignee="reviewer",
                              body="review body")
    store.link_tasks(parent, child)
    store.add_comment(child, author="ops", body="please review carefully")
    return child


def _pg_store(dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    from uuid import uuid4
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    return PostgresKanbanStore(board=f"test_{uuid4().hex[:8]}", pool=pool), pool


def test_build_worker_context_parity(tmp_path, monkeypatch, _pg_dsn):
    # sqlite store
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    from hermes_cli import kanban_db as kb
    kb.connect(db_path=db, readonly=False, _bootstrap=True).close()
    s_sqlite = SqliteKanbanStore(board=None)
    # postgres store
    s_pg, pool = _pg_store(_pg_dsn)
    try:
        c1 = _seed(s_sqlite)
        c2 = _seed(s_pg)
        # Same seed logic → same ids are NOT guaranteed; compare the rendered
        # context with the task id line normalized out.
        t1 = s_sqlite.build_worker_context(c1)
        t2 = s_pg.build_worker_context(c2)
        norm = lambda s, cid: s.replace(cid, "<CHILD>")
        assert norm(t1, c1) == norm(t2, c2)
    finally:
        s_sqlite.close()
        s_pg.close()
        pool.close()
```

> Note for the implementer: if `create_task` does not let you pin `created_at`, the only timestamp-bearing lines that can differ are run/comment timestamps. Seed those via the store's run/comment APIs with controlled values, or normalize timestamp lines in the test the same way ids are normalized. Adjust `_seed` to whatever the store API actually exposes (inspect `store_sqlite.py` / `store_postgres.py` for `create_run`/run-completion helpers). The REQUIREMENT is: identical logical data → identical context string (modulo the task-id substitution).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_build_worker_context_parity.py -v`
Expected: FAIL — `AttributeError: 'PostgresKanbanStore' object has no attribute 'build_worker_context'` (or skip if docker unavailable — in that case note it and proceed; the conformance `build_worker_context_basic` postgres param is the fallback check).

- [ ] **Step 3: Implement `build_worker_context` on `PostgresKanbanStore`**

Add the method to `hermes_cli/kanban/store_postgres.py` (near `latest_summary`). It must reproduce `kanban_db.build_worker_context`'s sections in order: header, closeout block, implementation-PR-evidence (impl lanes) / final-review-PR-head-gate (review lanes), body, prior attempts, parent task results, recent-work-by-assignee, comment thread. Use imported helpers/constants, store methods for fetches, the local `_cap`, and a single board-scoped query (using `self._pool` with `dict_row`) for recent-work:

```sql
SELECT t.id, t.title, r.summary, r.ended_at
  FROM task_runs r JOIN tasks t ON r.board = t.board AND r.task_id = t.id
 WHERE r.board = %s AND r.profile = %s AND r.task_id <> %s
   AND r.outcome = 'completed'
 ORDER BY r.ended_at DESC LIMIT 5
```

Reproduce `_expected_parent_pr_head_sha` from primitives (parent_ids dependency → list_runs completed → `_extract_pr_head_sha(run.metadata)` → fallback parent `result` when `status=='done'`). Raise `ValueError(f"unknown task {task_id}")` when `get_task` is None (matches upstream).

- [ ] **Step 4: Run the parity test (and the conformance build_worker_context test) to verify they pass**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_build_worker_context_parity.py tests/hermes_cli/kanban/test_store_conformance.py -k "build_worker_context or parity" -v`
Expected: PASS (both backends). Iterate on the Postgres assembly until the parity string matches exactly.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_build_worker_context_parity.py
git commit -m "feat(kanban-pg): PostgresKanbanStore.build_worker_context (parity with sqlite)"
```

---

### Task 4: Route worker in-agent reads through the store under Postgres

**Files:**
- Modify: `tools/kanban_tools.py` (`_handle_show` ~357-437; `_handle_list` ~440-500; `_task_summary_dict` ~324-350)
- Test: `tests/tools/test_kanban_tools_pg.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_kanban_tools_pg.py` (mirror the existing kanban_tools test style; if `tests/tools/` lacks a conftest with `_pg_dsn`, import the fixture path used elsewhere or start a pool directly as in Task 3):

```python
import json
from uuid import uuid4
import pytest


def _pg_store_env(monkeypatch, dsn):
    """Point the kanban_tools store factory at a fresh PG board."""
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    return PostgresKanbanStore(board=board, pool=pool), pool, board


def test_kanban_show_reads_postgres(monkeypatch, _pg_dsn):
    from tools import kanban_tools
    store, pool, board = _pg_store_env(monkeypatch, _pg_dsn)
    try:
        tid = store.create_task(title="pg show", assignee="engineer", body="b")
        out = json.loads(kanban_tools._handle_show({"task_id": tid, "board": board}))
        assert out["task"]["id"] == tid
        assert out["task"]["title"] == "pg show"
        assert "worker_context" in out and f"# Kanban task {tid}" in out["worker_context"]
    finally:
        store.close(); pool.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/tools/test_kanban_tools_pg.py -v`
Expected: FAIL — `_handle_show` reads frozen sqlite (`task ... not found`) because the read path ignores the backend.

- [ ] **Step 3: Branch the read tools on backend**

In `tools/kanban_tools.py`, in `_handle_show`, replace the `_connect`-based fetch block with a backend branch. Under Postgres use the store; keep the sqlite branch byte-identical:

```python
    board = args.get("board")
    try:
        from hermes_cli.kanban.store import resolve_backend
        from hermes_cli.kanban_db import LINK_RELATION_ROLLUP

        def _task_dict(t):
            return {
                "id": t.id, "title": t.title, "body": t.body,
                "assignee": t.assignee, "status": t.status,
                "tenant": t.tenant, "priority": t.priority,
                "workspace_kind": t.workspace_kind,
                "workspace_path": t.workspace_path,
                "created_by": t.created_by, "created_at": t.created_at,
                "started_at": t.started_at, "completed_at": t.completed_at,
                "result": t.result, "current_run_id": t.current_run_id,
                "model_override": t.model_override,
            }

        def _run_dict(r):
            return {
                "id": r.id, "profile": r.profile, "status": r.status,
                "outcome": r.outcome, "summary": r.summary, "error": r.error,
                "metadata": r.metadata, "started_at": r.started_at,
                "ended_at": r.ended_at,
            }

        def _payload(task, comments, events, runs, parents, children,
                     rollup_parents, rollup_children, worker_context):
            return json.dumps({
                "task": _task_dict(task),
                "parents": parents, "children": children,
                "rollup_parents": rollup_parents, "rollup_children": rollup_children,
                "comments": [{"author": c.author, "body": c.body,
                              "created_at": c.created_at} for c in comments],
                "events": [{"kind": e.kind, "payload": e.payload,
                            "created_at": e.created_at, "run_id": e.run_id}
                           for e in events[-50:]],
                "runs": [_run_dict(r) for r in runs],
                "worker_context": worker_context,
            })

        if resolve_backend() == "postgres":
            store = _store(board=board)
            try:
                task = store.get_task(tid)
                if task is None:
                    return tool_error(f"task {tid} not found")
                return _payload(
                    task,
                    store.list_comments(tid),
                    store.list_events(tid),
                    store.list_runs(tid),
                    store.parent_ids(tid),
                    store.child_ids(tid),
                    store.parent_ids(tid, relation_type=LINK_RELATION_ROLLUP),
                    store.child_ids(tid, relation_type=LINK_RELATION_ROLLUP),
                    store.build_worker_context(tid),
                )
            finally:
                store.close()

        # sqlite — unchanged snapshot-conn aggregation
        _kb, conn = _connect(board=board)
        try:
            task = _kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            return _payload(
                task,
                _kb.list_comments(conn, tid),
                _kb.list_events(conn, tid),
                _kb.list_runs(conn, tid),
                _kb.parent_ids(conn, tid),
                _kb.child_ids(conn, tid),
                _kb.parent_ids(conn, tid, relation_type=_kb.LINK_RELATION_ROLLUP),
                _kb.child_ids(conn, tid, relation_type=_kb.LINK_RELATION_ROLLUP),
                _kb.build_worker_context(conn, tid),
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")
```

Then fix the `_handle_list` summary-dict path (~488-491). `_task_summary_dict` currently takes `(kb, conn, task)`. Under Postgres, the rollup/parent/child reads must come from the store. Change the summary block to branch:

```python
        truncated = len(rows) > limit
        tasks = rows[:limit]
        from hermes_cli.kanban.store import resolve_backend
        if resolve_backend() == "postgres":
            summaries = [_task_summary_dict_store(store, t) for t in tasks]
        else:
            _, conn = _connect(board=board)
            try:
                summaries = [_task_summary_dict(kb, conn, t) for t in tasks]
            finally:
                conn.close()
        return json.dumps({
            "tasks": summaries,
            "count": len(tasks), "limit": limit, "truncated": truncated,
            "next_limit": (min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                           if truncated and limit < KANBAN_LIST_MAX_LIMIT else None),
            "promoted": promoted,
        })
```

(Note: the existing code closes `store` in a `finally` BEFORE building summaries; move `store.close()` to after the summary build for the Postgres branch, or reopen via `_store(board=board)` inside `_task_summary_dict_store`. Simplest: keep one `store` open for the whole `_handle_list` body and close it in an outer `finally`.) Add the helper next to `_task_summary_dict`:

```python
def _task_summary_dict_store(store, task) -> dict:
    """Compact task shape for board-listing tools (backend-agnostic via store)."""
    from hermes_cli.kanban_db import LINK_RELATION_ROLLUP
    parents = store.parent_ids(task.id)
    children = store.child_ids(task.id)
    rollup_children = store.child_ids(task.id, relation_type=LINK_RELATION_ROLLUP)
    return {
        "id": task.id, "title": task.title, "assignee": task.assignee,
        "status": task.status, "priority": task.priority, "tenant": task.tenant,
        "workspace_kind": task.workspace_kind, "workspace_path": task.workspace_path,
        "created_by": task.created_by, "created_at": task.created_at,
        "started_at": task.started_at, "completed_at": task.completed_at,
        "current_run_id": task.current_run_id, "model_override": task.model_override,
        "parents": parents, "children": children, "rollup_children": rollup_children,
        "parent_count": len(parents), "child_count": len(children),
        "rollup_child_count": len(rollup_children),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/tools/test_kanban_tools_pg.py -v`
Expected: PASS. Then run the existing kanban_tools test module to confirm the sqlite path is unchanged: `... -m pytest tests/tools/ -k kanban -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/kanban_tools.py tests/tools/test_kanban_tools_pg.py
git commit -m "feat(kanban-pg): worker kanban_show/list read the Postgres store under backend=postgres"
```

---

### Task 5: Gateway exports backend + DSN into its process env at startup

**Files:**
- Modify: `gateway/run.py` (add `_export_kanban_backend_env()` + call it just before `self._start_kanban_writer_daemon()` at ~line 5088)
- Test: `tests/gateway/test_kanban_backend_env_export.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/gateway/test_kanban_backend_env_export.py`:

```python
def test_export_kanban_backend_env_sets_env(monkeypatch):
    import gateway.run as run_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PG_DSN", raising=False)
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("postgres", "postgresql://u:p@h:6543/db"))
    run_mod._export_kanban_backend_env()
    import os
    assert os.environ["HERMES_KANBAN_BACKEND"] == "postgres"
    assert os.environ["HERMES_KANBAN_PG_DSN"] == "postgresql://u:p@h:6543/db"


def test_export_noop_when_sqlite(monkeypatch):
    import os
    import gateway.run as run_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("sqlite", None))
    run_mod._export_kanban_backend_env()
    assert os.environ.get("HERMES_KANBAN_BACKEND") in (None, "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/gateway/test_kanban_backend_env_export.py -v`
Expected: FAIL — `AttributeError: module 'gateway.run' has no attribute '_export_kanban_backend_env'`.

- [ ] **Step 3: Implement the helper + call site**

Add to `gateway/run.py` (module level, near the other helpers):

```python
def _resolve_kanban_backend_for_export():
    """Return (backend, dsn|None) from the gateway's (root) config without
    raising. dsn is None for non-postgres backends or if resolution fails."""
    try:
        from hermes_cli.kanban.store import resolve_backend
        backend = resolve_backend()
    except Exception:
        return ("sqlite", None)
    if backend != "postgres":
        return (backend, None)
    try:
        from hermes_cli.kanban.pg_pool import resolve_dsn
        return ("postgres", resolve_dsn())
    except Exception:
        return ("postgres", None)


def _export_kanban_backend_env() -> None:
    """Propagate the live kanban backend + DSN into os.environ so dispatcher-
    spawned workers (which run under a profile-scoped HERMES_HOME whose config
    may not carry the backend) inherit it via `env = dict(os.environ)`. The DSN
    value is never logged."""
    backend, dsn = _resolve_kanban_backend_for_export()
    if backend != "postgres":
        return
    os.environ["HERMES_KANBAN_BACKEND"] = "postgres"
    if dsn and not os.environ.get("HERMES_KANBAN_PG_DSN"):
        os.environ["HERMES_KANBAN_PG_DSN"] = dsn
    logger.info("kanban backend propagated to worker env: postgres "
                "(DSN %s)", "present" if dsn else "MISSING")
```

Then add the call just before line 5088 (`self._start_kanban_writer_daemon()`):

```python
        # Propagate the live kanban backend + DSN to spawned workers BEFORE any
        # writer/dispatcher starts, so worker in-agent kanban tools resolve the
        # same backend the gateway uses (not the profile-config default).
        _export_kanban_backend_env()

        # Start the single-writer daemon(s) BEFORE the dispatcher tick runs ...
        self._start_kanban_writer_daemon()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/gateway/test_kanban_backend_env_export.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_kanban_backend_env_export.py
git commit -m "feat(kanban-pg): gateway propagates backend+DSN to spawned workers via env"
```

---

### Task 6: CLI `main()` skips the sqlite `init_db` preamble under Postgres

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (the `init_db` preamble at ~line 1372-1379)
- Test: `tests/hermes_cli/kanban/test_cli_pg_init.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/kanban/test_cli_pg_init.py`:

```python
def test_main_skips_sqlite_init_db_under_postgres(monkeypatch):
    """Under backend=postgres, main() must not call the sqlite kb.init_db()."""
    import hermes_cli.kanban.cli as cli
    monkeypatch.setattr(cli, "resolve_backend", lambda: "postgres", raising=False)
    called = {"init": False}
    monkeypatch.setattr(cli.kb, "init_db", lambda *a, **k: called.__setitem__("init", True))
    # `list` is non-observational → previously always triggered init_db.
    # Stub the handler so we only exercise the preamble.
    monkeypatch.setitem(
        cli.__dict__.setdefault("_TEST_HANDLER_OVERRIDE", {}), "list", lambda args: 0)
    rc = cli.main(["list"])
    assert called["init"] is False
```

> Implementer note: if `resolve_backend` is not already imported at module scope in `cli.py`, import it (`from hermes_cli.kanban.store import resolve_backend`) so the monkeypatch target exists, and so the preamble can call it. If a `_TEST_HANDLER_OVERRIDE` shim doesn't fit the existing `main()` structure, instead assert via a Postgres-backed `_pg_dsn` integration test that `cli.main(["list"])` returns 0 without the `could not initialize database` stderr — pick whichever matches the real `main()` signature you find.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_pg_init.py -v`
Expected: FAIL — `init_db` is called (preamble is unconditional for non-observational actions).

- [ ] **Step 3: Guard the preamble**

In `hermes_cli/kanban/cli.py`, ensure `resolve_backend` is imported at module scope. Change the preamble (lines 1372-1379) to:

```python
    observational_actions = {"doctor", "reconcile", "metrics", "repair-db"}
    # Postgres ensures its schema via the store (pg_pool.ensure_schema); the
    # sqlite init_db() opens a writable sqlite connection and would trip the
    # single-writer guard under backend=postgres. Skip it for Postgres.
    if action not in observational_actions and resolve_backend() != "postgres":
        try:
            kb.init_db()
        except Exception as exc:
            print(f"kanban: could not initialize database: {exc}", file=sys.stderr)
            _restore_board_env()
            return 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_pg_init.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/kanban/test_cli_pg_init.py
git commit -m "feat(kanban-pg): CLI main() skips sqlite init_db under backend=postgres"
```

---

### Task 7: CLI `_cmd_show` routes through the store under Postgres

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_show`, ~line 2030; the `with kb.connect_closing() as conn:` fetch block)
- Test: `tests/hermes_cli/kanban/test_cli_show_pg.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/kanban/test_cli_show_pg.py`:

```python
import json
from uuid import uuid4


def test_cmd_show_json_reads_postgres(monkeypatch, capsys, _pg_dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    import hermes_cli.kanban.cli as cli
    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=board, pool=pool)
    try:
        tid = store.create_task(title="pg cli show", assignee="engineer", body="b")
        monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
        monkeypatch.setenv("HERMES_KANBAN_PG_DSN", _pg_dsn)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
        rc = cli.main(["show", tid, "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["task"]["id"] == tid and out["task"]["title"] == "pg cli show"
    finally:
        store.close(); pool.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_show_pg.py -v`
Expected: FAIL — either the sqlite init error (until Task 6 lands; it does) or "no such task" (reads frozen sqlite / empty test sqlite).

- [ ] **Step 3: Branch the `_cmd_show` fetch**

In `hermes_cli/kanban/cli.py` `_cmd_show`, replace the `with kb.connect_closing() as conn:` fetch block so that under Postgres the same local variables (`task`, `comments`, `events`, `parents`, `children`, `latest_summary`, `rollup_parents`, `rollup_children`, `runs`) are populated from `_make_store()`; leave the sqlite block byte-identical. The display logic below (JSON + human print, diagnostics) is unchanged because it operates on those locals:

```python
    if resolve_backend() == "postgres":
        store = _make_store()
        try:
            task = store.get_task(args.task_id)
            if not task:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            comments = store.list_comments(args.task_id)
            events = store.list_events(args.task_id)
            parents = store.parent_ids(args.task_id)
            children = store.child_ids(args.task_id)
            latest_summary = store.latest_summary(args.task_id)
            rollup_parents = store.parent_ids(args.task_id, relation_type=kb.LINK_RELATION_ROLLUP)
            rollup_children = store.child_ids(args.task_id, relation_type=kb.LINK_RELATION_ROLLUP)
            runs = store.list_runs(args.task_id, **rsk)
        finally:
            store.close()
    else:
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, args.task_id)
            if not task:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            comments = kb.list_comments(conn, args.task_id)
            events = kb.list_events(conn, args.task_id)
            parents = kb.parent_ids(conn, args.task_id)
            children = kb.child_ids(conn, args.task_id)
            latest_summary = kb.latest_summary(conn, args.task_id)
            rollup_parents = kb.parent_ids(conn, args.task_id, relation_type=kb.LINK_RELATION_ROLLUP)
            rollup_children = kb.child_ids(conn, args.task_id, relation_type=kb.LINK_RELATION_ROLLUP)
            runs = kb.list_runs(conn, args.task_id, **rsk)
```

> Implementer note: confirm `list_runs(**rsk)` is accepted by the store (the sqlite/PG `list_runs` signatures accept the `state_type`/`state_name` kwargs `rsk` provides, or `rsk` is empty `{}`); if the store's `list_runs` doesn't accept those kwargs, pass them only when non-empty. Keep the JSON payload + human-print blocks below exactly as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_show_pg.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/kanban/test_cli_show_pg.py
git commit -m "feat(kanban-pg): hermes kanban show reads the Postgres store under backend=postgres"
```

---

### Task 8: CLI `_cmd_liveness` + `_cmd_context` route through Postgres

**Files:**
- Modify: `hermes_cli/kanban/cli.py` (`_cmd_liveness` ~1497-1521; `_cmd_context` ~3524-3530)
- Test: `tests/hermes_cli/kanban/test_cli_liveness_context_pg.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/kanban/test_cli_liveness_context_pg.py`:

```python
from uuid import uuid4


def _pg_board(monkeypatch, dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn); pg_pool.ensure_schema(pool)
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    return PostgresKanbanStore(board=board, pool=pool), pool, board


def test_cmd_liveness_pg(monkeypatch, capsys, _pg_dsn):
    import hermes_cli.kanban.cli as cli
    store, pool, board = _pg_board(monkeypatch, _pg_dsn)
    try:
        store.create_task(title="x", assignee="engineer")
        rc = cli.main(["liveness", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "oldest_ready_age_seconds" in out
    finally:
        store.close(); pool.close()


def test_cmd_context_pg(monkeypatch, capsys, _pg_dsn):
    import hermes_cli.kanban.cli as cli
    store, pool, board = _pg_board(monkeypatch, _pg_dsn)
    try:
        tid = store.create_task(title="ctx", assignee="engineer", body="b")
        rc = cli.main(["context", tid])
        assert rc == 0
        assert f"# Kanban task {tid}" in capsys.readouterr().out
    finally:
        store.close(); pool.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_liveness_context_pg.py -v`
Expected: FAIL — `_cmd_liveness` opens sqlite (`could not initialize database` is now skipped by Task 6, but the handler still calls `kb.connect`), `_cmd_context` calls `kb.build_worker_context(conn, ...)` against empty/sqlite.

- [ ] **Step 3: Branch both handlers**

`_cmd_liveness` (mirror the gateway loop): under Postgres, compute via `compute_board_liveness_pg`:

```python
def _cmd_liveness(args: argparse.Namespace) -> int:
    import dataclasses
    from hermes_cli import kanban_liveness as kliv
    board = getattr(args, "board", None)
    if resolve_backend() == "postgres":
        from hermes_cli.kanban import pg_pool
        from psycopg.rows import dict_row
        slug = board or kb.get_current_board()
        with pg_pool.get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            snap = kliv.compute_board_liveness_pg(cur, slug, now=int(time.time()))
    else:
        path = kb.kanban_db_path(board=board)
        if path.exists():
            conn = kb.connect(board=board, readonly=True)
            try:
                snap = kliv.compute_board_liveness(conn, now=int(time.time()))
            finally:
                conn.close()
        else:
            snap = kliv.Liveness()
    data = dataclasses.asdict(snap)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        for key, value in data.items():
            if key == "extra":
                continue
            print(f"{key}: {value}")
    return 0
```

`_cmd_context` (line ~3524): branch the `kb.build_worker_context(conn, ...)` call:

```python
    if resolve_backend() == "postgres":
        store = _make_store()
        try:
            text = store.build_worker_context(args.task_id)
        finally:
            store.close()
    else:
        with kb.connect() as conn:
            text = kb.build_worker_context(conn, args.task_id)
```

> Implementer note: read `_cmd_context`'s actual body (~3524-3530) and match its existing connection idiom and output/printing for the sqlite branch exactly. Confirm `compute_board_liveness_pg`'s signature `(cur, board, *, now)` against `hermes_cli/kanban_liveness.py` (added in the doctor/liveness phase).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_cli_liveness_context_pg.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/cli.py tests/hermes_cli/kanban/test_cli_liveness_context_pg.py
git commit -m "feat(kanban-pg): hermes kanban liveness/context read Postgres under backend=postgres"
```

---

### Task 9: Full-suite regression + sqlite byte-identical confirmation

**Files:** none (verification only)

- [ ] **Step 1: Run the kanban + worker-tool + gateway suites**

Run:
```
cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/hermes_cli/kanban/ tests/tools/ -k "kanban or store or liveness or context or backend" \
  tests/gateway/test_kanban_backend_env_export.py -q
```
Expected: all PASS (sqlite + docker-PG params); no regressions in the existing sqlite conformance/CLI/worker-tool tests.

- [ ] **Step 2: Confirm `kanban_db.py` untouched**

Run: `cd <worktree> && git diff --stat main -- hermes_cli/kanban_db.py`
Expected: **empty** (no changes to the upstream file).

- [ ] **Step 3: Confirm no DSN literal landed in source**

Run: `cd <worktree> && git diff main | grep -iE "supabase|pooler\.supabase|postgresql://postgres" || echo "clean"`
Expected: `clean` (no DSN/password in the diff).

---

## Self-Review

**Spec coverage:**
- Layer 1 propagation → Task 1 (resolve_backend env) + Task 5 (gateway export). ✓
- Layer 2 worker reads → Task 4 (`_handle_show`/`_handle_list`). ✓
- Layer 3 CLI → Task 6 (init_db preamble) + Task 7 (`show`) + Task 8 (`liveness`/`context`). ✓
- Store-primitive `build_worker_context` → Task 2 (protocol+sqlite) + Task 3 (PG+parity). ✓
- Hard boundaries (kanban_db.py untouched, sqlite byte-identical, no secret leak) → Task 9 verification + every task's sqlite-branch preservation. ✓
- Testing requirements (parity, propagation, CLI-under-PG, regression) → Tasks 1-9. ✓

**Type/name consistency:** `resolve_backend()` (store.py) used in cli.py, kanban_tools.py, gateway/run.py; `build_worker_context(task_id)` declared in protocol (Task 2), implemented sqlite (Task 2) + PG (Task 3), consumed in kanban_tools (Task 4) + cli `_cmd_context` (Task 8); `compute_board_liveness_pg(cur, board, *, now)` reused in Task 8 from the prior phase; `_make_store()` (cli.py) vs `_store()` (kanban_tools.py) are the per-module factory names (both → `kanban_store(board)`), used consistently within their files.

**Placeholder scan:** no TBD/TODO; every code step shows the code; the two genuinely test-driven spots (PG `build_worker_context` assembly, parity-test seeding) carry explicit implementer notes plus the upstream line references and the parity test as the executable spec.
