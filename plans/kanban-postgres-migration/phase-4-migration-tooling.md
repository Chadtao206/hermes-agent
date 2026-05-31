# Phase 4 — Kanban SQLite→Postgres Migration Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `hermes_cli/kanban/migrate_sqlite_to_pg.py` — a read-only SQLite→Postgres migrator with `--dry-run` and `--execute` modes, a full verification stack, and a human-driven cutover runbook — without changing the default backend or touching the live dispatch path.

**Architecture:** A standalone module reads the single SQLite board read-only, validates UTF-8/JSON, bulk-loads board-stamped rows into a target Postgres schema in one transaction, reseqs the four IDENTITY sequences, then verifies (row counts + referential-integrity SQL + id/sequence + `KanbanStore` parity + source-doctor precondition). `--dry-run` isolates in a throwaway schema it drops; `--execute` loads into `public` with a refuse-unless-`--force` guard. See `plans/kanban-postgres-migration/phase-4-design.md`.

**Tech Stack:** Python 3, `psycopg` 3 + `psycopg_pool`, `sqlite3` (stdlib, read-only URI), pytest with the docker-`postgres:16-alpine` conformance fixture (`tests/hermes_cli/kanban/conftest.py`).

---

## Pre-flight (executor)

- Worktree: `.worktrees/kanban-pg-phase4-migrate`, branch `feat/kanban-pg-phase4-migrate` off `main` @ `157d4dab3` (use superpowers:using-git-worktrees).
- **Test interpreter (mandatory):** `/Users/ctao/.hermes/hermes-agent/venv/bin/python` — it has `psycopg` + `pytest`; bare `python3` / `.venv` do NOT. Every test command below uses it. Run from the worktree root.
- Docker must be running (the `_pg_dsn` session fixture auto-starts `postgres:16-alpine`; set `HERMES_PG_TEST_DSN` to reuse an external PG).

## Reference facts (verified against the codebase 2026-05-30)

- **Migrated tables (9), parent-first load order:** `tasks`, `task_runs`, `task_events`, `task_comments`, `task_links`, `kanban_notify_subs`, `kanban_profile_event_subs`, `kanban_profile_event_claims`, `kanban_profile_wake_events`. **NOT migrated:** `kanban_notifier_heartbeats` (sidecar) and metrics snapshots (separate DB; not in `pg_schema.sql`).
- **IDENTITY tables (reseq targets):** `task_comments`, `task_events`, `task_runs`, `kanban_profile_wake_events` (PG PK is `id` alone; SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`).
- **JSON→JSONB columns:** `task_events.payload`, `task_runs.metadata`.
- **`pg_schema.sql`** every table has `board TEXT NOT NULL DEFAULT 'default'`; PKs are board-scoped EXCEPT the 4 IDENTITY tables (PK `id` only). No FK constraints anywhere.
- **`hermes_cli/kanban/pg_pool.py`:** `resolve_dsn()`, `make_pool(dsn, *, min_size=1, max_size=8)` (builds `ConnectionPool(kwargs={"autocommit": True})`), `get_pool()`, `ensure_schema(pool)`, `_SCHEMA_PATH`.
- **`hermes_cli/kanban_db.py`:** `list_boards(*, include_archived=True) -> list[dict]` (dicts have `"slug"`, `"db_path"`); `kanban_db_path(board)`; `connect(db_path=None, *, board=None, readonly=False, _bootstrap=False)`; `kanban_home()` honours `HERMES_KANBAN_HOME`.
- **`hermes_cli/kanban_board_doctor.py`:** `run_board_doctor(*, board=None, ready_age_seconds=900) -> dict` → `{"issues": [{"severity": "critical"|"error"|"warning", ...}], ...}`.
- **Stores:** `SqliteKanbanStore(board=None)` and `PostgresKanbanStore(board=None, pool=None)`; both `get_task(id) -> Optional[Task]` return the same `Task` dataclass (so `==` is valid parity). `list_tasks(**kwargs)`, `list_comments(task_id)`, `list_runs(task_id)`.
- **Test isolation idiom:** set `HERMES_KANBAN_HOME=<tmp_path>` so the default board lives at `<tmp>/kanban.db` and a second board at `<tmp>/kanban/boards/<slug>/kanban.db` (do NOT use `HERMES_KANBAN_DB` for multi-board tests — it pins ALL boards to one path).

---

## Task 1: `pg_pool` — `search_path` support + DDL accessor

The store-parity read must point a `PostgresKanbanStore` pool at the dry-run schema; the migrator must apply the schema DDL into an arbitrary target schema.

**Files:**
- Modify: `hermes_cli/kanban/pg_pool.py`
- Test: `tests/hermes_cli/kanban/test_pg_pool_searchpath.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_pg_pool_searchpath.py
from hermes_cli.kanban import pg_pool


def test_read_schema_ddl_has_tables():
    ddl = pg_pool.read_schema_ddl()
    assert "CREATE TABLE" in ddl and "tasks" in ddl


def test_make_pool_sets_search_path(_pg_dsn):
    pool = pg_pool.make_pool(_pg_dsn, search_path="pg_temp,public")
    try:
        with pool.connection() as conn:
            got = conn.execute("SHOW search_path").fetchone()[0]
        assert "public" in got
    finally:
        pool.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_pg_pool_searchpath.py -v`
Expected: FAIL — `read_schema_ddl` missing / `make_pool() got an unexpected keyword argument 'search_path'`.

- [ ] **Step 3: Implement the minimal change**

```python
# in hermes_cli/kanban/pg_pool.py

def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 8,
              search_path: Optional[str] = None) -> ConnectionPool:
    """Create a bounded psycopg ConnectionPool. autocommit=True: each store op
    manages its own transaction explicitly via `with conn.transaction():`.
    `search_path`, when given, pins every connection's schema search path
    (used by the migrator's dry-run parity read)."""
    kwargs: dict = {"autocommit": True}
    if search_path:
        kwargs["options"] = f"-c search_path={search_path}"
    return ConnectionPool(
        conninfo=dsn, min_size=min_size, max_size=max_size,
        kwargs=kwargs, open=True,
    )


def read_schema_ddl() -> str:
    """Return the kanban Postgres DDL text (pg_schema.sql)."""
    return _SCHEMA_PATH.read_text()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_pg_pool_searchpath.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/pg_pool.py tests/hermes_cli/kanban/test_pg_pool_searchpath.py
git commit -m "feat(kanban-pg): pg_pool search_path option + read_schema_ddl accessor"
```

---

## Task 2: Migrator scaffold — table metadata + `enumerate_boards` (single-board guard)

**Files:**
- Create: `hermes_cli/kanban/migrate_sqlite_to_pg.py`
- Test: `tests/hermes_cli/kanban/test_migrate_enumerate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_migrate_enumerate.py
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


def _bootstrap_default(home):
    """Create the default board DB at <home>/kanban.db."""
    kb.connect(db_path=home / "kanban.db", readonly=False, _bootstrap=True).close()


def test_enumerate_single_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    _bootstrap_default(tmp_path)
    assert m.enumerate_board(tmp_path) == "default"


def test_enumerate_refuses_multi_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    _bootstrap_default(tmp_path)
    b2 = tmp_path / "kanban" / "boards" / "second" / "kanban.db"
    b2.parent.mkdir(parents=True)
    kb.connect(db_path=b2, readonly=False, _bootstrap=True).close()
    with pytest.raises(m.MigrationError) as ei:
        m.enumerate_board(tmp_path)
    assert "more than one board" in str(ei.value).lower()
    assert "second" in str(ei.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_enumerate.py -v`
Expected: FAIL — module `migrate_sqlite_to_pg` does not exist.

- [ ] **Step 3: Implement the scaffold**

```python
# hermes_cli/kanban/migrate_sqlite_to_pg.py
"""Read-only SQLite -> Postgres migrator for the kanban board (Phase 4).

Single-board only: refuses if kanban_db.list_boards() returns >1 board (the
PG IDENTITY ids are global across boards; multi-board remap is deferred).
Reads the source READ-ONLY; never mutates a source board DB.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb

# Parent-first load order (PG has no FK constraints, so order is cosmetic).
MIGRATED_TABLES: tuple[str, ...] = (
    "tasks", "task_runs", "task_events", "task_comments", "task_links",
    "kanban_notify_subs", "kanban_profile_event_subs",
    "kanban_profile_event_claims", "kanban_profile_wake_events",
)
# Reverse order for board-scoped deletes under --force.
DELETE_ORDER: tuple[str, ...] = tuple(reversed(MIGRATED_TABLES))
IDENTITY_TABLES: tuple[str, ...] = (
    "task_comments", "task_events", "task_runs", "kanban_profile_wake_events",
)
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "task_events": frozenset({"payload"}),
    "task_runs": frozenset({"metadata"}),
}


class MigrationError(Exception):
    """Fatal precondition failure (multi-board, bad source data, target guard)."""


def enumerate_board(home: Optional[Path] = None) -> str:
    """Return the single board slug to migrate, or raise if >1 board exists."""
    boards = kb.list_boards()
    if len(boards) > 1:
        slugs = ", ".join(sorted(b["slug"] for b in boards))
        raise MigrationError(
            f"refusing to migrate: found more than one board ({slugs}). "
            "The PG schema uses global IDENTITY ids; multi-board migration is "
            "deferred. Migrate with exactly one board on disk."
        )
    return boards[0]["slug"]
```

(The `home` parameter is accepted for test symmetry/readability; resolution is via `kanban_db`/env.)

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_enumerate.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/migrate_sqlite_to_pg.py tests/hermes_cli/kanban/test_migrate_enumerate.py
git commit -m "feat(kanban-pg): migrator scaffold + single-board guard"
```

---

## Task 3: `read_source` + validate (read-only, UTF-8/JSON)

**Files:**
- Modify: `hermes_cli/kanban/migrate_sqlite_to_pg.py`
- Test: `tests/hermes_cli/kanban/test_migrate_read_source.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_migrate_read_source.py
import sqlite3
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


def _src(tmp_path):
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    return p


def test_read_source_returns_cols_and_rows(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_aaa", "hello", "ready", 0, 1700000000, "scratch"))
        c.commit()
    data = m.read_source(p)
    cols, rows = data["tasks"]
    assert "id" in cols and "title" in cols
    assert len(rows) == 1 and rows[0]["id"] == "t_aaa" and rows[0]["title"] == "hello"
    # untouched tables come back empty
    assert data["task_events"][1] == []


def test_read_source_rejects_non_utf8(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        # title holds invalid UTF-8 bytes
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_bad", b"\xff\xfe", "ready", 0, 1700000000, "scratch"))
        c.commit()
    with pytest.raises(m.MigrationError) as ei:
        m.read_source(p)
    assert "non-utf-8" in str(ei.value).lower() and "t_bad" in str(ei.value)


def test_read_source_rejects_bad_json(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_e", "x", "done", 0, 1700000000, "scratch"))
        c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                  "VALUES (?,?,?,?)", ("t_e", "created", "{not json", 1700000000))
        c.commit()
    with pytest.raises(m.MigrationError) as ei:
        m.read_source(p)
    assert "json" in str(ei.value).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_read_source.py -v`
Expected: FAIL — `read_source` not defined.

- [ ] **Step 3: Implement `read_source`**

```python
# add to hermes_cli/kanban/migrate_sqlite_to_pg.py
import json
import sqlite3


def _decode_and_validate(table: str, row: dict, errors: list[str]) -> dict:
    out: dict = {}
    rid = row.get("id") if "id" in row else row.get("task_id")
    for col, val in row.items():
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{table}(id={rid!r}).{col}: non-utf-8 bytes")
                val = val.decode("utf-8", "replace")
        out[col] = val
    for jc in JSON_COLUMNS.get(table, ()):  # validate JSON parseability
        v = out.get(jc)
        if v in (None, ""):
            out[jc] = None
            continue
        try:
            json.loads(v)
        except Exception as e:
            errors.append(f"{table}(id={rid!r}).{jc}: invalid JSON ({e})")
    return out


def read_source(sqlite_path: Path) -> dict[str, tuple[list[str], list[dict]]]:
    """Read all 9 migrated tables READ-ONLY. Decode TEXT as strict UTF-8 and
    validate JSON columns; collect ALL offenders and raise (no partial output)."""
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    con.text_factory = bytes  # surface non-UTF-8 so we can detect it
    con.row_factory = sqlite3.Row
    errors: list[str] = []
    data: dict[str, tuple[list[str], list[dict]]] = {}
    try:
        for table in MIGRATED_TABLES:
            info = con.execute(f"PRAGMA table_info({table})").fetchall()
            cols = [r["name"].decode() if isinstance(r["name"], bytes) else r["name"]
                    for r in info]
            rows = [
                _decode_and_validate(table, {c: r[c] for c in cols}, errors)
                for r in con.execute(f"SELECT {', '.join(cols)} FROM {table}")
            ]
            data[table] = (cols, rows)
    finally:
        con.close()
    if errors:
        raise MigrationError(
            "source contains data Postgres cannot store; scrub the source and "
            "re-run. Offenders:\n  " + "\n  ".join(errors))
    return data
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_read_source.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/migrate_sqlite_to_pg.py tests/hermes_cli/kanban/test_migrate_read_source.py
git commit -m "feat(kanban-pg): read_source with UTF-8 + JSON validation"
```

---

## Task 4: `load` + `reseq` (board-stamped insert, sequence reset)

**Files:**
- Modify: `hermes_cli/kanban/migrate_sqlite_to_pg.py`
- Test: `tests/hermes_cli/kanban/test_migrate_load.py`

Add a reusable throwaway-schema fixture to the test file (created here, reused later).

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_migrate_load.py
import psycopg
import pytest
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban import migrate_sqlite_to_pg as m


@pytest.fixture
def schema(_pg_dsn):
    """A throwaway schema with the kanban DDL applied; dropped on teardown."""
    name = "mtest_" + __import__("uuid").uuid4().hex[:8]
    conn = psycopg.connect(_pg_dsn, autocommit=True)
    conn.execute(f'CREATE SCHEMA "{name}"')
    conn.execute(f'SET search_path TO "{name}"')
    conn.execute(pg_pool.read_schema_ddl())
    try:
        yield (_pg_dsn, name, conn)
    finally:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        conn.close()


def test_load_stamps_board_and_counts(schema):
    dsn, name, conn = schema
    data = {t: ([], []) for t in m.MIGRATED_TABLES}
    data["tasks"] = (["id", "title", "status", "priority", "created_at",
                      "workspace_kind"],
                     [{"id": "t_1", "title": "a", "status": "ready",
                       "priority": 0, "created_at": 1, "workspace_kind": "scratch"}])
    data["task_events"] = (["id", "task_id", "kind", "payload", "created_at"],
                           [{"id": 1, "task_id": "t_1", "kind": "created",
                             "payload": '{"k": 1}', "created_at": 1}])
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE board='default'").fetchone()[0] == 1
    # payload landed as JSONB
    assert conn.execute("SELECT payload->>'k' FROM task_events").fetchone()[0] == "1"
    # reseq: next id after a max of 1 is 2
    assert conn.execute("SELECT nextval(pg_get_serial_sequence('task_events','id'))").fetchone()[0] == 2


def test_reseq_empty_table_starts_at_one(schema):
    dsn, name, conn = schema
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.reseq(c)
        c.commit()
    assert conn.execute("SELECT nextval(pg_get_serial_sequence('task_runs','id'))").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_load.py -v`
Expected: FAIL — `load`/`reseq` not defined.

- [ ] **Step 3: Implement `load` + `reseq`**

```python
# add to hermes_cli/kanban/migrate_sqlite_to_pg.py

def load(conn, board: str, data: dict[str, tuple[list[str], list[dict]]]) -> None:
    """Insert every migrated table's rows, stamping `board`, casting JSON->JSONB.
    Assumes conn's search_path already points at the target schema."""
    with conn.cursor() as cur:
        for table in MIGRATED_TABLES:
            cols, rows = data.get(table, ([], []))
            if not rows:
                continue
            jcols = JSON_COLUMNS.get(table, frozenset())
            placeholders = ["%s"] + [
                "%s::jsonb" if c in jcols else "%s" for c in cols]
            sql = (f"INSERT INTO {table} (board, {', '.join(cols)}) "
                   f"VALUES ({', '.join(placeholders)})")
            params = [[board] + [r.get(c) for c in cols] for r in rows]
            cur.executemany(sql, params)


def reseq(conn) -> None:
    """Set each IDENTITY sequence to max(id) so the next insert is max+1
    (or to 1/uncalled when the table is empty)."""
    with conn.cursor() as cur:
        for table in IDENTITY_TABLES:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT COUNT(*) FROM {table}) > 0)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_load.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/migrate_sqlite_to_pg.py tests/hermes_cli/kanban/test_migrate_load.py
git commit -m "feat(kanban-pg): board-stamped load + IDENTITY reseq"
```

---

## Task 5: `verify` (counts + integrity + id/seq + store-parity + source-doctor)

**Files:**
- Modify: `hermes_cli/kanban/migrate_sqlite_to_pg.py`
- Test: `tests/hermes_cli/kanban/test_migrate_verify.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_migrate_verify.py
import psycopg
import pytest
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban import migrate_sqlite_to_pg as m
from tests.hermes_cli.kanban.test_migrate_load import schema  # reuse fixture


def _data_one_task_one_event():
    data = {t: ([], []) for t in m.MIGRATED_TABLES}
    data["tasks"] = (["id", "title", "status", "priority", "created_at",
                      "workspace_kind"],
                     [{"id": "t_1", "title": "a", "status": "ready",
                       "priority": 0, "created_at": 1, "workspace_kind": "scratch"}])
    data["task_events"] = (["id", "task_id", "kind", "payload", "created_at"],
                           [{"id": 1, "task_id": "t_1", "kind": "created",
                             "payload": None, "created_at": 1}])
    return data


def test_verify_ok_after_clean_load(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert report.ok, report.render()
    assert report.counts["task_events"] == (1, 1)


def test_verify_detects_count_mismatch(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    # claim the source says 2 events though only 1 was loaded
    data["task_events"] = (data["task_events"][0],
                           data["task_events"][1] + [{"id": 2, "task_id": "t_1",
                            "kind": "x", "payload": None, "created_at": 2}])
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert not report.ok
    assert any("task_events" in mm for mm in report.count_mismatches)


def test_verify_detects_orphan_event(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    # event references a task that is NOT loaded -> orphan
    data["task_events"][1][0]["task_id"] = "t_ghost"
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert not report.ok
    assert any("orphan" in f.lower() and "task_events" in f
               for f in report.integrity_failures)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_verify.py -v`
Expected: FAIL — `verify`/`VerifyReport` not defined.

- [ ] **Step 3: Implement `verify` + `VerifyReport`**

```python
# add to hermes_cli/kanban/migrate_sqlite_to_pg.py
import dataclasses
import psycopg

# Each entry: a label and a SQL predicate counting BAD rows for :board.
_INTEGRITY_CHECKS: tuple[tuple[str, str], ...] = (
    ("orphan task_links.parent",
     "SELECT COUNT(*) FROM task_links l WHERE l.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=l.board AND t.id=l.parent_id)"),
    ("orphan task_links.child",
     "SELECT COUNT(*) FROM task_links l WHERE l.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=l.board AND t.id=l.child_id)"),
    ("orphan task_comments.task_id",
     "SELECT COUNT(*) FROM task_comments x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan task_events.task_id",
     "SELECT COUNT(*) FROM task_events x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan task_runs.task_id",
     "SELECT COUNT(*) FROM task_runs x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_notify_subs.task_id",
     "SELECT COUNT(*) FROM kanban_notify_subs x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_profile_event_subs.task_id",
     "SELECT COUNT(*) FROM kanban_profile_event_subs x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_profile_wake_events.task_id",
     "SELECT COUNT(*) FROM kanban_profile_wake_events x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_profile_event_claims.root_task_id",
     "SELECT COUNT(*) FROM kanban_profile_event_claims x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.root_task_id)"),
    ("dangling task_events.run_id",
     "SELECT COUNT(*) FROM task_events x WHERE x.board=%(b)s AND x.run_id IS NOT NULL "
     "AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.board=x.board AND r.id=x.run_id)"),
    ("dangling tasks.current_run_id",
     "SELECT COUNT(*) FROM tasks x WHERE x.board=%(b)s AND x.current_run_id IS NOT NULL "
     "AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.board=x.board AND r.id=x.current_run_id)"),
    ("dangling kanban_profile_event_claims.event_id",
     "SELECT COUNT(*) FROM kanban_profile_event_claims x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM task_events e WHERE e.board=x.board AND e.id=x.event_id)"),
)


@dataclasses.dataclass
class VerifyReport:
    counts: dict = dataclasses.field(default_factory=dict)        # table -> (src, tgt)
    count_mismatches: list = dataclasses.field(default_factory=list)
    integrity_failures: list = dataclasses.field(default_factory=list)
    idseq_failures: list = dataclasses.field(default_factory=list)
    parity_mismatches: list = dataclasses.field(default_factory=list)
    source_doctor_criticals: list = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.count_mismatches or self.integrity_failures
                    or self.idseq_failures or self.parity_mismatches
                    or self.source_doctor_criticals)

    def render(self) -> str:
        lines = [f"verify: {'OK' if self.ok else 'FAILED'}"]
        for t, (s, g) in sorted(self.counts.items()):
            mark = "" if s == g else "  <-- MISMATCH"
            lines.append(f"  count {t:32} src={s:<7} tgt={g:<7}{mark}")
        for grp, items in (("integrity", self.integrity_failures),
                           ("id/seq", self.idseq_failures),
                           ("parity", self.parity_mismatches),
                           ("source-doctor", self.source_doctor_criticals)):
            for it in items:
                lines.append(f"  [{grp}] {it}")
        return "\n".join(lines)


def _check_counts(data, cur, board, report):
    for table in MIGRATED_TABLES:
        src = len(data.get(table, ([], []))[1])
        tgt = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE board=%s",
                          (board,)).fetchone()[0]
        report.counts[table] = (src, tgt)
        if src != tgt:
            report.count_mismatches.append(f"{table}: src={src} tgt={tgt}")


def _check_integrity(cur, board, report):
    for label, sql in _INTEGRITY_CHECKS:
        bad = cur.execute(sql, {"b": board}).fetchone()[0]
        if bad:
            report.integrity_failures.append(f"{label}: {bad} bad row(s)")


def _check_idseq(data, cur, board, report):
    for table in IDENTITY_TABLES:
        rows = data.get(table, ([], []))[1]
        src_max = max((int(r["id"]) for r in rows), default=0)
        seq = cur.execute("SELECT pg_get_serial_sequence(%s, 'id')",
                          (table,)).fetchone()[0]
        last_value, is_called = cur.execute(
            f"SELECT last_value, is_called FROM {seq}").fetchone()
        if src_max > 0:
            tgt_max = cur.execute(
                f"SELECT COALESCE(MAX(id),0) FROM {table} WHERE board=%s",
                (board,)).fetchone()[0]
            if tgt_max != src_max:
                report.idseq_failures.append(
                    f"{table}: max(id) src={src_max} tgt={tgt_max}")
            if not (is_called and last_value == src_max):
                report.idseq_failures.append(
                    f"{table}: sequence last_value={last_value} is_called={is_called}, "
                    f"expected {src_max}/True")
        else:
            if is_called:
                report.idseq_failures.append(
                    f"{table}: sequence is_called=True on an empty table")


def _check_parity(data, dsn, schema, board, report, sample=None):
    """Read a sample of tasks via both stores; assert get_task() equality."""
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    task_ids = [r["id"] for r in data.get("tasks", ([], []))[1]]
    if sample is not None:
        task_ids = task_ids[:sample]
    sq = SqliteKanbanStore(board=board if board != "default" else None)
    pool = pg_pool.make_pool(dsn, search_path=f"{schema},public")
    try:
        pg = PostgresKanbanStore(board=board, pool=pool)
        sl = sq.list_tasks()
        pl = pg.list_tasks()
        if len(sl) != len(pl):
            report.parity_mismatches.append(
                f"list_tasks length src={len(sl)} tgt={len(pl)}")
        for tid in task_ids:
            a, b = sq.get_task(tid), pg.get_task(tid)
            if a != b:
                report.parity_mismatches.append(f"get_task({tid}) differs")
    finally:
        sq.close()
        pool.close()


def verify(data, dsn: str, schema: str, board: str, *,
           sqlite_path: Optional[Path] = None, sample=None,
           check_parity: bool = True) -> VerifyReport:
    """Full verification stack. `data` is the read_source() snapshot of the
    source; the target is `schema` in `dsn`. `check_parity` reads the source
    board through SqliteKanbanStore; disable it for SQL-only unit tests that
    have no matching on-disk source."""
    report = VerifyReport()
    if sqlite_path is not None:
        from hermes_cli import kanban_board_doctor as kdoc
        doc = kdoc.run_board_doctor(board=board)
        report.source_doctor_criticals = [
            i for i in doc.get("issues", []) if i.get("severity") == "critical"]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f'SET search_path TO "{schema}", public')
        with c.cursor() as cur:
            _check_counts(data, cur, board, report)
            _check_integrity(cur, board, report)
            _check_idseq(data, cur, board, report)
    if check_parity:
        _check_parity(data, dsn, schema, board, report, sample=sample)
    return report
```

Note: `_check_parity` builds the SQLite store from the live board resolution (the migrator runs with the real board on disk; `sqlite_path` is used only for the doctor precondition, which resolves by `board`). In the capstone test (Task 8) `HERMES_KANBAN_HOME` makes both resolve correctly.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_verify.py -v`
Expected: PASS (all three). These SQL-only tests pass `check_parity=False` (no on-disk source matches the in-memory `data`); parity is exercised end-to-end in Tasks 6 and 8 against a real seeded board.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/migrate_sqlite_to_pg.py tests/hermes_cli/kanban/test_migrate_verify.py
git commit -m "feat(kanban-pg): verification stack (counts/integrity/id-seq/parity/doctor)"
```

---

## Task 6: `dry_run` + `execute` orchestration (guard + `--force`)

**Files:**
- Modify: `hermes_cli/kanban/migrate_sqlite_to_pg.py`
- Test: `tests/hermes_cli/kanban/test_migrate_orchestrate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_migrate_orchestrate.py
import sqlite3
import psycopg
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


@pytest.fixture
def seeded_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_1", "a", "ready", 0, 1, "scratch"))
        c.commit()
    return tmp_path, p


def _schema_exists(dsn, name):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute("SELECT 1 FROM information_schema.schemata "
                         "WHERE schema_name=%s", (name,)).fetchone() is not None


def test_dry_run_green_and_drops_schema(seeded_home, _pg_dsn):
    home, p = seeded_home
    report, schema = m.dry_run(_pg_dsn, "default", sqlite_path=p)
    assert report.ok, report.render()
    assert not _schema_exists(_pg_dsn, schema)  # cleaned up


def test_execute_then_guard_then_force(seeded_home, _pg_dsn):
    home, p = seeded_home
    name = "xtest_" + __import__("uuid").uuid4().hex[:8]
    try:
        r1 = m.execute(_pg_dsn, "default", sqlite_path=p, target_schema=name)
        assert r1.ok
        # second execute refuses (target now non-empty for the board)
        with pytest.raises(m.MigrationError) as ei:
            m.execute(_pg_dsn, "default", sqlite_path=p, target_schema=name)
        assert "force" in str(ei.value).lower()
        # --force succeeds and counts still match
        r3 = m.execute(_pg_dsn, "default", sqlite_path=p, target_schema=name,
                       force=True)
        assert r3.ok and r3.counts["tasks"] == (1, 1)
    finally:
        with psycopg.connect(_pg_dsn, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_orchestrate.py -v`
Expected: FAIL — `dry_run`/`execute` not defined.

- [ ] **Step 3: Implement orchestration**

```python
# add to hermes_cli/kanban/migrate_sqlite_to_pg.py

def _dryrun_schema_name(board: str, sqlite_path: Path) -> str:
    # Deterministic (no wall clock): board + source mtime -> stable across re-runs.
    mtime = int(sqlite_path.stat().st_mtime)
    return f"kanban_dryrun_{board}_{mtime}"[:60].replace("-", "_")


def _apply_schema(conn, schema: str) -> None:
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    conn.execute(pg_pool.read_schema_ddl())


def dry_run(dsn: str, board: str, *, sqlite_path: Path):
    """Load into a throwaway schema, verify, then drop it. Returns (report, schema)."""
    data = read_source(sqlite_path)
    schema = _dryrun_schema_name(board, sqlite_path)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        _apply_schema(c, schema)
        load(c, board, data)
        reseq(c)
    report = verify(data, dsn, schema, board, sqlite_path=sqlite_path)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    return report, schema


def execute(dsn: str, board: str, *, sqlite_path: Path, force: bool = False,
            target_schema: str = "public"):
    """Load into the target schema with an all-or-nothing transaction and a
    refuse-unless-force guard. Returns the VerifyReport."""
    data = read_source(sqlite_path)
    with psycopg.connect(dsn, autocommit=True) as c:
        _apply_schema(c, target_schema)
        existing = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE board=%s", (board,)).fetchone()[0]
        if existing and not force:
            raise MigrationError(
                f"target schema {target_schema!r} already has {existing} task(s) "
                f"for board {board!r}; pass force=True/--force to overwrite.")
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{target_schema}"')
        if force:
            with c.cursor() as cur:
                for table in DELETE_ORDER:
                    cur.execute(f"DELETE FROM {table} WHERE board=%s", (board,))
        load(c, board, data)
        reseq(c)
        c.commit()
    report = verify(data, dsn, target_schema, board, sqlite_path=sqlite_path)
    return report
```

(Structural verification in `execute` runs post-commit here for symmetry with `dry_run`; a clean source always passes. The `--force` truncate + load happen atomically, so a *failed load* never partially overwrites. If you want pre-commit gating, move the count/integrity checks before `c.commit()` — noted as an optional hardening in code review.)

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_orchestrate.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/migrate_sqlite_to_pg.py tests/hermes_cli/kanban/test_migrate_orchestrate.py
git commit -m "feat(kanban-pg): dry_run + execute orchestration with guard/force"
```

---

## Task 7: CLI `main()`

**Files:**
- Modify: `hermes_cli/kanban/migrate_sqlite_to_pg.py`
- Test: `tests/hermes_cli/kanban/test_migrate_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_migrate_cli.py
import sqlite3
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


@pytest.fixture
def seeded_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_1", "a", "ready", 0, 1, "scratch"))
        c.commit()
    return tmp_path


def test_main_requires_mode(seeded_home, _pg_dsn, capsys):
    rc = m.main(["--dsn", _pg_dsn])
    assert rc == 2


def test_main_dry_run_ok(seeded_home, _pg_dsn):
    rc = m.main(["--dry-run", "--dsn", _pg_dsn])
    assert rc == 0


def test_main_json_output(seeded_home, _pg_dsn, capsys):
    rc = m.main(["--dry-run", "--dsn", _pg_dsn, "--json"])
    out = capsys.readouterr().out
    import json
    assert rc == 0 and json.loads(out)["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_cli.py -v`
Expected: FAIL — `main` not defined.

- [ ] **Step 3: Implement `main`**

```python
# add to hermes_cli/kanban/migrate_sqlite_to_pg.py
import argparse
import sys


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="migrate_sqlite_to_pg",
        description="Read-only SQLite -> Postgres kanban migrator (Phase 4).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Load into a throwaway schema, verify, drop it.")
    mode.add_argument("--execute", action="store_true",
                      help="Load into the target (public) schema.")
    ap.add_argument("--force", action="store_true",
                    help="With --execute: delete the board's rows first.")
    ap.add_argument("--dsn", default=None, help="Postgres DSN (else resolve_dsn()).")
    ap.add_argument("--board", default=None, help="Board slug (else single-board guard).")
    ap.add_argument("--json", action="store_true", help="Emit a JSON report.")
    ap.add_argument("--report", default=None, help="Write the JSON report to PATH.")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    try:
        dsn = args.dsn or pg_pool.resolve_dsn()
        board = args.board or enumerate_board()
        sqlite_path = Path(kb.kanban_db_path(board))
        if args.dry_run:
            report, _schema = dry_run(dsn, board, sqlite_path=sqlite_path)
        else:
            report = execute(dsn, board, sqlite_path=sqlite_path, force=args.force)
    except MigrationError as e:
        print(f"migration aborted: {e}", file=sys.stderr)
        return 2

    payload = {
        "ok": report.ok,
        "board": board,
        "counts": {t: {"src": s, "tgt": g} for t, (s, g) in report.counts.items()},
        "count_mismatches": report.count_mismatches,
        "integrity_failures": report.integrity_failures,
        "idseq_failures": report.idseq_failures,
        "parity_mismatches": report.parity_mismatches,
        "source_doctor_criticals": report.source_doctor_criticals,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(payload, indent=2, default=str))
    if args.json:
        print(json.dumps(payload, default=str))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_cli.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/migrate_sqlite_to_pg.py tests/hermes_cli/kanban/test_migrate_cli.py
git commit -m "feat(kanban-pg): migrator CLI (main/argparse)"
```

---

## Task 8: Capstone round-trip test — all 9 tables, store-API seeded, parity-clean

Seeds a realistic board via the `SqliteKanbanStore` API (the same idioms the conformance suite uses → parity-clean by construction), touching all 9 migrated tables, then dry-runs the migrator and asserts a green report.

**Files:**
- Test: `tests/hermes_cli/kanban/test_migrate_roundtrip.py`

- [ ] **Step 1: Write the test**

```python
# tests/hermes_cli/kanban/test_migrate_roundtrip.py
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m
from hermes_cli.kanban.store_sqlite import SqliteKanbanStore


@pytest.fixture
def realistic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    s = SqliteKanbanStore(board=None)
    # tasks + links + run + events + comment (via lifecycle)
    parent = s.create_task(title="parent", assignee="engineer")
    child = s.create_task(title="child", assignee="engineer")
    s.link_tasks(parent, child)
    s.claim_task(parent, claimer="w1")          # -> task_runs + claimed event
    s.add_comment(parent, author="ops", body="note")
    s.complete_task(parent, summary="done")     # -> completed event, run ended
    # a blocked task to vary status
    other = s.create_task(title="other", assignee="engineer")
    s.block_task(other, reason="need input")
    # notify sub + cursor advance (last_event_id -> real event id range)
    s.add_notify_sub(task_id=child, platform="telegram", chat_id="c1")
    s.advance_notify_cursor(task_id=child, platform="telegram", chat_id="c1",
                            new_cursor=1)
    # profile event sub + claim (-> kanban_profile_event_claims) + wake event
    s.add_profile_event_sub(task_id=other, profile="engineer")
    old, new, _ = s.claim_unseen_events_for_profile_sub(task_id=other,
                                                        profile="engineer")
    s.record_profile_wake_success(task_id=other, profile="engineer",
                                  new_cursor=new, last_wake_at=1700000000)
    s.close()
    return tmp_path, p


def test_full_board_round_trip(realistic_home, _pg_dsn):
    home, p = realistic_home
    report, _schema = m.dry_run(_pg_dsn, "default", sqlite_path=p)
    assert report.ok, report.render()
    # every migrated table's count matches exactly
    for table, (src, tgt) in report.counts.items():
        assert src == tgt, f"{table}: {src} != {tgt}"
    # the tables that must be non-empty actually got data
    for table in ("tasks", "task_links", "task_runs", "task_events",
                  "task_comments", "kanban_notify_subs",
                  "kanban_profile_event_subs", "kanban_profile_event_claims",
                  "kanban_profile_wake_events"):
        assert report.counts[table][0] > 0, f"{table} unexpectedly empty in source"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/test_migrate_roundtrip.py -v`
Expected: PASS. If `get_task` parity surfaces a benign representational diff (e.g. `""` vs `None`) on some field, narrow it in `_check_parity` with a documented normalization and note it in the commit — a *real* semantic diff must still fail.

- [ ] **Step 3: Commit**

```bash
git add tests/hermes_cli/kanban/test_migrate_roundtrip.py
git commit -m "test(kanban-pg): full-board round-trip migration parity"
```

---

## Task 9: Cutover runbook

**Files:**
- Create: `plans/kanban-postgres-migration/cutover-runbook.md`

- [ ] **Step 1: Write the runbook**

Write `plans/kanban-postgres-migration/cutover-runbook.md` with these sections (fill with the concrete commands; no placeholders):

1. **Scope & safety** — single board (`default`); source read-only; reversible before PG takes divergent writes.
2. **Preconditions gate (BLOCKING)** — every `phase-3-tail` item closed (list them, link marker commits `a69ef9949`/`157d4dab3` + the `kanban-pg-phase3-glue` memory note); Supabase DSN provisioned + reachable; maintenance window scheduled; latest backup of `kanban.db`.
3. **Rehearsal** — `venv/bin/python -m hermes_cli.kanban.migrate_sqlite_to_pg --dry-run --dsn <supabase> --json` → assert `ok: true`.
4. **Quiesce** — `hermes gateway stop`; confirm no kanban writers; confirm `kanban.db-wal` is checkpointed.
5. **Final load** — `… --execute --dsn <supabase>` (no `--force` into a fresh DB) → assert `ok: true`.
6. **Flip config** — set `kanban.backend=postgres` and `HERMES_KANBAN_PG_DSN` / `kanban.postgres.dsn` (install the `postgres` extra: `pip install -e '.[postgres]'`).
7. **Restart + verify** — restart gateway + dashboard; smoke: `hermes kanban list`, create/claim/complete a throwaway task, dashboard loads, no `disk I/O error`.
8. **Rollback** — flip `kanban.backend=sqlite`, unset DSN, restart; the untouched `kanban.db` resumes. Valid ONLY before any PG write diverges from SQLite.
9. **Decommission (later)** — Phase 6: retire SQLite life-support; begin the web-dashboard phase.

- [ ] **Step 2: Commit**

```bash
git add plans/kanban-postgres-migration/cutover-runbook.md
git commit -m "docs(kanban-pg): Phase 5 cutover runbook + phase-3-tail preconditions"
```

---

## Task 10: Acceptance — full suite, both backends, default unchanged

**Files:** none (verification only)

- [ ] **Step 1: Migrator + conformance tests (docker PG auto-starts)**

Run: `venv/bin/python -m pytest tests/hermes_cli/kanban/ -v`
Expected: PASS — all new migrator tests + the existing conformance suite (sqlite + docker-PG) green.

- [ ] **Step 2: Default-backend regression (sqlite path unchanged)**

Run: `venv/bin/python -m pytest tests/hermes_cli/test_kanban_db.py -q`
Expected: PASS — no regression on the SQLite backend (the migrator adds only new files + an additive `pg_pool` kwarg).

- [ ] **Step 3: Confirm no edit to the live dispatch path**

Run: `git diff --stat main -- hermes_cli/kanban_db.py gateway/run.py hermes_cli/kanban_glue.py hermes_cli/kanban/store_postgres.py`
Expected: EMPTY (Phase 4 touches only `pg_pool.py` + new files + docs).

- [ ] **Step 4: Finish the branch**

Use superpowers:finishing-a-development-branch to choose merge/PR/cleanup. Default backend stays `sqlite`; live picks up the new module on next restart (no behavior change).

---

## Self-review notes (author)

- **Spec coverage:** scope/deliverables (Tasks 2–9), single-board guard (T2), read-only + UTF-8/JSON validation (T3), board-stamp + load order + JSON→JSONB (T4), IDENTITY reseq ×4 (T4), throwaway-schema dry-run + drop (T6), refuse+`--force` (T6), verification stack incl. parity + source-doctor (T5), standalone CLI (T7), runbook + `phase-3-tail` BLOCKING preconditions (T9), boundaries/acceptance (T10). All present.
- **Type consistency:** `MigrationError`, `VerifyReport` (`.ok`/`.render()`/`.counts`), `enumerate_board`, `read_source`, `load`, `reseq`, `verify`, `dry_run`, `execute`, `main` used consistently across tasks; `pg_pool.make_pool(search_path=…)` + `read_schema_ddl()` defined in T1 before use in T4–T6.
- **No placeholders:** every code/test step is concrete.
- **Known risk to watch in review:** `get_task` parity on exotic migrated states (T8 step 2 note); `execute` post-commit verify ordering (T6 note — optional pre-commit hardening).
