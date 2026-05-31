# Kanban dashboard read-path → Postgres — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `kanban.backend=postgres`, the kanban dashboard plugin API (`plugins/kanban/dashboard/plugin_api.py`) reads and resolves the live Postgres board for every browser-facing read, and its create/PATCH/bulk writes land in Postgres. Under `sqlite` it behaves byte-identically to today.

**Architecture:** A new fork-owned module `plugins/kanban/dashboard/pg_reads.py` holds the Postgres translation of the dashboard's direct-sqlite aggregate/tail/diagnostic reads (board-scoped SQL via `pg_pool.get_pool()` + `dict_row`, mirroring `kanban_board_doctor._run_board_doctor_pg`). First-class entity reads reuse the existing backend-aware `_store()`. Each browser-facing handler in `plugin_api.py` branches `if resolve_backend()=="postgres": <new> else: <existing-verbatim>`.

**Tech Stack:** Python, FastAPI, psycopg3 + psycopg_pool, pytest. `hermes_cli/kanban_db.py` and `hermes_cli/kanban_liveness.py` are upstream and **must not be edited**. Test interpreter (only this venv has psycopg + pytest): `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Postgres tests use a docker `postgres:16-alpine` fixture / `HERMES_PG_TEST_DSN` and auto-skip if neither is available.

**Hard boundaries (apply to EVERY task):**
- `hermes_cli/kanban_db.py` + `hermes_cli/kanban_liveness.py` are **not edited**. Their helpers/constants/dataclasses may be **imported** and reused.
- The **sqlite path stays byte-identical**. Every change is `if resolve_backend()=="postgres": <new> else: <existing-verbatim>`. The existing sqlite handler body is wrapped unchanged in the `else:`.
- **No secret leakage**: `pg_reads` uses `pg_pool.get_pool()` and never sees the DSN literal; PG failures log the target redacted to `host:port/db` (no password). Never print/commit the DSN.
- Default backend in code/tests stays `sqlite`.

**Verbatim facts (from the codebase, for reference in every task):**
- PG tables all have a `board TEXT` column. Columns: `tasks(board,id,title,body,assignee,status,priority,created_by,created_at,started_at,completed_at,workspace_kind,workspace_path,branch_name,claim_lock,claim_expires,tenant,result,idempotency_key,consecutive_failures,worker_pid,last_failure_error,max_runtime_seconds,last_heartbeat_at,current_run_id,workflow_template_id,current_step_key,skills,model_override,max_retries,session_id)`; `task_links(board,parent_id,child_id,relation_type)`; `task_comments(id,board,task_id,author,body,created_at)`; `task_events(id,board,task_id,run_id,kind,payload JSONB,created_at)`; `task_runs(id,board,task_id,profile,step_key,status,claim_lock,claim_expires,worker_pid,max_runtime_seconds,last_heartbeat_at,started_at,ended_at,outcome,summary,metadata JSONB,error)`; `kanban_profile_event_subs(board,task_id,profile,name,event_kinds,include_children,wake_agent,wake_prompt,created_at,last_event_id,last_wake_at,last_wake_error_at,last_wake_error,wake_failure_count,enabled)`.
- `PostgresKanbanStore.list_tasks` defaults to `ORDER BY priority DESC, created_at ASC` (identical to sqlite); supports filters `tenant`/`include_archived`/`workflow_template_id`/`current_step_key`/`assignee`/`status`/`session_id`/`limit`; `order_by=` raises `NotImplementedError`.
- `PostgresKanbanStore.list_runs(state_type=, state_name=)` raises `NotImplementedError`.
- `kanban_diagnostics.compute_task_diagnostics(task, events, runs, *, now=None, config=None)` is backend-agnostic; reads task keys `id,status,assignee,claim_lock,consecutive_failures,last_failure_error,created_at`, event keys `kind,created_at,payload` (dict or JSON str), run keys `id,outcome,error`. A `SELECT *` PG dict-row row supplies all of these (JSONB payload arrives as a dict).
- Dataclasses returned by the store: `Task`, `Run`, `Comment`, `Event` (field names exactly match what `_task_dict`/`_run_dict`/`_comment_dict`/`_event_dict` read; `_task_dict` also calls the backend-agnostic `kanban_db.task_age(task)`).
- `pg_pool.get_pool()` returns a cached pool (DSN from `HERMES_KANBAN_PG_DSN` env → config `kanban.postgres.dsn`); idiom `with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:`.
- Notifier heartbeats live in a backend-agnostic **sidecar** — `kanban_db.list_notifier_heartbeats(conn, ...)` ignores `conn` and reads the sidecar; `store.list_notifier_heartbeats(**kw)` delegates to it. So the PG path fetches heartbeats via the store (no new SQL).
- `plugins/kanban/dashboard/` is **not** a Python package; `plugin_api.py` is loaded by path (`spec_from_file_location`). `pg_reads.py` must be loaded the same way (Task 2 adds a `_pg_reads()` loader).

---

### Task 1: PG test harness — `tests/plugins/conftest.py` + PG client smoke test

Establishes the Postgres-backed dashboard TestClient before any `pg_reads` exists. The smoke test exercises `GET /stats` (already routed through the backend-aware `_store()`), proving the harness resolves Postgres end-to-end.

**Files:**
- Create: `tests/plugins/conftest.py`
- Create: `tests/plugins/test_kanban_dashboard_plugin_pg.py`

- [ ] **Step 1: Write the conftest fixtures**

Create `tests/plugins/conftest.py`:

```python
"""Postgres fixtures for the kanban dashboard plugin tests.

The store conformance suite's _pg_dsn lives under tests/hermes_cli/kanban/ and
is not visible here, so we provide an equivalent: HERMES_PG_TEST_DSN if set,
else a throwaway docker postgres:16-alpine container.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _pg_dsn():
    dsn = os.environ.get("HERMES_PG_TEST_DSN")
    if dsn:
        yield dsn
        return
    if not shutil.which("docker"):
        pytest.skip("docker not available and HERMES_PG_TEST_DSN unset")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
    except Exception:
        pytest.skip("docker not usable and HERMES_PG_TEST_DSN unset")
    name = f"hermes-kanban-dashpgtest-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name,
             "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=kanban",
             "-P", "postgres:16-alpine"],
            check=True, capture_output=True, timeout=120,
        )
        out = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        port = int(out.rsplit(":", 1)[-1])
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/kanban"
        import psycopg
        waited = 0
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3):
                    break
            except Exception:
                if waited >= 60:
                    raise
                time.sleep(1.0)
                waited += 1
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def _load_plugin_router():
    """Load plugins/kanban/dashboard/plugin_api.py by path (mirrors production)."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def pg_board(monkeypatch, _pg_dsn):
    """A clean Postgres 'default' board + a PostgresKanbanStore bound to it.

    Production is single-board 'default'; the dashboard's board=None path
    resolves to 'default', so tests seed and read 'default'. Rows for
    board='default' are deleted up-front for isolation across tests.
    """
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        for tbl in ("task_events", "task_comments", "task_runs", "task_links",
                    "kanban_profile_wake_events", "kanban_profile_event_subs", "tasks"):
            cur.execute(f"DELETE FROM {tbl} WHERE board=%s", ("default",))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", _pg_dsn)
    store = PostgresKanbanStore(board="default", pool=pool)
    try:
        yield store
    finally:
        store.close()
        pool.close()


@pytest.fixture
def pg_client(pg_board, tmp_path, monkeypatch):
    """A TestClient whose dashboard resolves backend=postgres (board 'default')."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    # HERMES_HOME isolation so any incidental sqlite path is a throwaway tmp dir.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    client = TestClient(app)
    client.pg_store = pg_board  # convenience handle for seeding
    return client
```

- [ ] **Step 2: Write the smoke test**

Create `tests/plugins/test_kanban_dashboard_plugin_pg.py`:

```python
"""Dashboard plugin API on the Postgres backend."""
import json


def test_pg_client_stats_resolves_postgres(pg_client):
    # /stats routes through the backend-aware _store(); a clean PG 'default'
    # board returns zeroed counts (proves the harness resolves Postgres).
    r = pg_client.get("/api/plugins/kanban/stats")
    assert r.status_code == 200
    body = r.json()
    assert "by_status" in body
    assert sum(body["by_status"].values()) == 0
```

- [ ] **Step 3: Run the smoke test**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -v`
Expected: PASS (or skip if no docker/DSN).

- [ ] **Step 4: Commit**

```bash
git add tests/plugins/conftest.py tests/plugins/test_kanban_dashboard_plugin_pg.py
git commit -m "test(kanban-pg): PG-backed dashboard TestClient harness + stats smoke"
```

---

### Task 2: `pg_reads.py` foundation + board-aggregate reads

Create the module + the `_pg_reads()` loader in `plugin_api.py`, plus the simple board-scoped aggregates used by `GET /board`. All are unit-tested directly against PG.

**Files:**
- Create: `plugins/kanban/dashboard/pg_reads.py`
- Modify: `plugins/kanban/dashboard/plugin_api.py` (add `_pg_reads()` loader near `_store`, ~line 248)
- Test: `tests/plugins/test_pg_reads.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/plugins/test_pg_reads.py`:

```python
"""Unit tests for the dashboard Postgres read helpers."""
import importlib.util
from pathlib import Path


def _load_pg_reads():
    repo_root = Path(__file__).resolve().parents[2]
    f = repo_root / "plugins" / "kanban" / "dashboard" / "pg_reads.py"
    spec = importlib.util.spec_from_file_location("kanban_dash_pg_reads_test", f)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_board_aggregates(pg_board, monkeypatch):
    pg = _load_pg_reads()
    s = pg_board
    p = s.create_task(title="parent", assignee="engineer", body="b", tenant="acme")
    c1 = s.create_task(title="c1", assignee="reviewer")
    c2 = s.create_task(title="c2", assignee="engineer")
    s.link_tasks(p, c1)
    s.link_tasks(p, c2)
    s.complete_task(c1, summary="done")  # c1 -> done
    s.add_comment(p, author="ops", body="hi")
    s.add_comment(p, author="ops", body="again")

    assert pg.comment_counts("default").get(p) == 2
    lc = pg.link_counts("default")
    assert lc.get(p, {}).get("children") == 2
    assert lc.get(c1, {}).get("parents") == 1
    prog = pg.child_progress("default")
    assert prog.get(p) == {"done": 1, "total": 2}
    assert "acme" in pg.distinct_tenants("default")
    assert set(pg.distinct_assignees("default")) >= {"engineer", "reviewer"}
    assert pg.latest_event_id("default") > 0
    assert pg.board_counts("default").get("done") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_pg_reads.py::test_board_aggregates -v`
Expected: FAIL (`pg_reads.py` does not exist / `ModuleNotFoundError` in the loader).

- [ ] **Step 3: Create `pg_reads.py` with the foundation + board aggregates**

```python
"""Postgres read helpers for the kanban dashboard plugin.

Mirrors the dashboard's direct-sqlite aggregate/tail/diagnostic reads as
board-scoped Postgres SQL, following kanban_board_doctor._run_board_doctor_pg
(pg_pool.get_pool() + dict_row, WHERE board=%s). Used only on the
resolve_backend()=="postgres" branches in plugin_api.py; the sqlite path is
untouched. The DSN is never logged.
"""
from __future__ import annotations

from typing import Any, Optional

from hermes_cli import kanban_db


def slug(board: Optional[str]) -> str:
    """Resolve a board query-param to a normalised slug (defaults to current)."""
    s = board or kanban_db.get_current_board()
    try:
        return kanban_db._normalize_board_slug(s) or kanban_db.DEFAULT_BOARD
    except Exception:
        return kanban_db.DEFAULT_BOARD


def _pool():
    from hermes_cli.kanban import pg_pool
    return pg_pool.get_pool()


def _query(sql: str, params: tuple) -> list[dict]:
    from psycopg.rows import dict_row
    with _pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def link_counts(board: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in _query("SELECT parent_id, child_id FROM task_links WHERE board=%s", (board,)):
        out.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        out.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
    return out


def comment_counts(board: str) -> dict[str, int]:
    rows = _query(
        "SELECT task_id, COUNT(*) AS n FROM task_comments WHERE board=%s GROUP BY task_id",
        (board,),
    )
    return {r["task_id"]: int(r["n"]) for r in rows}


def child_progress(board: str) -> dict[str, dict[str, int]]:
    progress: dict[str, dict[str, int]] = {}
    rows = _query(
        "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l "
        "JOIN tasks t ON t.board = l.board AND t.id = l.child_id WHERE l.board=%s",
        (board,),
    )
    for row in rows:
        p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
        p["total"] += 1
        if row["cstatus"] == "done":
            p["done"] += 1
    return progress


def distinct_tenants(board: str) -> list[str]:
    rows = _query(
        "SELECT DISTINCT tenant FROM tasks WHERE board=%s AND tenant IS NOT NULL "
        "ORDER BY tenant", (board,),
    )
    return [r["tenant"] for r in rows]


def distinct_assignees(board: str) -> list[str]:
    rows = _query(
        "SELECT DISTINCT assignee FROM tasks WHERE board=%s AND assignee IS NOT NULL "
        "AND status != 'archived' ORDER BY assignee", (board,),
    )
    return [r["assignee"] for r in rows]


def latest_event_id(board: str) -> int:
    rows = _query("SELECT COALESCE(MAX(id), 0) AS m FROM task_events WHERE board=%s", (board,))
    return int(rows[0]["m"]) if rows else 0


def board_counts(board: str) -> dict[str, int]:
    rows = _query(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE board=%s GROUP BY status", (board,),
    )
    return {r["status"]: int(r["n"]) for r in rows}
```

- [ ] **Step 4: Add the `_pg_reads()` loader to `plugin_api.py`**

In `plugins/kanban/dashboard/plugin_api.py`, just after the `_store()` function (~line 248), add:

```python
def _pg_reads():
    """Lazily load the sibling pg_reads.py by path.

    plugin_api.py is loaded via spec_from_file_location (no package context)
    and plugins/kanban/dashboard is not a package, so a normal package import
    of pg_reads is unavailable. Load it the same way, cached in sys.modules.
    """
    import importlib.util
    import os
    import sys
    name = "hermes_dashboard_plugin_kanban_pg_reads"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pg_reads.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _backend() -> str:
    """resolve_backend() with a defensive sqlite fallback (never raises)."""
    try:
        from hermes_cli.kanban.store import resolve_backend
        return resolve_backend()
    except Exception:
        return "sqlite"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_pg_reads.py::test_board_aggregates -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add plugins/kanban/dashboard/pg_reads.py plugins/kanban/dashboard/plugin_api.py tests/plugins/test_pg_reads.py
git commit -m "feat(kanban-pg): dashboard pg_reads board aggregates + _pg_reads loader"
```

---

### Task 3: `pg_reads` event tail + active workers + blocking-parents

**Files:**
- Modify: `plugins/kanban/dashboard/pg_reads.py`
- Test: `tests/plugins/test_pg_reads.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/plugins/test_pg_reads.py`:

```python
def test_events_since_active_workers_blocking(pg_board):
    pg = _load_pg_reads()
    s = pg_board
    p = s.create_task(title="parent", assignee="engineer")
    c = s.create_task(title="child", assignee="reviewer")
    s.link_tasks(p, c)  # c depends on p (p not done) -> p blocks c

    cursor, events = pg.events_since("default", 0, 200)
    assert cursor > 0
    assert all(isinstance(e["payload"], (dict, type(None))) for e in events)
    assert {e["task_id"] for e in events} >= {p, c}
    # incremental: nothing new past the cursor
    cursor2, events2 = pg.events_since("default", cursor, 200)
    assert events2 == [] and cursor2 == cursor

    blockers = pg.parents_blocking_ready("default", c)
    assert [b["id"] for b in blockers] == [p]
    assert blockers[0]["status"] != "done"

    assert pg.active_workers("default") == []  # nothing running/claimed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_pg_reads.py::test_events_since_active_workers_blocking -v`
Expected: FAIL (`AttributeError: module ... has no attribute 'events_since'`).

- [ ] **Step 3: Add the helpers**

Append to `plugins/kanban/dashboard/pg_reads.py`:

```python
def events_since(board: str, since_id: int, limit: int = 200) -> tuple[int, list[dict]]:
    """Return (new_cursor, events) for the /events tail. payload is a dict
    (JSONB) — already parsed, unlike the sqlite path which json.loads a TEXT col."""
    rows = _query(
        "SELECT id, task_id, run_id, kind, payload, created_at FROM task_events "
        "WHERE board=%s AND id > %s ORDER BY id ASC LIMIT %s",
        (board, int(since_id), int(limit)),
    )
    out: list[dict] = []
    new_cursor = int(since_id)
    for r in rows:
        out.append({
            "id": r["id"], "task_id": r["task_id"], "run_id": r["run_id"],
            "kind": r["kind"], "payload": r["payload"], "created_at": r["created_at"],
        })
        new_cursor = int(r["id"])
    return new_cursor, out


def active_workers(board: str) -> list[dict]:
    """Running workers: task_runs with no ended_at + a worker_pid, whose task
    is 'running'. Same shape and ORDER as the sqlite /workers/active query."""
    rows = _query(
        "SELECT r.id AS run_id, r.task_id, t.title AS task_title, t.status AS task_status, "
        "       t.assignee AS task_assignee, r.profile, r.worker_pid, r.started_at, "
        "       r.claim_lock, r.claim_expires, r.last_heartbeat_at, r.max_runtime_seconds "
        "FROM task_runs r JOIN tasks t ON t.board = r.board AND t.id = r.task_id "
        "WHERE r.board=%s AND r.ended_at IS NULL AND r.worker_pid IS NOT NULL "
        "  AND t.status = 'running' ORDER BY r.started_at ASC",
        (board,),
    )
    return [
        {
            "run_id": r["run_id"], "task_id": r["task_id"], "task_title": r["task_title"],
            "task_status": r["task_status"], "task_assignee": r["task_assignee"],
            "profile": r["profile"], "worker_pid": r["worker_pid"],
            "started_at": r["started_at"], "claim_lock": r["claim_lock"],
            "claim_expires": r["claim_expires"], "last_heartbeat_at": r["last_heartbeat_at"],
            "max_runtime_seconds": r["max_runtime_seconds"],
        }
        for r in rows
    ]


def parents_blocking_ready(board: str, task_id: str) -> list[dict]:
    """Parent rows (id,title,status) not yet 'done' — blocks ready promotion."""
    rows = _query(
        "SELECT t.id, t.title, t.status FROM tasks t "
        "JOIN task_links l ON l.board = t.board AND l.parent_id = t.id "
        "WHERE t.board=%s AND l.child_id = %s AND t.status != 'done'",
        (board, task_id),
    )
    return [{"id": r["id"], "title": r["title"], "status": r["status"]} for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_pg_reads.py::test_events_since_active_workers_blocking -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/pg_reads.py tests/plugins/test_pg_reads.py
git commit -m "feat(kanban-pg): pg_reads events_since + active_workers + parents_blocking_ready"
```

---

### Task 4: `pg_reads` diagnostics rows + wake-health

**Files:**
- Modify: `plugins/kanban/dashboard/pg_reads.py`
- Test: `tests/plugins/test_pg_reads.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/plugins/test_pg_reads.py`:

```python
def test_diagnostics_rows_and_wake_health(pg_board):
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config
    pg = _load_pg_reads()
    s = pg_board
    t = s.create_task(title="t", assignee="engineer")
    s.add_profile_event_sub(task_id=t, profile="engineer", name="", wake_agent=True)

    task_rows, events_by, runs_by = pg.diagnostics_rows("default")
    assert any(r["id"] == t for r in task_rows)
    # rows are dict-shaped with the keys the engine reads
    row = next(r for r in task_rows if r["id"] == t)
    for k in ("id", "status", "assignee", "consecutive_failures", "last_failure_error", "created_at"):
        assert k in row
    # engine consumes them without error
    cfg = kd.config_from_runtime_config(load_config())
    diags = kd.compute_task_diagnostics(row, events_by.get(t, []), runs_by.get(t, []), config=cfg)
    assert isinstance(diags, list)

    wh = pg.wake_health("default", [t])
    assert wh["subscription_count"] == 1
    rows, overflow = pg.wake_health_rows("default", [t], {t: s.get_task(t)}, 50)
    assert isinstance(rows, list) and overflow == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_pg_reads.py::test_diagnostics_rows_and_wake_health -v`
Expected: FAIL (`AttributeError: ... 'diagnostics_rows'`).

- [ ] **Step 3: Add the helpers**

Append to `plugins/kanban/dashboard/pg_reads.py`:

```python
def diagnostics_rows(
    board: str, task_ids: Optional[list[str]] = None,
) -> tuple[list[dict], dict[str, list], dict[str, list]]:
    """Fetch task/event/run dict rows for the diagnostics engine (which is
    backend-agnostic and reads rows by column name). Mirrors the sqlite
    _compute_task_diagnostics 3-query structure. JSONB payload/metadata arrive
    as dicts — the engine handles dict payloads."""
    from psycopg.rows import dict_row
    with _pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if task_ids is not None:
            if not task_ids:
                return [], {}, {}
            cur.execute(
                "SELECT * FROM tasks WHERE board=%s AND id = ANY(%s)", (board, list(task_ids)),
            )
        else:
            cur.execute(
                "SELECT * FROM tasks WHERE board=%s AND status != 'archived'", (board,),
            )
        task_rows = cur.fetchall()
        if not task_rows:
            return [], {}, {}
        ids = [r["id"] for r in task_rows]
        events_by: dict[str, list] = {tid: [] for tid in ids}
        runs_by: dict[str, list] = {tid: [] for tid in ids}
        cur.execute(
            "SELECT * FROM task_events WHERE board=%s AND task_id = ANY(%s) ORDER BY id",
            (board, ids),
        )
        for ev in cur.fetchall():
            events_by.setdefault(ev["task_id"], []).append(ev)
        cur.execute(
            "SELECT * FROM task_runs WHERE board=%s AND task_id = ANY(%s) ORDER BY id",
            (board, ids),
        )
        for rn in cur.fetchall():
            runs_by.setdefault(rn["task_id"], []).append(rn)
    return task_rows, events_by, runs_by


def wake_health(board: str, task_ids: list[str]) -> dict:
    """Board-level profile wake-health aggregate (mirrors _compute_wake_health)."""
    import time
    out = {
        "subscription_count": 0, "failing_count": 0, "stale_count": 0,
        "severity": "none", "as_of": int(time.time()),
    }
    if not task_ids:
        return out
    rows = _query(
        "SELECT "
        "  COUNT(*) AS subscription_count, "
        "  SUM(CASE WHEN (last_wake_error_at IS NOT NULL OR COALESCE(wake_failure_count,0) > 0) "
        "      THEN 1 ELSE 0 END) AS failing_count, "
        "  SUM(CASE WHEN (last_wake_error_at IS NULL AND COALESCE(wake_failure_count,0) = 0 "
        "      AND COALESCE(last_wake_at,0) = 0) THEN 1 ELSE 0 END) AS stale_count "
        "FROM kanban_profile_event_subs "
        "WHERE board=%s AND enabled = 1 AND wake_agent = 1 AND task_id = ANY(%s)",
        (board, list(task_ids)),
    )
    row = rows[0] if rows else {}
    subscription_count = int(row.get("subscription_count") or 0)
    failing_count = int(row.get("failing_count") or 0)
    stale_count = int(row.get("stale_count") or 0)
    if subscription_count == 0:
        severity = "none"
    elif failing_count > 0:
        severity = "failing"
    elif stale_count > 0:
        severity = "stale"
    else:
        severity = "healthy"
    out.update({
        "subscription_count": subscription_count, "failing_count": failing_count,
        "stale_count": stale_count, "severity": severity,
    })
    return out


def wake_health_rows(
    board: str, task_ids: list[str], tasks_by_id: dict, limit: int,
) -> tuple[list[dict], int]:
    """Ordered failing+stale rows + overflow (mirrors _collect_wake_health_rows)."""
    if not task_ids:
        return [], 0
    failing: list[dict] = []
    stale: list[dict] = []
    rows = _query(
        "SELECT task_id, profile, name, last_wake_at, last_wake_error_at, "
        "       last_wake_error, wake_failure_count FROM kanban_profile_event_subs "
        "WHERE board=%s AND enabled = 1 AND wake_agent = 1 AND task_id = ANY(%s)",
        (board, list(task_ids)),
    )
    for row in rows:
        tid = row["task_id"]
        failure_count = int(row["wake_failure_count"] or 0)
        error_at = row["last_wake_error_at"]
        last_wake_at = row["last_wake_at"]
        is_failing = error_at is not None or failure_count > 0
        is_stale = (not is_failing) and (last_wake_at is None or last_wake_at == 0)
        if not is_failing and not is_stale:
            continue
        task = tasks_by_id.get(tid)
        base = {
            "task_id": tid, "task_title": task.title if task else tid,
            "task_status": task.status if task else None,
            "profile": row["profile"], "name": row["name"] or "",
            "last_wake_at": last_wake_at, "last_wake_error_at": error_at,
            "wake_failure_count": failure_count,
        }
        if is_failing:
            base["kind"] = "failing"
            base["message"] = row["last_wake_error"] or "Last wake errored"
            failing.append(base)
        else:
            base["kind"] = "stale"
            base["message"] = "No successful wake yet"
            stale.append(base)
    failing.sort(key=lambda r: (-(r["last_wake_error_at"] or 0), r["task_id"], r["profile"], r["name"]))
    stale.sort(key=lambda r: (r["task_id"], r["profile"], r["name"]))
    combined = failing + stale
    overflow = max(0, len(combined) - limit)
    return combined[:limit], overflow
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_pg_reads.py -v`
Expected: PASS (all pg_reads unit tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/pg_reads.py tests/plugins/test_pg_reads.py
git commit -m "feat(kanban-pg): pg_reads diagnostics_rows + wake_health (+rows)"
```

---

### Task 5: Wire `GET /board` Postgres branch (the symptom fix)

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py` (`_compute_notifier_health` ~532; add `_compute_task_diagnostics_pg`; `get_board` ~690-830)
- Test: `tests/plugins/test_kanban_dashboard_plugin_pg.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/plugins/test_kanban_dashboard_plugin_pg.py`:

```python
def test_board_reflects_live_postgres(pg_client):
    s = pg_client.pg_store
    p = s.create_task(title="parent", assignee="engineer", body="b", tenant="acme")
    c = s.create_task(title="child", assignee="reviewer")
    s.link_tasks(p, c)
    s.add_comment(p, author="ops", body="hi")
    # Put a task into 'running' so the running column is non-empty.
    r = pg_client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    body = r.json()
    cols = {col["name"]: col["tasks"] for col in body["columns"]}
    all_ids = {t["id"] for tasks in cols.values() for t in tasks}
    assert {p, c} <= all_ids
    # aggregates present
    pcard = next(t for tasks in cols.values() for t in tasks if t["id"] == p)
    assert pcard["comment_count"] == 1
    assert pcard["link_counts"]["children"] == 1
    assert pcard["progress"] == {"done": 0, "total": 1}
    assert "acme" in body["tenants"]
    assert set(body["assignees"]) >= {"engineer", "reviewer"}
    assert body["latest_event_id"] > 0


def test_board_running_column_from_postgres(pg_client):
    s = pg_client.pg_store
    t = s.create_task(title="run me", assignee="engineer")
    # Drive it to running via a claim (mirrors the dispatcher claim path).
    claimed = s.claim_task(t)
    assert claimed is not None
    body = pg_client.get("/api/plugins/kanban/board").json()
    running = next(col["tasks"] for col in body["columns"] if col["name"] == "running")
    assert t in {x["id"] for x in running}
```

> Implementer note: confirm `claim_task` transitions the task to `running` on PG (it should, per the store Protocol). If a single `claim_task` is insufficient to reach `running`, use the store method that the dispatcher uses to mark running; the assertion is simply that a PG `running` task appears in the board's running column.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k board -v`
Expected: FAIL — `/board` reads the (empty) sqlite snapshot, so the seeded PG tasks are absent.

- [ ] **Step 3: Add `_compute_task_diagnostics_pg` + a PG notifier-health helper, then branch `get_board`**

In `plugin_api.py`, add near `_compute_task_diagnostics` (~line 410):

```python
def _compute_task_diagnostics_pg(
    board_slug: str, task_ids: Optional[list[str]] = None,
) -> dict[str, list[dict]]:
    """Postgres counterpart to _compute_task_diagnostics: fetch dict rows via
    pg_reads and run the (backend-agnostic) diagnostics engine."""
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config
    pg = _pg_reads()
    diag_config = kd.config_from_runtime_config(load_config())
    task_rows, events_by, runs_by = pg.diagnostics_rows(board_slug, task_ids=task_ids)
    if not task_rows:
        return {}
    out: dict[str, list[dict]] = {}
    for r in task_rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r, events_by.get(tid, []), runs_by.get(tid, []), config=diag_config,
        )
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def _compute_notifier_health_pg(
    board: Optional[str], *, wake_health: dict[str, Any],
) -> dict[str, Any]:
    """Notifier presence/overlap health on Postgres. Heartbeats live in a
    backend-agnostic sidecar, so reuse the same fetch the sqlite path uses
    (via the store, which delegates to kanban_db.list_notifier_heartbeats)."""
    as_of = int(time.time())
    subscription_count = int(wake_health.get("subscription_count") or 0)
    _slug, db_path = _resolved_board_slug_and_db_path(board)
    store = _store(board=board)
    try:
        rows = store.list_notifier_heartbeats(
            db_path=db_path, now=as_of,
            min_last_seen_at=as_of - kanban_db.NOTIFIER_HEARTBEAT_RETENTION_SECONDS,
            limit=kanban_db.NOTIFIER_HEARTBEAT_LIST_LIMIT,
        )
    except Exception as exc:
        store.close()
        return {
            "active_count": 0, "overlap_count": 0, "stale_count": 0,
            "severity": "unavailable",
            "message": ("Notifier heartbeat health is temporarily unavailable; "
                        "board tasks are still shown from the main task tables."),
            "latest_notifier": None, "rows": [], "as_of": as_of,
            "error": f"{type(exc).__name__}",
        }
    finally:
        try:
            store.close()
        except Exception:
            pass
    return _notifier_health_from_rows(rows, subscription_count, as_of)
```

> Implementer note: the sqlite `_compute_notifier_health` (lines ~532-616) computes `active_count`/severity/message from `rows`. Extract that pure post-fetch computation into a new module-level `_notifier_health_from_rows(rows, subscription_count, as_of)` and have BOTH the new `_compute_notifier_health_pg` and the existing sqlite `_compute_notifier_health` call it. This is a pure-refactor of post-fetch logic; verify the existing sqlite tests stay green (the sqlite fetch + error path is unchanged — only the shared tail is extracted). If you prefer zero change to the sqlite function, instead duplicate the post-fetch computation inside `_compute_notifier_health_pg` verbatim. Either keeps the sqlite path's behavior identical.

Then in `get_board` (~line 711), wrap the existing body in an `else:` and add the PG branch at the top of the function body (after `board = _resolve_board(board)`):

```python
    board = _resolve_board(board)
    if _backend() == "postgres":
        pg = _pg_reads()
        bslug = pg.slug(board)
        store = _store(board=board)
        try:
            tasks = store.list_tasks(
                tenant=tenant, include_archived=include_archived,
                workflow_template_id=workflow_template_id,
                current_step_key=current_step_key,
            )
            task_ids = [t.id for t in tasks]
            wake_health = pg.wake_health(bslug, task_ids)
            notifier_health = _compute_notifier_health_pg(board, wake_health=wake_health)
            link_counts = pg.link_counts(bslug)
            comment_counts = pg.comment_counts(bslug)
            progress = pg.child_progress(bslug)
            diagnostics_per_task = _compute_task_diagnostics_pg(bslug, task_ids=None)
            latest_event_id = pg.latest_event_id(bslug)
            summary_map = store.latest_summaries(task_ids)

            columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
            if include_archived:
                columns["archived"] = []
            for t in tasks:
                full = summary_map.get(t.id)
                preview = full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None
                d = _task_dict(t, latest_summary=preview)
                d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
                d["comment_count"] = comment_counts.get(t.id, 0)
                d["progress"] = progress.get(t.id)
                diags = diagnostics_per_task.get(t.id)
                if diags:
                    d["diagnostics"] = diags
                    d["warnings"] = _warnings_summary_from_diagnostics(diags)
                col = t.status if t.status in columns else "todo"
                columns[col].append(d)

            return {
                "columns": [{"name": name, "tasks": columns[name]} for name in columns.keys()],
                "tenants": pg.distinct_tenants(bslug),
                "assignees": pg.distinct_assignees(bslug),
                "wake_health": wake_health,
                "notifier_health": notifier_health,
                "latest_event_id": int(latest_event_id),
                "now": int(time.time()),
            }
        finally:
            store.close()
    conn = _conn(board=board, readonly=True)
    try:
        <... existing sqlite body unchanged ...>
    finally:
        conn.close()
```

> Implementer note: the PG branch reproduces the sqlite assembly so the response dict is identical. Keep the sqlite branch (the `conn = _conn(...)` block through its `finally: conn.close()`) byte-identical — just indented under the implicit fall-through (no `else:` needed since the PG branch returns).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k board -v`
Expected: PASS. Then sqlite regression: `... -m pytest tests/plugins/test_kanban_dashboard_plugin_api.py -k board -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin_pg.py
git commit -m "feat(kanban-pg): GET /board reads live Postgres under backend=postgres"
```

---

### Task 6: Wire `WS /events` Postgres branch (live updates)

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py` (`stream_events._fetch_new` ~2951-2995)
- Test: `tests/plugins/test_kanban_dashboard_plugin_pg.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/plugins/test_kanban_dashboard_plugin_pg.py`:

```python
def test_events_stream_tails_postgres(pg_client):
    s = pg_client.pg_store
    t = s.create_task(title="evt", assignee="engineer")  # emits task_events rows
    with pg_client.websocket_connect("/api/plugins/kanban/events?since=0") as ws:
        msg = ws.receive_json()
        assert "events" in msg and msg["cursor"] > 0
        assert any(e["task_id"] == t for e in msg["events"])
        # payloads are objects/None, never raw JSON strings
        assert all(not isinstance(e["payload"], str) for e in msg["events"])
```

> Implementer note: `TestClient.websocket_connect` requires the WS auth to pass. `_check_ws_token` returns True when the dashboard `web_server` module isn't importable (test context) — confirm; if it requires a token in this harness, pass `?token=` accordingly or monkeypatch `_check_ws_token` to True for the test.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k events_stream -v`
Expected: FAIL — the tail reads the empty sqlite snapshot, so the seeded PG event is absent (the WS sends nothing / times out).

- [ ] **Step 3: Branch `_fetch_new`**

In `stream_events`, replace the body of the inner `_fetch_new(cursor_val, wake_cursor_val)` with a backend branch (keep the sqlite branch verbatim):

```python
        def _fetch_new(cursor_val: int, wake_cursor_val: int) -> tuple[int, list[dict], int, list[dict]]:
            if _backend() == "postgres":
                pg = _pg_reads()
                bslug = pg.slug(ws_board)
                new_cursor, out = pg.events_since(bslug, cursor_val, 200)
                store = _store(board=ws_board)
                try:
                    wake_rows = store.list_profile_wake_events(
                        since_id=wake_cursor_val, limit=200,
                    )
                finally:
                    store.close()
                wake_out: list[dict] = []
                new_wake_cursor = wake_cursor_val
                for wr in wake_rows:
                    wake_out.append({
                        "id": wr.get("id"), "task_id": wr.get("task_id"),
                        "profile": wr.get("profile"), "name": wr.get("name") or "",
                        "status": wr.get("status"), "error": wr.get("error"),
                        "claimed_event_cursor": wr.get("claimed_event_cursor"),
                        "created_at": wr.get("created_at"),
                    })
                    new_wake_cursor = int(wr.get("id") or new_wake_cursor)
                return new_cursor, out, new_wake_cursor, wake_out
            conn = _readonly_snapshot_conn(board=ws_board)
            try:
                <... existing sqlite _fetch_new body unchanged ...>
            finally:
                conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k events_stream -v`
Expected: PASS. sqlite regression: `... -k events` on the existing plugin test → PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin_pg.py
git commit -m "feat(kanban-pg): WS /events tails the Postgres board under backend=postgres"
```

---

### Task 7: Wire `GET /tasks/{id}`, `/workers/active`, `/diagnostics`, `/wake-health/details`, log existence check

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py` (`get_task` ~987; `list_active_workers` ~1565; `list_diagnostics` ~1478; `get_wake_health_details` ~925; `get_task_log` ~2311 + `stream_task_log` ~2359; `_links_for_pg`)
- Test: `tests/plugins/test_kanban_dashboard_plugin_pg.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/plugins/test_kanban_dashboard_plugin_pg.py`:

```python
def test_task_detail_workers_diagnostics_pg(pg_client):
    s = pg_client.pg_store
    p = s.create_task(title="parent", assignee="engineer", body="pbody")
    c = s.create_task(title="child", assignee="reviewer", body="cbody")
    s.link_tasks(p, c)
    s.add_comment(c, author="ops", body="please review")

    r = pg_client.get(f"/api/plugins/kanban/tasks/{c}")
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["id"] == c and body["task"]["body"] == "cbody"
    assert [cm["body"] for cm in body["comments"]] == ["please review"]
    assert body["links"]["parents"] == [p]

    # run-state filters are not supported on PG -> clean 400, not 500
    r2 = pg_client.get(f"/api/plugins/kanban/tasks/{c}?run_state_type=status&run_state_name=running")
    assert r2.status_code == 400

    aw = pg_client.get("/api/plugins/kanban/workers/active").json()
    assert aw["count"] == 0 and aw["workers"] == []

    dg = pg_client.get("/api/plugins/kanban/diagnostics").json()
    assert "diagnostics" in dg and "count" in dg

    wh = pg_client.get("/api/plugins/kanban/wake-health/details").json()
    assert "wake_health" in wh and "rows" in wh

    lg = pg_client.get(f"/api/plugins/kanban/tasks/{c}/log")
    assert lg.status_code == 200  # exists check via store; log file absent -> content ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k "task_detail" -v`
Expected: FAIL — 404 (task not in sqlite snapshot).

- [ ] **Step 3: Branch each handler**

Add a `_links_for_pg` helper near `_links_for` (~619):

```python
def _links_for_pg(board_slug: str, task_id: str) -> dict[str, list[str]]:
    pg = _pg_reads()
    rows = pg._query(
        "SELECT parent_id FROM task_links WHERE board=%s AND child_id=%s ORDER BY parent_id",
        (board_slug, task_id),
    )
    parents = [r["parent_id"] for r in rows]
    rows = pg._query(
        "SELECT child_id FROM task_links WHERE board=%s AND parent_id=%s ORDER BY child_id",
        (board_slug, task_id),
    )
    children = [r["child_id"] for r in rows]
    return {"parents": parents, "children": children}
```

**`get_task`** (~987): after `board = _resolve_board(board)` and the run_state validation, add a PG branch (the run_state validation stays before it so a bad combination still 400s). Under PG, if `run_state_type`/`run_state_name` are provided, return 400 (filters unsupported); else assemble from the store + pg_reads:

```python
        if _backend() == "postgres":
            if run_state_type is not None or run_state_name is not None:
                raise HTTPException(
                    status_code=400,
                    detail="run state filtering is not yet supported on the postgres backend",
                )
            pg = _pg_reads()
            bslug = pg.slug(board)
            store = _store(board=board)
            try:
                task = store.get_task(task_id)
                if task is None:
                    raise HTTPException(status_code=404, detail=f"task {task_id} not found")
                full_summary = store.latest_summary(task_id)
                task_d = _task_dict(task, latest_summary=full_summary)
                diag_list = _compute_task_diagnostics_pg(bslug, task_ids=[task_id]).get(task_id) or []
                if diag_list:
                    task_d["diagnostics"] = diag_list
                    task_d["warnings"] = _warnings_summary_from_diagnostics(diag_list)
                return {
                    "task": task_d,
                    "comments": [_comment_dict(c) for c in store.list_comments(task_id)],
                    "events": [_event_dict(e) for e in store.list_events(task_id)],
                    "links": _links_for_pg(bslug, task_id),
                    "runs": [_run_dict(r) for r in store.list_runs(task_id)],
                }
            finally:
                store.close()
        conn = _conn(board=board, readonly=True)
        try:
            <... existing sqlite body unchanged ...>
        finally:
            conn.close()
```

> Implementer note: the existing `get_task` already does the `(run_state_type is None) ^ (run_state_name is None)` and value validation inside the `try:`; keep those validations running for BOTH backends (they raise 400 on bad input). Place the PG branch right after those validations.

**`list_active_workers`** (~1565): branch:

```python
    board = _resolve_board(board)
    if _backend() == "postgres":
        pg = _pg_reads()
        workers = pg.active_workers(pg.slug(board))
        return {"workers": workers, "count": len(workers), "checked_at": int(time.time())}
    conn = _conn(board=board, readonly=True)
    try:
        <... existing sqlite body unchanged ...>
    finally:
        conn.close()
```

**`list_diagnostics`** (~1478): branch the `conn`-based body. Under PG, build `diags_by_task` via `_compute_task_diagnostics_pg`, and fetch the title/status/assignee rows from the store:

```python
    board = _resolve_board(board)
    if _backend() == "postgres":
        pg = _pg_reads()
        bslug = pg.slug(board)
        diags_by_task = _compute_task_diagnostics_pg(bslug, task_ids=None)
        if not diags_by_task:
            return {"diagnostics": [], "count": 0}
        if severity:
            filtered = {}
            for tid, dl in diags_by_task.items():
                keep = [d for d in dl if kd.severity_at_or_above(d.get("severity"), severity)]
                if keep:
                    filtered[tid] = keep
            diags_by_task = filtered
            if not diags_by_task:
                return {"diagnostics": [], "count": 0}
        store = _store(board=board)
        try:
            tasks_by_id = {t.id: t for t in store.list_tasks(include_archived=True)}
        finally:
            store.close()
        out = []
        for tid, dl in diags_by_task.items():
            t = tasks_by_id.get(tid)
            out.append({
                "task_id": tid,
                "task_title": t.title if t else None,
                "task_status": t.status if t else None,
                "task_assignee": t.assignee if t else None,
                "diagnostics": dl,
            })
        from hermes_cli.kanban_diagnostics import SEVERITY_ORDER
        sev_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        def _sort_key(row):
            top = row["diagnostics"][0]
            return (-sev_idx.get(top.get("severity"), -1), -(top.get("last_seen_at") or 0))
        out.sort(key=_sort_key)
        return {"diagnostics": out, "count": sum(len(d["diagnostics"]) for d in out)}
    conn = _conn(board=board, readonly=True)
    try:
        <... existing sqlite body unchanged ...>
    finally:
        conn.close()
```

**`get_wake_health_details`** (~925): branch:

```python
    board = _resolve_board(board)
    if _backend() == "postgres":
        pg = _pg_reads()
        bslug = pg.slug(board)
        store = _store(board=board)
        try:
            tasks = store.list_tasks(
                tenant=tenant, include_archived=include_archived,
                workflow_template_id=workflow_template_id, current_step_key=current_step_key,
            )
        finally:
            store.close()
        task_ids = [t.id for t in tasks]
        tasks_by_id = {t.id: t for t in tasks}
        wake_health = pg.wake_health(bslug, task_ids)
        notifier_health = _compute_notifier_health_pg(board, wake_health=wake_health)
        rows, overflow = pg.wake_health_rows(bslug, task_ids, tasks_by_id, limit)
        return {
            "wake_health": wake_health, "notifier_health": notifier_health,
            "rows": rows, "overflow_count": overflow, "as_of": int(time.time()),
        }
    conn = _conn(board=board, readonly=True)
    try:
        <... existing sqlite body unchanged ...>
    finally:
        conn.close()
```

**`get_task_log`** (~2311) and **`stream_task_log`** (~2384): the existence check uses `_conn` + `kanban_db.get_task`. Branch only that check:

```python
    # get_task_log:
    board = _resolve_board(board)
    if _backend() == "postgres":
        store = _store(board=board)
        try:
            task = store.get_task(task_id)
        finally:
            store.close()
    else:
        conn = _conn(board=board, readonly=True)
        try:
            task = kanban_db.get_task(conn, task_id)
        finally:
            conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    <... rest unchanged: read_worker_log / worker_log_path ...>
```

For `stream_task_log` apply the same branch to its `task = kanban_db.get_task(conn, task_id)` existence probe (before `await ws.accept()`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k "task_detail" -v`
Expected: PASS. sqlite regression on the existing plugin tests for these endpoints → PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin_pg.py
git commit -m "feat(kanban-pg): /tasks/{id}, /workers/active, /diagnostics, /wake-health/details, log read Postgres"
```

---

### Task 8: Wire the 3 writes + `/reconcile` no-op + `/boards` counts

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py` (`create_task` ~1065; `update_task` ~1131; `bulk_update` ~1372; `get_reconcile_health` ~643; `_board_counts` ~2528)
- Test: `tests/plugins/test_kanban_dashboard_plugin_pg.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/plugins/test_kanban_dashboard_plugin_pg.py`:

```python
def test_writes_land_in_postgres(pg_client):
    s = pg_client.pg_store
    # create
    r = pg_client.post("/api/plugins/kanban/tasks", json={"title": "made in dashboard", "assignee": "engineer"})
    assert r.status_code == 200
    tid = r.json()["task"]["id"]
    assert s.get_task(tid) is not None  # landed in PG, not frozen sqlite

    # patch (rename + priority)
    r = pg_client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"title": "renamed", "priority": 5})
    assert r.status_code == 200
    assert s.get_task(tid).title == "renamed"
    assert s.get_task(tid).priority == 5

    # bulk archive
    r = pg_client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [tid], "archive": True})
    assert r.status_code == 200
    assert r.json()["results"][0]["ok"] is True
    assert s.get_task(tid).status == "archived"


def test_reconcile_graceful_noop_pg(pg_client):
    r = pg_client.get("/api/plugins/kanban/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert body.get("actions") in ([], None) or body.get("total_actions", 0) == 0
    assert "postgres" in (body.get("text_preview", "") + str(body.get("note", ""))).lower()


def test_boards_counts_pg(pg_client):
    s = pg_client.pg_store
    s.create_task(title="x", assignee="engineer")
    body = pg_client.get("/api/plugins/kanban/boards").json()
    default = next((b for b in body["boards"] if b["slug"] == "default"), None)
    assert default is not None and default["total"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k "writes or reconcile or boards_counts" -v`
Expected: FAIL — writes go to sqlite (`s.get_task` is None / wrong); `/reconcile` errors or reads frozen sqlite; board counts read sqlite.

- [ ] **Step 3: Branch each handler**

**`create_task`** (~1065): branch the `write_session` + read-back:

```python
    board = _resolve_board(board)
    if _backend() == "postgres":
        store = _store(board=board)
        try:
            try:
                task_id = store.create_task(
                    title=payload.title, body=payload.body, assignee=payload.assignee,
                    created_by="dashboard", workspace_kind=payload.workspace_kind,
                    workspace_path=payload.workspace_path, tenant=payload.tenant,
                    priority=payload.priority, parents=payload.parents,
                    triage=payload.triage, idempotency_key=payload.idempotency_key,
                    max_runtime_seconds=payload.max_runtime_seconds, skills=payload.skills,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            task = store.get_task(task_id)
            body_out: dict[str, Any] = {"task": _task_dict(task) if task else None}
            if task and task.status == "ready" and task.assignee:
                try:
                    from hermes_cli.kanban import _check_dispatcher_presence
                    running, message = _check_dispatcher_presence()
                    if not running and message:
                        body_out["warning"] = message
                except Exception:
                    pass
            return body_out
        finally:
            store.close()
    # sqlite (unchanged)
    try:
        with kanban_db.write_session(board=board) as w:
            <... existing sqlite body unchanged ...>
```

> Implementer note: confirm `store.create_task(...)` accepts `parents=` and `triage=` kwargs (the Protocol's `create_task(self, **kwargs)` forwards them to the backend; the PG impl should mirror the sqlite signature). If a kwarg is unsupported on PG, surface it; do not silently drop `parents`.

**`update_task`** (~1131): under PG, open one store and replicate the multi-op flow with store methods; `_read_status()` and the existence check read via the store; the 409 blocking-parents enrichment uses `_links`/`pg_reads.parents_blocking_ready`:

```python
    board = _resolve_board(board)
    # (validation of title/status as today, runs for both backends)
    ...
    if _backend() == "postgres":
        store = _store(board=board)
        try:
            if store.get_task(task_id) is None:
                raise HTTPException(status_code=404, detail=f"task {task_id} not found")

            def _read_status_pg():
                t = store.get_task(task_id)
                return t.status if t else None

            if payload.assignee is not None:
                try:
                    ok = store.assign_task(task_id=task_id, profile=payload.assignee or None)
                except RuntimeError as e:
                    raise HTTPException(status_code=409, detail=str(e))
                if not ok:
                    raise HTTPException(status_code=404, detail="task not found")
            if payload.status is not None:
                s = payload.status
                if s == "done":
                    ok = store.complete_task(task_id=task_id, result=payload.result,
                                             summary=payload.summary, metadata=payload.metadata)
                elif s == "blocked":
                    ok = store.block_task(task_id=task_id, reason=payload.block_reason)
                elif s == "scheduled":
                    ok = store.schedule_task(task_id=task_id, reason=payload.block_reason)
                elif s == "ready":
                    ok = (store.unblock_task(task_id=task_id)
                          if _read_status_pg() in ("blocked", "scheduled")
                          else store.set_status_direct(task_id=task_id, new_status="ready"))
                elif s == "archived":
                    ok = store.archive_task(task_id=task_id)
                else:
                    ok = store.set_status_direct(task_id=task_id, new_status=s)
                if not ok:
                    if s == "ready":
                        blockers = _pg_reads().parents_blocking_ready(_pg_reads().slug(board), task_id)
                        if blockers:
                            names = ", ".join(
                                f"{p['title']!r} ({p['id']}, status={p['status']})" for p in blockers)
                            raise HTTPException(
                                status_code=409,
                                detail=f"Cannot move to 'ready': blocked by parent(s) not done — {names}")
                    raise HTTPException(
                        status_code=409,
                        detail=f"status transition to {s!r} not valid from current state")
            if payload.priority is not None:
                store.set_task_priority(task_id=task_id, priority=int(payload.priority))
            if payload.title is not None or payload.body is not None:
                store.edit_task_fields(task_id=task_id, title=payload.title, body=payload.body)
            updated = store.get_task(task_id)
            return {"task": _task_dict(updated) if updated else None}
        finally:
            store.close()
    # sqlite (unchanged)
    from hermes_cli.kanban_writer_client import RemoteWriteError
    rconn = _conn(board=board, readonly=True)
    <... existing sqlite body unchanged ...>
```

> Implementer note: the `payload.status == "running"` 400 and the unknown-status 400 validations at the top of `update_task` run for both backends (keep them before the branch). Confirm `store.complete_task` accepts `result=`/`summary=`/`metadata=` (Protocol: yes).

**`bulk_update`** (~1372): under PG, open one store and run the per-id loop with store methods (pre-batch reads via `store.get_task`):

```python
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    results: list[dict] = []
    board = _resolve_board(board)
    if _backend() == "postgres":
        store = _store(board=board)
        try:
            for tid in ids:
                entry: dict[str, Any] = {"id": tid, "ok": True}
                try:
                    task = store.get_task(tid)
                    if task is None:
                        entry.update(ok=False, error="not found"); results.append(entry); continue
                    if payload.archive:
                        if not store.archive_task(task_id=tid):
                            entry.update(ok=False, error="archive refused")
                    if payload.status is not None and not payload.archive:
                        s = payload.status
                        if s == "done":
                            ok = store.complete_task(task_id=tid, result=payload.result,
                                                     summary=payload.summary, metadata=payload.metadata)
                        elif s == "blocked":
                            ok = store.block_task(task_id=tid)
                        elif s == "ready":
                            cur = store.get_task(tid)
                            ok = (store.unblock_task(task_id=tid)
                                  if cur and cur.status in ("blocked", "scheduled")
                                  else store.set_status_direct(task_id=tid, new_status="ready"))
                        elif s == "running":
                            entry.update(ok=False, error=("Cannot set status to 'running' directly; "
                                                          "use the dispatcher/claim path"))
                            results.append(entry); continue
                        elif s == "scheduled":
                            ok = store.schedule_task(task_id=tid)
                        elif s in {"todo", "triage"}:
                            ok = store.set_status_direct(task_id=tid, new_status=s)
                        else:
                            entry.update(ok=False, error=f"unknown status {s!r}")
                            results.append(entry); continue
                        if not ok:
                            entry.update(ok=False, error=f"transition to {s!r} refused")
                    if payload.assignee is not None:
                        try:
                            ok = (store.reassign_task(task_id=tid, profile=payload.assignee or None,
                                                      reclaim_first=True)
                                  if payload.reclaim_first
                                  else store.assign_task(task_id=tid, profile=payload.assignee or None))
                            if not ok:
                                entry.update(ok=False, error="assign refused")
                        except RuntimeError as e:
                            entry.update(ok=False, error=str(e))
                    if payload.priority is not None:
                        store.set_task_priority(task_id=tid, priority=int(payload.priority))
                except Exception as e:
                    entry.update(ok=False, error=str(e))
                results.append(entry)
        finally:
            store.close()
        return {"results": results}
    from hermes_cli.kanban_writer_client import RemoteWriteError
    rconn = _conn(board=board, readonly=True)
    <... existing sqlite body unchanged ...>
```

**`get_reconcile_health`** (~643): graceful no-op under PG:

```python
    board = _resolve_board(board)
    if _backend() == "postgres":
        note = "reconcile is not yet available on the postgres backend"
        return {
            "board": _pg_reads().slug(board), "actions": [], "total_actions": 0,
            "note": note, "text_preview": note,
        }
    result = kanban_reconciler.run_reconciler(...)  # existing unchanged
    ...
```

> Implementer note: match the existing `get_reconcile_health` return shape's key names where the frontend/CLI read them; the safe minimum is an empty `actions` list + a `text_preview` mentioning postgres. Inspect `kanban_reconciler.run_reconciler`'s result dict keys and mirror the empty-but-valid shape so any consumer that indexes a key doesn't KeyError.

**`_board_counts`** (~2528): under PG, the active/`default` board's counts come from `pg_reads.board_counts`; non-default on-disk boards return `{}` (single-board on PG):

```python
def _board_counts(slug: str) -> dict[str, int]:
    if _backend() == "postgres":
        try:
            pg = _pg_reads()
            if slug == pg.slug(None):  # the active/default PG board
                return pg.board_counts(slug)
            return {}
        except Exception:
            return {}
    try:
        <... existing sqlite body unchanged ...>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_plugin_pg.py -k "writes or reconcile or boards_counts" -v`
Expected: PASS. sqlite regression: the existing plugin tests for create/patch/bulk/reconcile/boards → PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin_pg.py
git commit -m "feat(kanban-pg): dashboard create/PATCH/bulk write to Postgres; /reconcile no-op; /boards counts"
```

---

### Task 9: sqlite↔Postgres `/board` + `/tasks/{id}` parity test

**Files:**
- Test: `tests/plugins/test_kanban_dashboard_parity.py` (create)

- [ ] **Step 1: Write the parity test**

Create `tests/plugins/test_kanban_dashboard_parity.py`:

```python
"""GET /board and GET /tasks/{id} must be identical across sqlite and postgres
for identical seed data (modulo ids/timestamps)."""
import importlib.util
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _router():
    repo_root = Path(__file__).resolve().parents[2]
    f = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_dash_parity_test", f)
    m = importlib.util.module_from_spec(spec)
    import sys; sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m.router


def _seed(store):
    p = store.create_task(title="parent", assignee="engineer", body="pbody", tenant="acme")
    c = store.create_task(title="child", assignee="reviewer", body="cbody")
    store.link_tasks(p, c)
    store.add_comment(p, author="ops", body="comment one")
    return p, c


def _norm(obj, idmap, ts_keys=("created_at", "started_at", "completed_at", "now",
                               "as_of", "latest_event_id", "checked_at", "age")):
    """Recursively replace task ids with stable tokens and null out timestamps."""
    if isinstance(obj, dict):
        return {k: ("<TS>" if k in ts_keys else _norm(v, idmap, ts_keys)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm(x, idmap, ts_keys) for x in obj]
    if isinstance(obj, str):
        return idmap.get(obj, obj)
    return obj


def test_board_parity(tmp_path, monkeypatch, _pg_dsn):
    # --- sqlite ---
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PG_DSN", raising=False)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    s_sql = SqliteKanbanStore(board=None)
    p1, c1 = _seed(s_sql)
    app1 = FastAPI(); app1.include_router(_router(), prefix="/api/plugins/kanban")
    b_sql = TestClient(app1).get("/api/plugins/kanban/board").json()
    t_sql = TestClient(app1).get(f"/api/plugins/kanban/tasks/{c1}").json()
    s_sql.close()

    # --- postgres ---
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        for tbl in ("task_events", "task_comments", "task_runs", "task_links",
                    "kanban_profile_wake_events", "kanban_profile_event_subs", "tasks"):
            cur.execute(f"DELETE FROM {tbl} WHERE board=%s", ("default",))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", _pg_dsn)
    s_pg = PostgresKanbanStore(board="default", pool=pool)
    p2, c2 = _seed(s_pg)
    app2 = FastAPI(); app2.include_router(_router(), prefix="/api/plugins/kanban")
    b_pg = TestClient(app2).get("/api/plugins/kanban/board").json()
    t_pg = TestClient(app2).get(f"/api/plugins/kanban/tasks/{c2}").json()
    s_pg.close(); pool.close()

    idmap = {p2: "<P>", c2: "<C>", p1: "<P>", c1: "<C>"}
    assert _norm(b_sql, idmap) == _norm(b_pg, idmap)
    assert _norm(t_sql, idmap) == _norm(t_pg, idmap)
```

> Implementer note: this is the drift-defense test. If a non-id/non-timestamp field legitimately differs (e.g. `notifier_health.message` text, or a key present on one backend only), expand `ts_keys`/`idmap` or normalize that field — but FIRST confirm the difference is benign and not a real payload-shape divergence. The goal is identical logical content.

- [ ] **Step 2: Run the parity test**

Run: `cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/plugins/test_kanban_dashboard_parity.py -v`
Expected: PASS (iterate normalization until identical; any genuine shape divergence is a bug to fix in the PG branch).

- [ ] **Step 3: Commit**

```bash
git add tests/plugins/test_kanban_dashboard_parity.py
git commit -m "test(kanban-pg): sqlite↔postgres /board + /tasks parity"
```

---

### Task 10: Full regression + boundary verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full dashboard + pg_reads suites on both backends**

Run:
```
cd <worktree> && HERMES_PG_TEST_DSN="$HERMES_PG_TEST_DSN" /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/plugins/ -q
```
Expected: all PASS (sqlite regression 140+ green, PG tests green, parity green). No regressions.

- [ ] **Step 2: Confirm `kanban_db.py` + `kanban_liveness.py` untouched**

Run: `cd <worktree> && git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_liveness.py`
Expected: **empty**.

- [ ] **Step 3: Confirm sqlite handler bodies byte-identical**

Run: `cd <worktree> && git diff main -- plugins/kanban/dashboard/plugin_api.py`
Inspect: every change is an additive `if _backend()=="postgres": ... ` branch or a `_pg_reads()`/`_backend()`/`_compute_*_pg`/`_links_for_pg`/`_notifier_health_from_rows` helper; no sqlite branch logic altered (other than the documented `_notifier_health_from_rows` pure-extraction, if taken).

- [ ] **Step 4: Confirm no DSN/secret literal landed**

Run: `cd <worktree> && git diff main | grep -iE "supabase|pooler\.supabase|postgresql://postgres\.[a-z]" || echo "clean"`
Expected: `clean`.

- [ ] **Step 5: Confirm default backend stays sqlite**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -c "import os; os.environ.pop('HERMES_KANBAN_BACKEND', None); from hermes_cli.kanban.store import resolve_backend; print(resolve_backend())"`
Expected: `sqlite`.

---

## Self-Review

**Spec coverage:**
- Architecture (Approach 2: pg_reads module + if-pg branches) → Tasks 2-8. ✓
- `GET /board` live PG (symptom) → Task 5. ✓
- `WS /events` PG tail → Task 6. ✓
- `GET /tasks/{id}`, `/workers/active`, `/diagnostics`, `/wake-health/details`, log existence → Task 7. ✓
- 3 writes (create/PATCH/bulk) → Task 8. ✓
- `/reconcile` graceful no-op + `/boards` counts → Task 8. ✓
- `/doctor` already PG-aware → no task needed (verified in design). ✓
- notifier_health via backend-agnostic sidecar → Task 5 (`_compute_notifier_health_pg`). ✓
- list_runs state-filter 400 + single-board + ordering edge cases → Task 7 (400) / verbatim facts (ordering). ✓
- Testing: PG harness (Task 1), pg_reads units (2-4), endpoint PG tests (5-8), parity (9), regression+boundaries (10). ✓
- Hard boundaries (kanban_db/kanban_liveness untouched, sqlite byte-identical, no DSN leak, default sqlite) → Task 10. ✓

**Type/name consistency:** `_backend()` / `_pg_reads()` / `_compute_task_diagnostics_pg()` / `_compute_notifier_health_pg()` / `_links_for_pg()` defined in Task 2/5/7 and used consistently. `pg_reads` public fns: `slug`, `link_counts`, `comment_counts`, `child_progress`, `distinct_tenants`, `distinct_assignees`, `latest_event_id`, `board_counts` (Task 2); `events_since`, `active_workers`, `parents_blocking_ready` (Task 3); `diagnostics_rows`, `wake_health`, `wake_health_rows` (Task 4) — all called with matching signatures in Tasks 5-8. `pg._query` reused by `_links_for_pg` (Task 7). Store methods used (`list_tasks`, `latest_summaries`, `latest_summary`, `get_task`, `list_comments`, `list_events`, `list_runs`, `list_profile_wake_events`, `list_notifier_heartbeats`, `create_task`, `assign_task`, `complete_task`, `block_task`, `schedule_task`, `unblock_task`, `set_status_direct`, `archive_task`, `set_task_priority`, `edit_task_fields`, `reassign_task`, `claim_task`, `board_stats`) all exist on the Protocol per the verbatim facts.

**Placeholder scan:** every code step shows complete code; the four implementer-notes (claim_task→running, WS token in tests, store.create_task parents kwarg, reconcile result-shape keys) flag reality-checks against existing code, each with the executable test as the spec — not deferred work.
