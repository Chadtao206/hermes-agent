# Kanban Postgres Migration — Phase 2: PostgresKanbanStore + pool + conformance vs Postgres

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real Postgres backend (`PostgresKanbanStore`) behind the existing `KanbanStore` interface, proven by running the backend-parametrized conformance suite green against an ephemeral docker Postgres — with **zero behavior change** on the default `sqlite` backend.

**Architecture:** New fork-owned files under `hermes_cli/kanban/`: `pg_schema.sql` (DDL translated from `kanban_db.SCHEMA_SQL`, with a `board` column added), `pg_pool.py` (a process-wide psycopg 3 `ConnectionPool` whose DSN comes from config/env; Supabase-transaction-pooler-shaped, validated against docker), and `store_postgres.py` (`PostgresKanbanStore`, a fresh psycopg implementation of the conformance-covered `KanbanStore` surface — NOT a delegating adapter). The `kanban_store()` factory returns it when `kanban.backend == "postgres"`. The conformance fixture gains a `postgres` param backed by a throwaway docker container.

**Tech Stack:** Python 3.11, `psycopg[binary,pool]` v3 (NEW dependency), Docker (ephemeral Postgres for tests), `pytest`, existing `hermes_cli.kanban.store` Protocol + conformance suite.

**Reference spec:** `plans/kanban-postgres-migration/design.md`. Phase 1 (the interface + `SqliteKanbanStore` + the conformance suite) is already merged to `main`.

---

## Decisions locked in brainstorming (read before starting)

1. **Board model:** a `board TEXT NOT NULL DEFAULT 'default'` column on every table (single Postgres database/schema), folded into primary keys / unique constraints / indexes. The store captures `board` at construction (`PostgresKanbanStore(board=...)`, default `'default'`) and every query filters/sets `board`. (The live system effectively runs one board; this keeps multi-board support cheap.)
2. **Scope this phase:** the **conformance-covered core** — make the existing conformance suite green against Postgres, then extend it modestly to cover `complete_task` (basic), runs reads, `list_events`, and event-claiming, plus a `claim_task` (SKIP LOCKED) primitive + test. The most intricate `kanban_db` semantics (`complete_task`'s hallucination-card / PR-head gates + closeout packets, `build_worker_context`, the failure-breaker/`gave_up` machinery, sticky-block nuances beyond the simple case) are **deferred** and must raise a loud `NotImplementedError("phase-2-tail: <method>")` rather than silently diverge.
3. **Test target:** ephemeral docker Postgres via a pytest fixture (psycopg 3). Live Supabase wiring (project, credentials, region, pooler endpoint) is deferred to the cutover phase; `pg_pool.py` is written Supabase-shaped but validated against docker.

**Not touched in Phase 2:** `gateway/run.py` / dispatcher-notifier glue (Phase 3), migration tooling (Phase 4), cutover (Phase 5), `kanban_db.py` internals (the SQLite backend stays as-is), the agent-tools / dashboard / CLI callers (already route through the store).

---

## Reference: the conformance suite is the gate

`tests/hermes_cli/kanban/test_store_conformance.py` (already merged) is the executable spec. Every test takes the parametrized `store` fixture and runs against each backend in `conftest._BACKENDS`. Phase 1 has `_BACKENDS = ["sqlite"]`; this phase adds `"postgres"`. The existing tests exercise (do NOT modify them): `create_task`/`get_task`, `block_task`/`unblock_task`, `set_task_priority`/`edit_task_fields`/`set_status_direct`, `reassign_task`/`delete_task`, `link_tasks`/`unlink_tasks`/`parent_ids`/`child_ids`, `add_comment`/`list_comments`, `add_notify_sub`/`list_notify_subs`(+`task_id` filter)/`remove_notify_sub`, `add_profile_event_sub`/`list_profile_event_subs`/`remove_profile_event_sub`/`recompute_ready`/`has_spawnable_ready`, `gc_events`, `set_workspace_path`, `promote_task`.

**Return shapes the Postgres backend MUST match** (dataclasses from `hermes_cli/kanban_db.py`, reused as return types — import them, do not redefine):
- `Task` (fields incl. `id, title, body, assignee, status, priority, created_by, created_at, started_at, completed_at, workspace_kind, workspace_path, claim_lock, claim_expires, tenant, branch_name, result, idempotency_key, consecutive_failures, worker_pid, last_failure_error, max_runtime_seconds, last_heartbeat_at, current_run_id, workflow_template_id, current_step_key, skills (parsed list), model_override, max_retries, session_id`).
- `Run`, `Event`, `Comment` (see kanban_db). `list_notify_subs` / `list_profile_event_subs` return `list[dict]`.

**Key constants to mirror** (import from `kanban_db` where possible): `VALID_STATUSES = {triage, todo, scheduled, ready, running, blocked, done, archived}`; `VALID_INITIAL_STATUSES = {running, blocked, scheduled}`; task id = `"t_" + secrets.token_hex(4)`; timestamps are `int(time.time())` epoch seconds; `LINK_RELATION_DEPENDENCY = "dependency"`; `DEFAULT_NOTIFY_TERMINAL_KINDS = ("completed","blocked","gave_up","crashed","timed_out","archived")`.

---

## File structure

- Create: `hermes_cli/kanban/pg_schema.sql` — Postgres DDL (Task 2).
- Create: `hermes_cli/kanban/pg_pool.py` — psycopg 3 pool + DSN resolution (Task 3).
- Create: `hermes_cli/kanban/store_postgres.py` — `PostgresKanbanStore` (Tasks 4–7, 9, 10).
- Modify: `hermes_cli/kanban/store.py` — `kanban_store()` returns `PostgresKanbanStore` for `backend=="postgres"` (Task 8).
- Modify: `tests/hermes_cli/kanban/conftest.py` — add env-gated `postgres` param + docker fixture (Task 1).
- Modify: `tests/hermes_cli/kanban/test_store_conformance.py` — append complete/runs/events/claim tests (Tasks 9, 10).
- Modify: `requirements`/deps — add `psycopg[binary,pool]` (Task 1).
- Modify: `hermes_cli/kanban_writer_daemon.py` — add `"claim_task"` to `OP_ALLOWLIST` (Task 10).

---

### Task 1: Dependency + env-gated docker-Postgres conformance fixture

**Files:**
- Modify: dependency manifest (find it: `requirements.txt` / `pyproject.toml` / `setup.py` — `grep -rl "psycopg2\|^anthropic" ...`); add `psycopg[binary,pool]>=3.1`.
- Modify: `tests/hermes_cli/kanban/conftest.py`

- [ ] **Step 1: Install psycopg 3 into the venv**

```bash
/Users/ctao/.hermes/hermes-agent/venv/bin/pip install "psycopg[binary,pool]>=3.1"
```
Add `psycopg[binary,pool]>=3.1` to the project's dependency manifest (wherever `psycopg2` / other deps are declared). Verify: `venv/bin/python -c "import psycopg, psycopg_pool; print(psycopg.__version__)"`.

- [ ] **Step 2: Add the `postgres` backend param + docker fixture to conftest**

The current `conftest.py` has `_BACKENDS = ["sqlite"]` and a `store` fixture. Replace it with the version below. The `postgres` param is **gated**: it is skipped unless `HERMES_PG_TEST_DSN` is set OR docker is available (the fixture starts a throwaway container and tears it down). A **session-scoped** fixture owns the container so it starts once for the whole conformance run.

```python
# tests/hermes_cli/kanban/conftest.py
import os
import shutil
import subprocess
import time
import uuid

import pytest

_BACKENDS = ["sqlite"]
if os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker"):
    _BACKENDS.append("postgres")


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], check=True,
                       capture_output=True, timeout=15)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def _pg_dsn():
    """Session-wide Postgres DSN. Uses HERMES_PG_TEST_DSN if set; else starts a
    throwaway docker postgres:16 container and tears it down at session end."""
    dsn = os.environ.get("HERMES_PG_TEST_DSN")
    if dsn:
        yield dsn
        return
    if not _docker_available():
        pytest.skip("docker not available and HERMES_PG_TEST_DSN unset")
    name = f"hermes-kanban-pgtest-{uuid.uuid4().hex[:8]}"
    port = 0
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name,
             "-e", "POSTGRES_PASSWORD=postgres",
             "-e", "POSTGRES_DB=kanban",
             "-P", "postgres:16-alpine"],
            check=True, capture_output=True, timeout=120,
        )
        # Resolve the mapped host port for 5432.
        out = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        port = int(out.rsplit(":", 1)[-1])
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/kanban"
        # Wait for readiness (psycopg connect retry).
        import psycopg
        deadline = 60
        waited = 0
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3):
                    break
            except Exception:
                if waited >= deadline:
                    raise
                time.sleep(1.0)
                waited += 1
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, timeout=30)


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path, monkeypatch):
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
    elif backend == "postgres":
        dsn = request.getfixturevalue("_pg_dsn")
        from hermes_cli.kanban import pg_pool
        from hermes_cli.kanban.store_postgres import PostgresKanbanStore
        # Fresh schema per test for isolation: use a unique board namespace so
        # tests don't see each other's rows (cheap with the board-column model).
        board = f"test_{uuid.uuid4().hex[:8]}"
        pool = pg_pool.make_pool(dsn)
        pg_pool.ensure_schema(pool)        # idempotent; applies pg_schema.sql once
        s = PostgresKanbanStore(board=board, pool=pool)
        try:
            yield s
        finally:
            s.close()
            pool.close()
    else:
        pytest.skip(f"backend {backend} not available")
```

> Per-test board namespacing (`board=f"test_<uuid>"`) gives row isolation without recreating the schema each test — `ensure_schema` is idempotent (`CREATE TABLE IF NOT EXISTS`). `make_pool`/`ensure_schema`/`PostgresKanbanStore(pool=...)` are defined in Tasks 3–4.

- [ ] **Step 3: Run conformance to verify postgres params now ERROR (red)**

Run: `PYTHONPATH=… venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py -q`
Expected: sqlite params PASS; `postgres` params ERROR at fixture setup (`ModuleNotFoundError: hermes_cli.kanban.pg_pool` / `store_postgres`). This confirms the harness is wired.

- [ ] **Step 4: Commit**

```bash
git add tests/hermes_cli/kanban/conftest.py <deps-manifest>
git commit -m "test(kanban): add psycopg3 dep + env-gated docker-postgres conformance backend"
```

---

### Task 2: `pg_schema.sql` — Postgres DDL

**Files:**
- Create: `hermes_cli/kanban/pg_schema.sql`

- [ ] **Step 1: Write the DDL**

Translate `kanban_db.SCHEMA_SQL` (10 tables; NO metrics tables — those live in a separate sidecar). Rules: `t_…` ids stay `TEXT`; epoch timestamps stay `BIGINT`; autoincrement integer PKs (`task_comments.id`, `task_events.id`, `task_runs.id`, `kanban_profile_wake_events.id`) → `BIGINT GENERATED BY DEFAULT AS IDENTITY`; JSON text columns (`task_events.payload`, `task_runs.metadata`, and JSON-bearing `event_kinds`/`skills` — keep `skills`/`event_kinds` as `TEXT` to match the Python JSON-string handling, or `JSONB`; **use `JSONB` only for `task_events.payload` and `task_runs.metadata`**, keep `skills`/`event_kinds` `TEXT` so the existing JSON-string parse/serialize in the store works identically). **Add `board TEXT NOT NULL DEFAULT 'default'` to every table** and fold it into PKs/unique constraints + the lookup indexes.

```sql
-- hermes_cli/kanban/pg_schema.sql
-- Postgres DDL for the kanban board (Phase 2). Translated from
-- hermes_cli/kanban_db.py SCHEMA_SQL. Every table carries a `board` column
-- (default 'default'); board is part of each natural key.

CREATE TABLE IF NOT EXISTS tasks (
    board                TEXT    NOT NULL DEFAULT 'default',
    id                   TEXT    NOT NULL,
    title                TEXT    NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT    NOT NULL,
    priority             INTEGER NOT NULL DEFAULT 0,
    created_by           TEXT,
    created_at           BIGINT  NOT NULL,
    started_at           BIGINT,
    completed_at         BIGINT,
    workspace_kind       TEXT    NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    claim_lock           TEXT,
    claim_expires        BIGINT,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    BIGINT,
    current_run_id       BIGINT,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    skills               TEXT,
    model_override       TEXT,
    max_retries          INTEGER,
    session_id           TEXT,
    PRIMARY KEY (board, id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(board, assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(board, status);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant          ON tasks(board, tenant);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency     ON tasks(board, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_tasks_session_id      ON tasks(board, session_id);

CREATE TABLE IF NOT EXISTS task_links (
    board         TEXT NOT NULL DEFAULT 'default',
    parent_id     TEXT NOT NULL,
    child_id      TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'dependency',
    PRIMARY KEY (board, parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS idx_links_child  ON task_links(board, child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent ON task_links(board, parent_id);
CREATE INDEX IF NOT EXISTS idx_links_relation_child  ON task_links(board, relation_type, child_id);
CREATE INDEX IF NOT EXISTS idx_links_relation_parent ON task_links(board, relation_type, parent_id);

CREATE TABLE IF NOT EXISTS task_comments (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY,
    board      TEXT   NOT NULL DEFAULT 'default',
    task_id    TEXT   NOT NULL,
    author     TEXT   NOT NULL,
    body       TEXT   NOT NULL,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_comments_task ON task_comments(board, task_id, created_at);

CREATE TABLE IF NOT EXISTS task_events (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY,
    board      TEXT   NOT NULL DEFAULT 'default',
    task_id    TEXT   NOT NULL,
    run_id     BIGINT,
    kind       TEXT   NOT NULL,
    payload    JSONB,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(board, task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_run  ON task_events(run_id, id);

CREATE TABLE IF NOT EXISTS task_runs (
    id                  BIGINT GENERATED BY DEFAULT AS IDENTITY,
    board               TEXT   NOT NULL DEFAULT 'default',
    task_id             TEXT   NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT   NOT NULL,
    claim_lock          TEXT,
    claim_expires       BIGINT,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   BIGINT,
    started_at          BIGINT NOT NULL,
    ended_at            BIGINT,
    outcome             TEXT,
    summary             TEXT,
    metadata            JSONB,
    error               TEXT,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_runs_task   ON task_runs(board, task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON task_runs(board, status);

CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    board            TEXT   NOT NULL DEFAULT 'default',
    task_id          TEXT   NOT NULL,
    platform         TEXT   NOT NULL,
    chat_id          TEXT   NOT NULL,
    thread_id        TEXT   NOT NULL DEFAULT '',
    user_id          TEXT,
    notifier_profile TEXT,
    created_at       BIGINT NOT NULL,
    last_event_id    BIGINT NOT NULL DEFAULT 0,
    event_kinds      TEXT,
    include_children INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (board, task_id, platform, chat_id, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_notify_task ON kanban_notify_subs(board, task_id);

CREATE TABLE IF NOT EXISTS kanban_profile_event_subs (
    board              TEXT   NOT NULL DEFAULT 'default',
    task_id            TEXT   NOT NULL,
    profile            TEXT   NOT NULL,
    name               TEXT   NOT NULL DEFAULT '',
    event_kinds        TEXT,
    include_children   INTEGER NOT NULL DEFAULT 0,
    wake_agent         INTEGER NOT NULL DEFAULT 1,
    wake_prompt        TEXT,
    created_at         BIGINT NOT NULL,
    last_event_id      BIGINT NOT NULL DEFAULT 0,
    last_wake_at       BIGINT,
    last_wake_error_at BIGINT,
    last_wake_error    TEXT,
    wake_failure_count INTEGER NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (board, task_id, profile, name)
);
CREATE INDEX IF NOT EXISTS idx_profile_event_subs_task    ON kanban_profile_event_subs(board, task_id);
CREATE INDEX IF NOT EXISTS idx_profile_event_subs_profile ON kanban_profile_event_subs(board, profile);

CREATE TABLE IF NOT EXISTS kanban_profile_event_claims (
    board        TEXT   NOT NULL DEFAULT 'default',
    event_id     BIGINT NOT NULL,
    profile      TEXT   NOT NULL,
    name         TEXT   NOT NULL DEFAULT '',
    root_task_id TEXT   NOT NULL,
    claimed_at   BIGINT NOT NULL,
    PRIMARY KEY (board, event_id, profile, name)
);
CREATE INDEX IF NOT EXISTS idx_profile_event_claims_root
    ON kanban_profile_event_claims(board, root_task_id, claimed_at);

CREATE TABLE IF NOT EXISTS kanban_profile_wake_events (
    id                   BIGINT GENERATED BY DEFAULT AS IDENTITY,
    board                TEXT   NOT NULL DEFAULT 'default',
    task_id              TEXT   NOT NULL,
    profile              TEXT   NOT NULL,
    name                 TEXT   NOT NULL DEFAULT '',
    status               TEXT   NOT NULL,
    error                TEXT,
    claimed_event_cursor BIGINT NOT NULL DEFAULT 0,
    created_at           BIGINT NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_profile_wake_events_task
    ON kanban_profile_wake_events(board, task_id, profile, name);

CREATE TABLE IF NOT EXISTS kanban_notifier_heartbeats (
    board            TEXT   NOT NULL DEFAULT 'default',
    notifier_id      TEXT   NOT NULL,
    board_slug       TEXT   NOT NULL,
    db_path          TEXT   NOT NULL,
    notifier_profile TEXT,
    host             TEXT   NOT NULL,
    pid              INTEGER NOT NULL,
    started_at       BIGINT NOT NULL,
    last_seen_at     BIGINT NOT NULL,
    PRIMARY KEY (board, notifier_id, board_slug, db_path)
);
CREATE INDEX IF NOT EXISTS idx_notifier_heartbeats_seen ON kanban_notifier_heartbeats(last_seen_at);
```

> Note: `kanban_notifier_heartbeats` / `record_notifier_heartbeat` / `list_notifier_heartbeats` are sidecar-backed in `kanban_db` and ignore `conn`. The Postgres backend will keep these as table-backed for parity, but they are NOT exercised by the conformance suite — implement them straightforwardly (or defer with `NotImplementedError("phase-2-tail")` if not reached by tests). Decide during Task 9.

- [ ] **Step 2: Commit**

```bash
git add hermes_cli/kanban/pg_schema.sql
git commit -m "feat(kanban): pg_schema.sql — Postgres DDL with board column"
```

---

### Task 3: `pg_pool.py` — psycopg 3 pool + DSN + schema bootstrap

**Files:**
- Create: `hermes_cli/kanban/pg_pool.py`

- [ ] **Step 1: Implement the pool module**

```python
# hermes_cli/kanban/pg_pool.py
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from psycopg_pool import ConnectionPool

_SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")

# Process-wide pools keyed by DSN so callers in one process share connections
# (Supabase transaction pooler handles cross-process fan-out).
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()


def resolve_dsn() -> str:
    """Resolve the Postgres DSN for the kanban board.

    Order: HERMES_KANBAN_PG_DSN env → config kanban.postgres.dsn → raise.
    (Supabase: use the transaction-pooler connection string. No LISTEN/NOTIFY
    is used, so transaction-mode pooling is safe.)
    """
    dsn = os.environ.get("HERMES_KANBAN_PG_DSN")
    if dsn:
        return dsn
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        pg = ((cfg.get("kanban") or {}).get("postgres") or {})
        dsn = pg.get("dsn")
    except Exception:
        dsn = None
    if not dsn:
        raise RuntimeError(
            "kanban backend=postgres but no DSN configured "
            "(set HERMES_KANBAN_PG_DSN or kanban.postgres.dsn)"
        )
    return dsn


def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    """Create a bounded psycopg ConnectionPool. autocommit=True: each store op
    manages its own transaction explicitly via `with conn.transaction():`."""
    return ConnectionPool(
        conninfo=dsn, min_size=min_size, max_size=max_size,
        kwargs={"autocommit": True}, open=True,
    )


def get_pool(dsn: Optional[str] = None) -> ConnectionPool:
    """Return the shared pool for a DSN (resolving from config/env if omitted)."""
    dsn = dsn or resolve_dsn()
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = make_pool(dsn)
            _POOLS[dsn] = pool
    return pool


def ensure_schema(pool: ConnectionPool) -> None:
    """Apply pg_schema.sql once per pool (idempotent CREATE ... IF NOT EXISTS)."""
    key = str(id(pool))
    if key in _SCHEMA_DONE:
        return
    ddl = _SCHEMA_PATH.read_text()
    with pool.connection() as conn:
        conn.execute(ddl)
    _SCHEMA_DONE.add(key)
```

- [ ] **Step 2: Smoke test against docker (manual, optional)**

```bash
# with a docker postgres running and HERMES_PG_TEST_DSN exported:
venv/bin/python -c "from hermes_cli.kanban import pg_pool; p=pg_pool.make_pool('$HERMES_PG_TEST_DSN'); pg_pool.ensure_schema(p); print('schema OK'); p.close()"
```

- [ ] **Step 3: Commit**

```bash
git add hermes_cli/kanban/pg_pool.py
git commit -m "feat(kanban): pg_pool.py — psycopg3 pool, DSN resolution, schema bootstrap"
```

---

### Task 4: `PostgresKanbanStore` — skeleton + helpers + task CRUD

**Files:**
- Create: `hermes_cli/kanban/store_postgres.py`

**Implementation conventions for ALL methods (read once):**
- Constructor: `PostgresKanbanStore(board=None, pool=None)`. `self.board = board or "default"`. `self._pool = pool or pg_pool.get_pool()`. `close()` is a no-op (the pool is shared/owned by the caller); do NOT close the shared pool in `close()`.
- Every query goes through a pooled connection: `with self._pool.connection() as conn: ...`. The pool is `autocommit=True`; wrap multi-statement mutations in `with conn.transaction():` for atomicity.
- **Always** filter/set `board = self.board`. Use psycopg `%s` placeholders and pass params as tuples — NEVER f-string interpolate values.
- Timestamps: `now = int(time.time())`.
- Row→dataclass: build `Task`/`Run`/`Event`/`Comment` from named columns (use `psycopg.rows.dict_row` row factory: `with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:`). `skills` column is a JSON string → `json.loads` (or None); `task_events.payload`/`task_runs.metadata` are `JSONB` → already dict via psycopg (do NOT json.loads a dict).
- Event emission: a private `_emit(cur, task_id, kind, payload=None, run_id=None)` that `INSERT INTO task_events (board, task_id, run_id, kind, payload, created_at) VALUES (%s,%s,%s,%s,%s,%s)` with `payload` passed as `psycopg.types.json.Jsonb(payload)` (or None).
- Deferred/intricate methods raise `NotImplementedError("phase-2-tail: <name>")`.

- [ ] **Step 1: Skeleton + helpers + `create_task`/`get_task`/`list_tasks`**

```python
# hermes_cli/kanban/store_postgres.py
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hermes_cli.kanban_db import Task, Run, Event, Comment  # reuse dataclasses
from hermes_cli.kanban import pg_pool

_VALID_INITIAL_STATUSES = {"running", "blocked", "scheduled"}
_DEFAULT_NOTIFY_TERMINAL_KINDS = ("completed", "blocked", "gave_up",
                                  "crashed", "timed_out", "archived")


def _new_task_id() -> str:
    return "t_" + secrets.token_hex(4)


class PostgresKanbanStore:
    """KanbanStore backed by Postgres (psycopg 3). Fresh implementation of the
    conformance-covered surface; board captured at construction. NOT a delegating
    adapter. Intricate kanban_db semantics are deferred (raise NotImplementedError)."""

    def __init__(self, board: Optional[str] = None, pool=None):
        self.board = board or "default"
        self._pool = pool or pg_pool.get_pool()

    def close(self) -> None:
        return None  # shared pool; owner closes it

    # --- helpers ---------------------------------------------------------
    def _conn(self):
        return self._pool.connection()

    def _emit(self, cur, task_id: str, kind: str, payload=None, run_id=None) -> None:
        cur.execute(
            "INSERT INTO task_events (board, task_id, run_id, kind, payload, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (self.board, task_id, run_id, kind,
             Jsonb(payload) if payload is not None else None, int(time.time())),
        )

    def _row_to_task(self, row: dict) -> Task:
        d = dict(row)
        d.pop("board", None)
        sk = d.get("skills")
        if isinstance(sk, str):
            try:
                d["skills"] = json.loads(sk)
            except Exception:
                d["skills"] = None
        return Task(**{k: d.get(k) for k in Task.__dataclass_fields__})

    # --- task CRUD -------------------------------------------------------
    def create_task(self, *, title, body=None, assignee=None, created_by=None,
                    workspace_kind="scratch", workspace_path=None, branch_name=None,
                    tenant=None, priority=0, parents=(), triage=False,
                    idempotency_key=None, max_runtime_seconds=None, skills=None,
                    max_retries=None, initial_status="running", session_id=None,
                    **_ignored: Any) -> str:
        now = int(time.time())
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                if idempotency_key:
                    cur.execute(
                        "SELECT id FROM tasks WHERE board=%s AND idempotency_key=%s "
                        "AND status != 'archived'", (self.board, idempotency_key))
                    hit = cur.fetchone()
                    if hit:
                        return hit["id"]
                # status resolution (mirror kanban_db.create_task)
                if initial_status in ("blocked", "scheduled"):
                    status = initial_status
                elif triage:
                    status = "triage"
                else:
                    status = "ready"
                    if parents:
                        cur.execute(
                            "SELECT 1 FROM tasks WHERE board=%s AND id = ANY(%s) "
                            "AND status != 'done' LIMIT 1",
                            (self.board, list(parents)))
                        if cur.fetchone():
                            status = "todo"
                tid = _new_task_id()
                cur.execute(
                    "INSERT INTO tasks (board, id, title, body, assignee, status, "
                    "priority, created_by, created_at, workspace_kind, workspace_path, "
                    "branch_name, tenant, idempotency_key, max_runtime_seconds, skills, "
                    "max_retries, session_id) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (self.board, tid, title, body, assignee, status, priority,
                     created_by, now, workspace_kind, workspace_path, branch_name,
                     tenant, idempotency_key, max_runtime_seconds,
                     json.dumps(skills) if skills else None, max_retries, session_id))
                for p in parents:
                    cur.execute(
                        "INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                        "VALUES (%s,%s,%s,'dependency') ON CONFLICT DO NOTHING",
                        (self.board, p, tid))
                self._emit(cur, tid, "created", {
                    "assignee": assignee, "status": status,
                    "parents": list(parents), "tenant": tenant,
                    "branch_name": branch_name, "skills": skills})
                if status == "blocked":
                    self._emit(cur, tid, "blocked", {"reason": "initial_status=blocked"})
            return tid

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM tasks WHERE board=%s AND id=%s",
                        (self.board, task_id))
            row = cur.fetchone()
            return self._row_to_task(row) if row else None

    def list_tasks(self, *, assignee=None, status=None, tenant=None, session_id=None,
                   include_archived=False, limit=None, order_by=None,
                   workflow_template_id=None, current_step_key=None,
                   **_ignored: Any) -> list[Task]:
        clauses = ["board=%s"]
        params: list[Any] = [self.board]
        for col, val in (("assignee", assignee), ("status", status),
                         ("tenant", tenant), ("session_id", session_id),
                         ("workflow_template_id", workflow_template_id),
                         ("current_step_key", current_step_key)):
            if val is not None:
                clauses.append(f"{col}=%s")
                params.append(val)
        if not include_archived and status != "archived":
            clauses.append("status != 'archived'")
        sql = ("SELECT * FROM tasks WHERE " + " AND ".join(clauses) +
               " ORDER BY priority DESC, created_at ASC")
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [self._row_to_task(r) for r in cur.fetchall()]
```

- [ ] **Step 2: Run the two CRUD conformance tests against postgres**

Run: `PYTHONPATH=… venv/bin/python -m pytest "tests/hermes_cli/kanban/test_store_conformance.py::test_create_then_get[postgres]" "tests/hermes_cli/kanban/test_store_conformance.py::test_get_missing_returns_none[postgres]" -q`
Expected: PASS. (sqlite params already pass.)

- [ ] **Step 3: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py
git commit -m "feat(kanban): PostgresKanbanStore skeleton + task CRUD"
```

---

### Task 5: `PostgresKanbanStore` — status transitions + recompute + has_spawnable_ready

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py`

Implement these methods to make `test_block_unblock_roundtrip`, `test_priority_and_edit_and_status_direct`, `test_reassign_and_delete`, and `test_promote_task_is_callable` pass. Per-method exact target behavior (translate the SQLite semantics from `kanban_db`; emit the listed event):

- [ ] **Step 1: Implement the methods**

| Method | Behavior (Postgres) | Event | Returns |
|---|---|---|---|
| `block_task(task_id, *, reason=None, expected_run_id=None)` | `UPDATE tasks SET status='blocked', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE board=%s AND id=%s AND status IN ('running','ready')`. If a `current_run_id` is set, close that run (`UPDATE task_runs SET status='blocked', outcome='blocked', ended_at=%s WHERE id=...`) and clear `current_run_id`. | `"blocked"` payload `{"reason": reason}` | `rowcount==1` |
| `unblock_task(task_id)` | Re-gate on parents: compute target = `'ready'` unless any `dependency` parent has `status != 'done'` → `'todo'`. `UPDATE tasks SET status=<target>, current_run_id=NULL, consecutive_failures=0, last_failure_error=NULL WHERE board=%s AND id=%s AND status IN ('blocked','scheduled')`. | `"unblocked"` payload `{"status":"todo"}` if todo else `None` | `rowcount==1` |
| `schedule_task(task_id, *, reason=None, expected_run_id=None)` | `UPDATE ... SET status='scheduled', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE board AND id AND status IN ('todo','ready','running','blocked')`; close active run outcome='scheduled' if present. | `"scheduled"` `{"reason":reason}` | `rowcount==1` |
| `archive_task(task_id)` | `UPDATE ... SET status='archived', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE board AND id AND status != 'archived'`; close active run outcome='reclaimed'. Then `self.recompute_ready()`. | `"archived"` | `rowcount==1` |
| `assign_task(task_id, profile)` | If task `status='running' AND claim_lock IS NOT NULL` → raise `RuntimeError`. `UPDATE ... SET assignee=%s, consecutive_failures=0, last_failure_error=NULL WHERE board AND id`. | `"assigned"` `{"assignee":profile}` | `rowcount==1` |
| `reassign_task(task_id, profile, *, reclaim_first=False, reason=None)` | If `reclaim_first`: `self.reclaim_task(task_id, reason=reason)` first. If task currently running and not reclaim_first → return False. Else `self.assign_task(task_id, profile)`. | (delegated) | bool |
| `reclaim_task(task_id, *, reason=None)` | `UPDATE ... SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, consecutive_failures=0 WHERE board AND id AND (status='running' OR claim_lock IS NOT NULL)`; close active run outcome='reclaimed'; clear current_run_id. | `"reclaimed"` `{"manual":True,"reason":reason}` | `rowcount==1` |
| `set_status_direct(task_id, new_status)` | If `new_status='ready'` and any dependency parent `status != 'done'` → return False. `UPDATE ... SET status=%s, claim_lock=(CASE WHEN %s='running' THEN claim_lock ELSE NULL END), claim_expires=(…), worker_pid=(…) WHERE board AND id`. If `new_status in ('done','ready')` afterwards → `self.recompute_ready()`. | `"status"` `{"status":new_status}` | `rowcount==1` |
| `set_task_priority(task_id, priority)` | `UPDATE ... SET priority=%s WHERE board AND id`. | `"reprioritized"` `{"priority":priority}` | `rowcount==1` |
| `edit_task_fields(task_id, *, title=None, body=None)` | If both None → False. If `title` given and blank → raise ValueError. Build dynamic SET for provided fields; `UPDATE ... WHERE board AND id`. | `"edited"` (payload None) | `rowcount==1` |
| `delete_task(task_id)` | In one transaction: DELETE from task_links/task_comments/task_events/task_runs/kanban_notify_subs WHERE board AND (task_id or parent/child) = id, then DELETE FROM tasks WHERE board AND id. Then `self.recompute_ready()`. | none | `tasks rowcount==1` |
| `promote_task(task_id, *, actor, reason=None, force=False, dry_run=False)` | If task missing → `(False, "task <id> not found")`. Else if status not in ('todo','blocked') → `(False, "<reason>")`. Else if not force and any dependency parent status not in ('done','archived') → `(False, "blocked by parent ...")`. Else (not dry_run) `UPDATE ... SET status='ready' ...` + emit. | `"promoted_manual"` `{actor,reason,forced:force}` | `tuple[bool, Optional[str]]` |
| `recompute_ready()` | For every task with `status IN ('todo','blocked')` on this board: if it's `blocked` skip when a sticky-block marker exists (Phase-2 simplification: treat a task as sticky-blocked if its most-recent `blocked` event has no later `unblocked`; if implementing the full sticky rule is unclear, scope to: promote only `todo` tasks whose all dependency parents are done/archived, and leave `blocked` tasks alone — **document this simplification**). Promote eligible tasks: `UPDATE tasks SET status='ready', consecutive_failures=0, last_failure_error=NULL WHERE board AND id AND status IN ('todo','blocked')`; emit `"promoted"` (payload None) per promoted task. | `"promoted"` per task | `int` promoted count |
| `has_spawnable_ready()` | `SELECT DISTINCT assignee FROM tasks WHERE board=%s AND status='ready' AND assignee IS NOT NULL AND claim_lock IS NULL`; return True if any assignee maps to a real profile via `kanban_db.profile_exists`-equivalent. For Phase 2 conformance (which only asserts the return is a bool), returning `bool(rows)` is acceptable — **document that on-disk profile validation is deferred**. | none | `bool` |

> `recompute_ready` and `has_spawnable_ready` carry small, explicitly-documented Phase-2 simplifications (the conformance suite only asserts types/basic promotion, not the full sticky-block / profile-on-disk rules). Anything beyond the conformance assertions that you cannot fully replicate must be commented as a deferral, not silently approximated in a way that could mislead.

- [ ] **Step 2: Run the lifecycle conformance tests vs postgres**

Run: `pytest "tests/hermes_cli/kanban/test_store_conformance.py" -k "postgres and (block_unblock or priority or reassign or promote)" -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py
git commit -m "feat(kanban): PostgresKanbanStore status transitions + recompute_ready"
```

---

### Task 6: `PostgresKanbanStore` — links, comments, events, gc, workspace

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py`

Make `test_link_unlink_and_parents_children`, `test_comment_roundtrip`, `test_gc_events_returns_int`, `test_set_workspace_path` pass.

- [ ] **Step 1: Implement**

| Method | Behavior | Event | Returns |
|---|---|---|---|
| `link_tasks(parent_id, child_id, *, relation_type='dependency')` | Validate both ids exist (raise ValueError if not). `INSERT INTO task_links (board,parent_id,child_id,relation_type) VALUES (...) ON CONFLICT DO NOTHING`. For `dependency`: if parent `status != 'done'`, `UPDATE tasks SET status='todo' WHERE board AND id=child AND status='ready'`. (Cycle detection: Phase-2 may skip the DFS cycle check — **document** the deferral; conformance doesn't exercise cycles.) | `"linked"` on child `{parent,child,relation_type}` | None |
| `unlink_tasks(parent_id, child_id, *, relation_type='dependency')` | `DELETE FROM task_links WHERE board AND parent_id AND child_id AND relation_type`. If deleted and dependency → `self.recompute_ready()`. | `"unlinked"` on child | `rowcount>0` |
| `parent_ids(task_id, *, relation_type='dependency')` | `SELECT parent_id FROM task_links WHERE board AND child_id=%s [AND relation_type=%s] ORDER BY parent_id`. `relation_type=None` → all. | — | `list[str]` |
| `child_ids(task_id, *, relation_type='dependency')` | symmetric (`WHERE parent_id=%s` → `child_id`). | — | `list[str]` |
| `add_comment(task_id, *, author, body)` | Validate task exists + non-empty. `INSERT INTO task_comments (board,task_id,author,body,created_at) VALUES (...) RETURNING id`. | `"commented"` `{author, len:len(body)}` | `int` id |
| `list_comments(task_id)` | `SELECT * ... WHERE board AND task_id ORDER BY created_at ASC` → `Comment(**…)` (drop `board`). | — | `list[Comment]` |
| `list_events(task_id, **kw)` | `SELECT * FROM task_events WHERE board AND task_id ORDER BY created_at ASC, id ASC` → `Event(...)`. `payload` is JSONB → already dict. | — | `list[Event]` |
| `gc_events(*, older_than_seconds=30*24*3600)` | `DELETE FROM task_events WHERE board=%s AND created_at < %s AND task_id IN (SELECT id FROM tasks WHERE board=%s AND status IN ('done','archived'))`. | — | `int` rowcount |
| `set_workspace_path(task_id, path)` | `UPDATE tasks SET workspace_path=%s WHERE board AND id`. | — | None |

- [ ] **Step 2: Run those conformance tests vs postgres** → PASS.

Run: `pytest tests/hermes_cli/kanban/test_store_conformance.py -k "postgres and (link_unlink or comment or gc_events or workspace)" -q`

- [ ] **Step 3: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py
git commit -m "feat(kanban): PostgresKanbanStore links/comments/events/gc/workspace"
```

---

### Task 7: `PostgresKanbanStore` — notify subs + profile-event subs

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py`

Make `test_notify_sub_add_list_remove` and `test_profile_sub_and_recompute` pass.

- [ ] **Step 1: Implement**

| Method | Behavior | Returns |
|---|---|---|
| `add_notify_sub(*, task_id, platform, chat_id, thread_id=None, user_id=None, notifier_profile=None, event_kinds=None, include_children=None)` | `thread_id = thread_id or ''`. `INSERT INTO kanban_notify_subs (board,task_id,platform,chat_id,thread_id,user_id,notifier_profile,created_at,last_event_id,event_kinds,include_children) VALUES (...,0,...) ON CONFLICT (board,task_id,platform,chat_id,thread_id) DO NOTHING`. Then selective UPDATE: backfill `notifier_profile` only if NULL; set `event_kinds=json.dumps(list)` if caller passed a list; set `include_children` if non-None. | None |
| `remove_notify_sub(*, task_id, platform, chat_id, thread_id=None)` | `DELETE ... WHERE board AND task_id AND platform AND chat_id AND thread_id=COALESCE(%s,'')`. | `rowcount>0` |
| `list_notify_subs(task_id=None)` | `SELECT * FROM kanban_notify_subs WHERE board=%s [AND task_id=%s]` → `list[dict]` (use dict_row; drop or keep `board` — tests check `task_id`). | `list[dict]` |
| `claim_unseen_events_for_sub(...)` | Phase-2: implement (see Task 9) or `NotImplementedError("phase-2-tail")` until Task 9. | tuple |
| `add_profile_event_sub(*, task_id, profile, name='', event_kinds=_UNSET, include_children=None, wake_agent=None, wake_prompt=_UNSET, enabled=None)` | `INSERT INTO kanban_profile_event_subs (board,task_id,profile,name,created_at,last_event_id) VALUES (...,0) ON CONFLICT (board,task_id,profile,name) DO NOTHING`. Then selective UPDATE only for explicitly-passed fields (use a module `_UNSET = object()` sentinel to distinguish unset from None). | None |
| `remove_profile_event_sub(*, task_id, profile, name='')` | `DELETE ... WHERE board AND task_id AND profile AND name`. | `rowcount>0` |
| `list_profile_event_subs(*, task_id=None, profile=None, enabled_only=True)` | `SELECT * ... WHERE board [AND task_id] [AND profile] [AND enabled=1 if enabled_only] ORDER BY created_at ASC` → `list[dict]`. | `list[dict]` |

- [ ] **Step 2: Run those conformance tests vs postgres** → PASS.

Run: `pytest tests/hermes_cli/kanban/test_store_conformance.py -k "postgres and (notify_sub or profile_sub)" -q`

- [ ] **Step 3: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py
git commit -m "feat(kanban): PostgresKanbanStore notify + profile-event subs"
```

---

### Task 8: Factory wiring + full existing conformance green vs both backends

**Files:**
- Modify: `hermes_cli/kanban/store.py`

- [ ] **Step 1: Wire the factory**

Replace the `raise NotImplementedError` postgres branch in `kanban_store()`:
```python
def kanban_store(board: Optional[str] = None) -> "KanbanStore":
    backend = resolve_backend()
    if backend == "sqlite":
        from .store_sqlite import SqliteKanbanStore
        return SqliteKanbanStore(board=board)
    if backend == "postgres":
        from .store_postgres import PostgresKanbanStore
        return PostgresKanbanStore(board=board)   # pool from pg_pool.get_pool()
    raise NotImplementedError(f"kanban backend '{backend}' not available yet")
```

- [ ] **Step 2: Update the factory test**

`tests/hermes_cli/kanban/test_store_factory.py::test_factory_postgres_not_available_phase1` asserts postgres raises `NotImplementedError`. That premise is now obsolete. Replace it with a test that postgres backend returns a `PostgresKanbanStore` **without connecting** (monkeypatch `pg_pool.get_pool` to a sentinel so construction doesn't need a live DB):
```python
def test_factory_returns_postgres_store(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban import pg_pool
    monkeypatch.setattr(pg_pool, "get_pool", lambda dsn=None: object())
    from hermes_cli.kanban.store import kanban_store
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    s = kanban_store(board=None)
    assert isinstance(s, PostgresKanbanStore)
    s.close()
```

- [ ] **Step 3: Full conformance vs BOTH backends + factory tests**

Run: `pytest tests/hermes_cli/kanban/ -q`
Expected: every conformance test passes for `[sqlite]` AND `[postgres]`; factory tests pass.

- [ ] **Step 4: Commit**

```bash
git add hermes_cli/kanban/store.py tests/hermes_cli/kanban/test_store_factory.py
git commit -m "feat(kanban): kanban_store() returns PostgresKanbanStore for backend=postgres"
```

---

### Task 9: Extend conformance — complete_task (basic), runs reads, list_events, event-claiming

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py`
- Modify: `tests/hermes_cli/kanban/test_store_conformance.py`

These methods are already on the Protocol + SqliteKanbanStore; this task implements them in Postgres and adds conformance coverage that runs against BOTH backends (so the new tests double as a parity check on SQLite).

- [ ] **Step 1: Implement in PostgresKanbanStore**

- `complete_task(task_id, *, result=None, summary=None, metadata=None, created_cards=None, expected_run_id=None)` — **basic** semantics only: transition `running|ready|blocked|scheduled → done`; set `completed_at=now`, `result`; if a `current_run_id` exists close it (`status='done', outcome='completed', summary=COALESCE(summary,result), metadata, ended_at=now`) and clear `current_run_id`; emit `"completed"` payload `{}`; then `self.recompute_ready()`. Return `rowcount==1`. **DO NOT** implement hallucinated-cards / PR-head gates / closeout packets — if `created_cards` is non-empty, raise `NotImplementedError("phase-2-tail: complete_task created_cards gating")`.
- `list_runs(task_id, *, include_active=True, state_type=None, state_name=None)` — `SELECT * FROM task_runs WHERE board AND task_id [AND ended_at IS NOT NULL if not include_active] ORDER BY started_at ASC, id ASC` → `Run(...)` (metadata JSONB already dict). `state_type`/`state_name` filtering may raise `NotImplementedError("phase-2-tail")` if not exercised.
- `get_run(run_id)` — `SELECT * FROM task_runs WHERE board AND id=%s` → `Run` or None.
- `latest_run(task_id)` — `... ORDER BY started_at DESC, id DESC LIMIT 1`.
- `latest_summary(task_id)` — `SELECT summary ... WHERE board AND task_id AND summary IS NOT NULL AND summary<>'' ORDER BY COALESCE(ended_at,started_at) DESC, id DESC LIMIT 1` → str|None.
- `latest_summaries(task_ids)` — window-function query partitioned by task_id → `{task_id: summary}`.
- `claim_unseen_events_for_sub(*, task_id, platform, chat_id, thread_id=None, kinds=None, include_children=False)` — read `last_event_id` for the sub; `SELECT * FROM task_events WHERE board AND task_id=%s AND id > %s [AND kind = ANY(%s)] ORDER BY id ASC` (include_children: also children via `task_links`); inside `with conn.transaction():` CAS-advance `UPDATE kanban_notify_subs SET last_event_id=%s WHERE board AND ...keys AND last_event_id=%s`; return `(old_cursor, new_cursor, events)`. (Postgres MVCC + the CAS guard replaces the SQLite BEGIN IMMEDIATE dance.)
- `claim_unseen_events_for_profile_sub(*, task_id, profile, name='')` — same shape; per-event dedup via `INSERT INTO kanban_profile_event_claims (...) ON CONFLICT DO NOTHING` (only `rowcount==1` events returned); advance cursor over all scanned. Return `(old, new, claimed_events)`.
- `list_profile_wake_events`, `record_notifier_heartbeat`, `list_notifier_heartbeats`, `heartbeat_worker` — implement straightforwardly OR `NotImplementedError("phase-2-tail")` if not reached by the new tests. `board_stats`, `known_assignees` — implement basic versions (`board_stats`: counts grouped by status/assignee excluding archived + `now`; `known_assignees`: distinct non-archived assignees as `[{"name":..,"on_disk":False,"counts":{...}}]`, on_disk deferred). 

- [ ] **Step 2: Add conformance tests (run vs both backends)**

Append to `test_store_conformance.py`:
```python
def test_complete_basic_and_runs_and_summary(store):
    tid = store.create_task(title="x", assignee="engineer")
    # blocking with a reason synthesizes/closes a run on sqlite; on pg complete
    # closes current_run_id if any. Drive a completion and read it back.
    assert store.complete_task(tid, result="done-res", summary="sum") is True
    assert store.get_task(tid).status == "done"
    # latest_summary reflects the closing run summary when a run exists; if no
    # run was open, summary may be None — assert type, not exact value.
    s = store.latest_summary(tid)
    assert s is None or isinstance(s, str)
    assert isinstance(store.list_runs(tid), list)


def test_events_recorded_and_listed(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.set_task_priority(tid, 3)
    kinds = [e.kind for e in store.list_events(tid)]
    assert "created" in kinds and "reprioritized" in kinds


def test_notify_event_claiming_cursor(store):
    tid = store.create_task(title="x", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    # create some events
    store.add_comment(tid, author="ops", body="n1")
    old, new, evs = store.claim_unseen_events_for_sub(
        task_id=tid, platform="telegram", chat_id="c1")
    assert new >= old and isinstance(evs, list)
    # second claim sees nothing new
    old2, new2, evs2 = store.claim_unseen_events_for_sub(
        task_id=tid, platform="telegram", chat_id="c1")
    assert new2 == new and evs2 == []
```

> If any new test exposes a real SQLite-vs-Postgres divergence, STOP and report it — the test encodes intended parity. (E.g. if `complete_task` event payload differs; align the Postgres impl, do not weaken the test.)

- [ ] **Step 3: Run vs both backends** → PASS.

Run: `pytest tests/hermes_cli/kanban/test_store_conformance.py -q`

- [ ] **Step 4: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban): PostgresKanbanStore complete(basic)/runs/events/event-claiming + conformance"
```

---

### Task 10: `claim_task` (SKIP LOCKED) store primitive + conformance

**Files:**
- Modify: `hermes_cli/kanban/store.py` (Protocol), `hermes_cli/kanban/store_sqlite.py`, `hermes_cli/kanban/store_postgres.py`, `hermes_cli/kanban_writer_daemon.py`, `tests/hermes_cli/kanban/test_store_conformance.py`

This adds the design's headline atomic-claim primitive to the store interface (the dispatcher LOOP that calls it stays Phase-3 glue).

- [ ] **Step 1: Add to the Protocol** (`store.py`):
```python
    def claim_task(self, task_id: str, *, ttl_seconds: Optional[int] = None,
                   claimer: Optional[str] = None) -> Optional[Task]: ...
```

- [ ] **Step 2: SqliteKanbanStore** (`store_sqlite.py`): delegate as a write:
```python
    def claim_task(self, task_id: str, **kwargs: Any):
        return self._write("claim_task", task_id=task_id, **kwargs)
```
And add `"claim_task"` to `OP_ALLOWLIST` in `hermes_cli/kanban_writer_daemon.py` (verify absent first; add with a comment "dispatcher atomic ready→running claim").

- [ ] **Step 3: PostgresKanbanStore** — SKIP LOCKED claim:
```python
    def claim_task(self, task_id, *, ttl_seconds=None, claimer=None):
        now = int(time.time())
        ttl = int(ttl_seconds) if ttl_seconds else 900
        with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT id FROM tasks WHERE board=%s AND id=%s AND status='ready' "
                    "AND claim_lock IS NULL FOR UPDATE SKIP LOCKED",
                    (self.board, task_id))
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "INSERT INTO task_runs (board,task_id,profile,status,claim_lock,"
                    "claim_expires,max_runtime_seconds,started_at) "
                    "SELECT %s,%s,assignee,'running',%s,%s,max_runtime_seconds,%s "
                    "FROM tasks WHERE board=%s AND id=%s RETURNING id",
                    (self.board, task_id, claimer, now + ttl, now, self.board, task_id))
                run_id = cur.fetchone()["id"]
                cur.execute(
                    "UPDATE tasks SET status='running', claim_lock=%s, claim_expires=%s, "
                    "started_at=COALESCE(started_at,%s), current_run_id=%s "
                    "WHERE board=%s AND id=%s",
                    (claimer, now + ttl, now, run_id, self.board, task_id))
                self._emit(cur, task_id, "claimed", {"claimer": claimer}, run_id=run_id)
            return self.get_task(task_id)
```

- [ ] **Step 4: Conformance test** (both backends):
```python
def test_claim_task_atomic(store):
    tid = store.create_task(title="x", assignee="engineer")
    # ensure ready (create with no parents → ready)
    assert store.get_task(tid).status == "ready"
    t1 = store.claim_task(tid, claimer="w1")
    assert t1 is not None and t1.status == "running"
    # second claim of the same task fails (already claimed)
    assert store.claim_task(tid, claimer="w2") is None
```

- [ ] **Step 5: Run vs both backends + the existing single-writer/allowlist tests**

Run: `pytest tests/hermes_cli/kanban/ tests/hermes_cli/test_kanban_tools_write_session.py tests/hermes_cli/test_kanban_writer_daemon.py -q`
Expected: PASS (claim conformance green on both; allowlist change doesn't break writer tests).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban/store.py hermes_cli/kanban/store_sqlite.py hermes_cli/kanban/store_postgres.py hermes_cli/kanban_writer_daemon.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban): claim_task store primitive (SKIP LOCKED on pg) + conformance"
```

---

### Task 11: Phase-2 acceptance gate

**Files:** none (verification only).

- [ ] **Step 1: Full conformance vs both backends**

Run: `pytest tests/hermes_cli/kanban/ -q`
Expected: all green for `[sqlite]` and `[postgres]`.

- [ ] **Step 2: Existing sqlite suites stay green (zero behavior change on default backend)**

Run: `pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_tools_write_session.py tests/hermes_cli/test_kanban_notify.py tests/plugins/ tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_single_writer.py -q`
Expected: PASS (the only known pre-existing failures are the 3 in `test_kanban_core_functionality.py` documented in Phase 1 — outside this list).

- [ ] **Step 3: Confirm default backend is still sqlite + postgres degrades cleanly without a DSN**

Run: `venv/bin/python -c "from hermes_cli.kanban.store import resolve_backend; assert resolve_backend()=='sqlite'; print('default sqlite OK')"`
And confirm selecting postgres without a DSN raises a clear error (not a silent fallback): construct `PostgresKanbanStore` via the factory with backend=postgres and no DSN → `RuntimeError` from `resolve_dsn`.

- [ ] **Step 4: Phase-2 marker commit**

```bash
git commit --allow-empty -m "chore(kanban): Phase 2 complete — PostgresKanbanStore conformance-green vs docker Postgres (sqlite default unchanged)"
```

---

## Deferred to later phases (explicitly NOT in Phase 2)

- **Phase 3** — `kanban_glue.py`: extract dispatcher/notifier bodies from `gateway/run.py` to `run_dispatch_tick(store)` / `run_notifier_tick(store, adapters)`; make dispatch/notify backend-agnostic (the loop that calls `claim_task`).
- **Phase 4** — `migrate_sqlite_to_pg.py`: export/transform/load + dry-run + row-count/integrity verification.
- **Phase 5** — maintenance-window cutover (quiesce → final export → load → flip `kanban.backend=postgres` → restart → verify) + rollback window.
- **Phase 6** — retire SQLite life-support under `backend=postgres`; web dashboard (Supabase Auth/RLS/Realtime).
- **`phase-2-tail` markers**: every `NotImplementedError("phase-2-tail: …")` in `store_postgres.py` is a parity gap (complete_task gates/closeout, build_worker_context, full sticky-block/breaker semantics, profile-on-disk validation, state-keyed run filters). These must be closed before cutover (Phase 5) — track them.
