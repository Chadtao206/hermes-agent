# Doctor / Liveness Postgres-Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `run_board_doctor`, `kanban_liveness.compute_board_liveness`, and the gateway liveness loop read the LIVE kanban backend (Postgres when `kanban.backend=postgres`), so post-cutover health/monitoring reflects reality instead of the frozen sqlite file.

**Architecture:** Backend-branch in the fork-owned diagnostics modules. The sqlite code paths are left byte-identical; a `resolve_backend()=="postgres"` branch runs the same logical/liveness invariants as board-scoped Postgres SQL. `kanban_db.py` is not touched.

**Tech Stack:** Python 3, `psycopg` 3 + `psycopg_pool` (via `hermes_cli.kanban.pg_pool`), pytest with the docker-`postgres:16-alpine` conformance fixture (`tests/hermes_cli/kanban/conftest.py`).

---

## Pre-flight (executor)

- Worktree: `.worktrees/kanban-doctor-pg`, branch `feat/kanban-doctor-pg` off `main` @ current HEAD (use superpowers:using-git-worktrees).
- **Test interpreter (mandatory):** `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest …` (only this venv has `psycopg`+`pytest`; `-m pytest` imports the worktree's code). Docker must be running (the `_pg_dsn` session fixture auto-starts `postgres:16-alpine`).
- Design: `plans/kanban-postgres-migration/doctor-liveness-pg-awareness-design.md`.

## Reference facts (verified against `main`)

- `hermes_cli/kanban_board_doctor.py`: `run_board_doctor(*, board=None, ready_age_seconds=15*60)` → `_quick_check(path)` (sqlite file integrity) + `kb.snapshot_connect(board)` SQL checks + `_reconcile_summary(...)` (sqlite reconciler) → returns `{ok, board, db_path, issues, reconcile_summary, as_of}`. Helpers: `_issue(severity, kind, message, **extra)`, `_alive(pid)`. Six issue kinds: `orphan_task_link`, `orphan_profile_event_subscription`, `stale_running_task`, `stale_running_run`, `blocked_with_completed_parents`, `old_ready_task`.
- `hermes_cli/kanban_liveness.py`: `@dataclass Liveness(oldest_ready_age_seconds, oldest_blocked_done_parents_age_seconds, oldest_stale_running_age_seconds, notifier_enabled=True, writer_daemon_disabled=False, extra={})`; `compute_board_liveness(conn, *, now) -> Liveness` (3 sqlite `_scalar` queries); `evaluate(snap, *, thresholds) -> list[Breach]` (backend-agnostic). `_scalar(conn, sql, *params)`.
- `gateway/run.py`: `_run_liveness_check_once(self, state)` (~5711) iterates `_kb.list_boards(include_archived=False)`, per board calls `_liveness_subsystem_flags(slug, key)` then opens `_kb.connect(board=slug, readonly=True)` → `compute_board_liveness(conn)` → `evaluate` → `_maybe_emit_liveness_alert`.
- Backend selector: `from hermes_cli.kanban.store import resolve_backend` (returns `"sqlite"`/`"postgres"` from config). PG connection: `from hermes_cli.kanban import pg_pool` → `pg_pool.get_pool()` (resolves DSN from config; pool sets `prepare_threshold=None`). `from psycopg.rows import dict_row`. Redacted DSN parse: `psycopg.conninfo.conninfo_to_dict(pg_pool.resolve_dsn())`.
- Both diagnostics modules are fork-owned (safe to edit); `kanban_db.py` must NOT be edited.

---

## Task 1: `compute_board_liveness_pg` (PG liveness metrics)

**Files:**
- Modify: `hermes_cli/kanban_liveness.py`
- Test: `tests/hermes_cli/kanban/test_kanban_liveness_pg.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_kanban_liveness_pg.py
import os, shutil, uuid
import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker")),
    reason="postgres backend unavailable")


@pytest.fixture
def pg(_pg_dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    board = f"liv_{uuid.uuid4().hex[:8]}"
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def test_compute_board_liveness_pg(pg):
    from hermes_cli import kanban_liveness as liv
    from psycopg.rows import dict_row
    s, pool, board = pg
    now = 1_000_000
    # oldest ready: a ready task backdated 5000s
    r = s.create_task(title="r", assignee="engineer")
    with pool.connection() as c:
        c.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                  (now - 5000, board, r))
    # blocked-with-done-parents: parent completed, child blocked, dep link, backdated 7000s
    parent = s.create_task(title="p", assignee="engineer")
    s.claim_task(parent, claimer="w1"); s.complete_task(parent, summary="done")
    child = s.create_task(title="c", assignee="engineer")
    s.link_tasks(parent, child)
    s.block_task(child, reason="x")
    with pool.connection() as c:
        c.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                  (now - 7000, board, child))
    with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
        snap = liv.compute_board_liveness_pg(cur, board, now=now)
    assert snap.oldest_ready_age_seconds == 5000
    assert snap.oldest_blocked_done_parents_age_seconds == 7000
    assert snap.oldest_stale_running_age_seconds == 0
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_liveness_pg.py -v`
Expected: FAIL — `module 'hermes_cli.kanban_liveness' has no attribute 'compute_board_liveness_pg'`.

- [ ] **Step 3: Implement** — add to `hermes_cli/kanban_liveness.py` (after `compute_board_liveness`):

```python
def _scalar_pg(cur, sql: str, params) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return 0
    val = row[0] if not isinstance(row, dict) else next(iter(row.values()))
    return int(val) if val is not None else 0


def compute_board_liveness_pg(cur, board: str, *, now: int) -> Liveness:
    """Postgres equivalent of compute_board_liveness: same three invariants,
    board-scoped, over a psycopg cursor. Mirrors the sqlite query logic."""
    oldest_ready = _scalar_pg(
        cur,
        "SELECT MAX(%s - created_at) FROM tasks WHERE board=%s AND status='ready'",
        (now, board))
    oldest_blocked = _scalar_pg(
        cur,
        "SELECT MAX(%s - t.created_at) FROM tasks t "
        "WHERE t.board=%s AND t.status='blocked' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_links l JOIN tasks p "
        "    ON p.board=l.board AND p.id=l.parent_id "
        "  WHERE l.board=t.board AND l.child_id=t.id "
        "    AND l.relation_type='dependency' "
        "    AND p.status NOT IN ('done','archived'))",
        (now, board))
    oldest_stale_running = _scalar_pg(
        cur,
        "SELECT MAX(%s - COALESCE(last_heartbeat_at, started_at, created_at)) "
        "FROM tasks WHERE board=%s AND status='running'",
        (now, board))
    return Liveness(
        oldest_ready_age_seconds=max(0, oldest_ready),
        oldest_blocked_done_parents_age_seconds=max(0, oldest_blocked),
        oldest_stale_running_age_seconds=max(0, oldest_stale_running))
```

Note the cursor may be a `dict_row` cursor (as in the test) — `_scalar_pg` handles both tuple and dict rows. The sqlite `compute_board_liveness`, `_scalar`, `Liveness`, and `evaluate` are unchanged.

- [ ] **Step 4: Run, verify it PASSES**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_liveness_pg.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_liveness.py tests/hermes_cli/kanban/test_kanban_liveness_pg.py
git commit -m "feat(kanban-pg): compute_board_liveness_pg (board-scoped PG liveness metrics)"
```

---

## Task 2: `run_board_doctor` Postgres path

**Files:**
- Modify: `hermes_cli/kanban_board_doctor.py`
- Test: `tests/hermes_cli/kanban/test_kanban_board_doctor_pg.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/kanban/test_kanban_board_doctor_pg.py
import os, shutil, uuid
import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker")),
    reason="postgres backend unavailable")


@pytest.fixture
def pg(_pg_dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    board = f"doc_{uuid.uuid4().hex[:8]}"
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def test_doctor_pg_detects_defects_and_redacts_dsn(pg):
    from hermes_cli import kanban_board_doctor as kdoc
    s, pool, board = pg
    # orphan task_link: link to a non-existent child
    a = s.create_task(title="a", assignee="engineer")
    with pool.connection() as c:
        c.execute("INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                  "VALUES (%s,%s,%s,'dependency')", (board, a, "t_ghostchild"))
        # old ready task: backdate well past the 900s threshold
        c.execute("UPDATE tasks SET created_at=created_at-100000 WHERE board=%s AND id=%s",
                  (board, a))
    res = kdoc._run_board_doctor_pg(board=board, ready_age_seconds=900, pool=pool)
    kinds = {i["kind"] for i in res["issues"]}
    assert "orphan_task_link" in kinds
    assert "old_ready_task" in kinds
    assert res["ok"] is False
    # db_path is the redacted postgres identifier, NOT a password
    assert res["db_path"].startswith("postgres://")
    assert ":6543" in res["db_path"] or "pooler" in res["db_path"] or "@" not in res["db_path"]


def test_doctor_pg_unreachable_is_critical(pg):
    from hermes_cli import kanban_board_doctor as kdoc
    s, pool, board = pg

    class _BadPool:  # .connection() raises -> connectivity probe fails fast
        def connection(self, *a, **k):
            raise RuntimeError("pool down")

    res = kdoc._run_board_doctor_pg(board=board, ready_age_seconds=900, pool=_BadPool())
    assert res["ok"] is False
    assert any(i["severity"] == "critical" and i["kind"] == "pg_unreachable"
               for i in res["issues"])
    # connectivity failure short-circuits: no logical-check issues mixed in
    assert all(i["kind"] == "pg_unreachable" for i in res["issues"])
```

- [ ] **Step 2: Run, verify it FAILS**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_board_doctor_pg.py -v`
Expected: FAIL — `_run_board_doctor_pg` not defined.

- [ ] **Step 3: Implement.**

(a) At the TOP of `run_board_doctor`, add the dispatch (the existing sqlite body stays exactly as-is below it):
```python
def run_board_doctor(*, board: str | None = None, ready_age_seconds: int = 15 * 60) -> dict[str, Any]:
    try:
        from hermes_cli.kanban.store import resolve_backend
        _backend = resolve_backend()
    except Exception:
        _backend = "sqlite"
    if _backend == "postgres":
        return _run_board_doctor_pg(board=board, ready_age_seconds=ready_age_seconds)
    # --- sqlite path (unchanged) ---
    path = kb.kanban_db_path(board=board)
    ...
```

(b) Add the new PG function (near `run_board_doctor`). Imports at module top: `from psycopg.rows import dict_row`. It accepts an optional `pool` for tests; production resolves via `pg_pool.get_pool()`:
```python
def _redacted_pg_dsn() -> str:
    try:
        from hermes_cli.kanban import pg_pool
        import psycopg.conninfo as _ci
        d = _ci.conninfo_to_dict(pg_pool.resolve_dsn())
        return f"postgres://{d.get('host')}:{d.get('port')}/{d.get('dbname')}"
    except Exception:
        return "postgres://<unknown>"


def _run_board_doctor_pg(*, board: str | None, ready_age_seconds: int, pool=None) -> dict[str, Any]:
    from hermes_cli.kanban import pg_pool
    from psycopg.rows import dict_row
    slug = board or kb.get_current_board()
    now = int(time.time())
    issues: list[Issue] = []
    db_path = _redacted_pg_dsn()
    pool = pool or pg_pool.get_pool()
    # 1. connectivity in place of sqlite file-integrity (bounded so an
    #    unreachable backend fails fast instead of hanging the doctor)
    try:
        with pool.connection(timeout=5) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        issues.append(_issue(
            "critical", "pg_unreachable",
            f"Postgres kanban backend is unreachable: {type(exc).__name__}: {exc}",
            action="check the Supabase pooler DSN/credentials/network before relying on the board"))
        return {"ok": False, "board": slug, "db_path": db_path, "issues": issues, "as_of": now}
    # 2. logical invariant checks (board-scoped PG SQL; same issue kinds as sqlite)
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # orphan dependency/rollup links
        cur.execute(
            "SELECT l.parent_id, l.child_id, l.relation_type, "
            "       p.id AS parent_exists, c.id AS child_exists "
            "FROM task_links l "
            "LEFT JOIN tasks p ON p.board=l.board AND p.id=l.parent_id "
            "LEFT JOIN tasks c ON c.board=l.board AND c.id=l.child_id "
            "WHERE l.board=%s AND (p.id IS NULL OR c.id IS NULL) "
            "ORDER BY l.parent_id, l.child_id", (slug,))
        for row in cur.fetchall():
            missing = []
            if row["parent_exists"] is None: missing.append("parent")
            if row["child_exists"] is None: missing.append("child")
            issues.append(_issue(
                "error", "orphan_task_link",
                f"task_links references missing {'/'.join(missing)} row",
                parent_id=row["parent_id"], child_id=row["child_id"],
                relation_type=row["relation_type"],
                action="remove/recreate the orphan link before relying on dependency promotion"))
        # profile event subscriptions pointing at missing tasks
        cur.execute(
            "SELECT s.task_id, s.profile, s.name FROM kanban_profile_event_subs s "
            "LEFT JOIN tasks t ON t.board=s.board AND t.id=s.task_id "
            "WHERE s.board=%s AND t.id IS NULL ORDER BY s.task_id, s.profile, s.name", (slug,))
        for row in cur.fetchall():
            issues.append(_issue(
                "error", "orphan_profile_event_subscription",
                "profile wake subscription references a missing task",
                task_id=row["task_id"], profile=row["profile"], name=row["name"],
                action="remove the subscription or recreate the task before enabling notifier wakes"))
        # running tasks with expired claim / dead worker / stale heartbeat
        cur.execute(
            "SELECT id, title, assignee, worker_pid, claim_expires, last_heartbeat_at, current_run_id "
            "FROM tasks WHERE board=%s AND status='running' ORDER BY started_at, created_at", (slug,))
        for row in cur.fetchall():
            pid_alive = _alive(row["worker_pid"])
            expired = bool(row["claim_expires"] and int(row["claim_expires"]) < now)
            stale_hb = bool(row["last_heartbeat_at"] and now - int(row["last_heartbeat_at"]) > 15 * 60)
            if not pid_alive or expired or stale_hb:
                issues.append(_issue(
                    "critical" if expired or not pid_alive else "warning",
                    "stale_running_task",
                    "running task has dead/missing worker, expired claim, or stale heartbeat",
                    task_id=row["id"], assignee=row["assignee"], worker_pid=row["worker_pid"],
                    pid_alive=pid_alive, claim_expired=expired,
                    heartbeat_age_seconds=(now - int(row["last_heartbeat_at"])) if row["last_heartbeat_at"] else None,
                    action="reclaim or inspect worker logs before retrying"))
        # stale run rows left marked running
        cur.execute(
            "SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at, "
            "       t.status AS task_status, t.current_run_id "
            "FROM task_runs r JOIN tasks t ON t.board=r.board AND t.id=r.task_id "
            "WHERE r.board=%s AND r.status='running' "
            "  AND (t.status != 'running' OR t.current_run_id IS NULL OR t.current_run_id != r.id) "
            "ORDER BY r.started_at DESC", (slug,))
        for row in cur.fetchall():
            issues.append(_issue(
                "warning", "stale_running_run",
                "task_run is still marked running but is not the task current running run",
                task_id=row["task_id"], run_id=row["run_id"], profile=row["profile"],
                task_status=row["task_status"], worker_pid=row["worker_pid"],
                pid_alive=_alive(row["worker_pid"]),
                action="mark/reconcile stale run metadata; do not treat it as an active worker"))
        # blocked tasks whose dependency parents are all terminal
        cur.execute(
            "SELECT c.id, c.title, c.assignee, COUNT(l.parent_id) AS parents, "
            "  SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) AS terminal_parents, "
            "  string_agg(p.id || ':' || p.status, ', ') AS parent_state "
            "FROM tasks c "
            "JOIN task_links l ON l.board=c.board AND l.child_id=c.id "
            "JOIN tasks p ON p.board=l.board AND p.id=l.parent_id "
            "WHERE c.board=%s AND c.status='blocked' "
            "  AND COALESCE(l.relation_type,'dependency')='dependency' "
            "GROUP BY c.id, c.title, c.assignee, c.created_at "
            "HAVING COUNT(l.parent_id) > 0 "
            "   AND COUNT(l.parent_id) = SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) "
            "ORDER BY c.created_at", (slug,))
        for row in cur.fetchall():
            issues.append(_issue(
                "warning", "blocked_with_completed_parents",
                "blocked task has all dependency parents completed; likely needs an explicit unblock/re-review decision",
                task_id=row["id"], assignee=row["assignee"], parents=row["parent_state"],
                action="if remediation evidence is sufficient, run `hermes kanban unblock <task>`; otherwise park with a fresh blocker comment"))
        # ready tasks older than threshold
        cur.execute(
            "SELECT id, title, assignee, created_at FROM tasks "
            "WHERE board=%s AND status='ready' ORDER BY created_at", (slug,))
        for row in cur.fetchall():
            age = now - int(row["created_at"])
            if age >= ready_age_seconds:
                issues.append(_issue(
                    "warning", "old_ready_task",
                    "ready task has not been claimed within the threshold",
                    task_id=row["id"], assignee=row["assignee"], age_seconds=age,
                    action="check gateway dispatcher health and whether assignee profile exists"))
    return {"ok": not issues, "board": slug, "db_path": db_path, "issues": issues,
            "reconcile_summary": {"ok": True, "backend": "postgres",
                                  "note": "reconciler embed not run on postgres"},
            "as_of": now}
```

(`Issue`, `_issue`, `_alive`, `kb`, `time` are already in the module.)

- [ ] **Step 4: Run, verify it PASSES**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_board_doctor_pg.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Run the sqlite doctor tests to confirm the byte-identical path is intact**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_board_doctor.py -q`
Expected: PASS (unchanged sqlite behavior; `resolve_backend()` defaults to sqlite in tests).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_board_doctor.py tests/hermes_cli/kanban/test_kanban_board_doctor_pg.py
git commit -m "feat(kanban-pg): run_board_doctor postgres path (connectivity + logical checks, redacted dsn)"
```

---

## Task 3: Gateway liveness loop Postgres branch

**Files:**
- Modify: `gateway/run.py` (`_run_liveness_check_once` only)

- [ ] **Step 1: Implement** — in `_run_liveness_check_once`, branch the snapshot computation on backend. Replace the snapshot block (the `snap = None; if path.exists(): … compute_board_liveness(conn) …` section) with:

```python
            notifier_disabled, writer_disabled = self._liveness_subsystem_flags(slug, key)
            snap = None
            try:
                from hermes_cli.kanban.store import resolve_backend as _rb
                _backend = _rb()
            except Exception:
                _backend = "sqlite"
            if _backend == "postgres":
                try:
                    from hermes_cli.kanban import pg_pool as _pg
                    from psycopg.rows import dict_row as _dr
                    with _pg.get_pool().connection(timeout=5) as _c, _c.cursor(row_factory=_dr) as _cur:
                        snap = _liv.compute_board_liveness_pg(_cur, slug, now=int(time.time()))
                except Exception as exc:
                    logger.debug("kanban liveness: cannot read board %s (postgres): %s", slug, exc)
            elif path.exists():
                try:
                    conn = _kb.connect(board=slug, readonly=True)
                    try:
                        snap = _liv.compute_board_liveness(conn, now=int(time.time()))
                    finally:
                        conn.close()
                except Exception as exc:
                    logger.debug("kanban liveness: cannot read board %s: %s", slug, exc)
            if snap is None:
                snap = _liv.Liveness()
            snap.notifier_enabled = not notifier_disabled
            snap.writer_daemon_disabled = writer_disabled
            breaches = _liv.evaluate(snap, thresholds=thresholds)
            _maybe_emit_liveness_alert(
                breaches, board=slug, state=state, emit=self._emit_liveness_alert,
            )
```

The sqlite branch (`elif path.exists():`) is the original code, unchanged. Only the postgres branch + the backend check are added. `_liv`, `_kb`, `time`, `logger` are already in scope.

- [ ] **Step 2: Verify the gateway module imports cleanly**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -c "import gateway.run"`
Expected: no error.

- [ ] **Step 3: Verify the sqlite liveness path + gateway suites are unaffected**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_liveness.py tests/gateway/test_kanban_notifier_single_writer.py -q`
Expected: PASS (the sqlite branch is byte-identical; `compute_board_liveness` + `evaluate` unchanged).

- [ ] **Step 4: Confirm the diff is scoped to `_run_liveness_check_once`**

Run: `cd <worktree> && git diff -- gateway/run.py`
Expected: only the snapshot-computation block inside `_run_liveness_check_once` changed (the backend branch). If anything else changed, revert it.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py
git commit -m "feat(kanban-pg): gateway liveness loop computes via PG on the postgres backend"
```

---

## Task 4: Acceptance + finish

**Files:** none (verification only)

- [ ] **Step 1: Full kanban + diagnostics suites, both backends**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/ tests/hermes_cli/test_kanban_board_doctor.py tests/hermes_cli/kanban/test_kanban_board_doctor_pg.py tests/hermes_cli/test_kanban_liveness.py tests/hermes_cli/kanban/test_kanban_liveness_pg.py -q`
Expected: PASS (sqlite paths unchanged; new PG paths green against docker PG).

- [ ] **Step 2: Confirm `kanban_db.py` is unedited**

Run: `cd <worktree> && git diff --stat main -- hermes_cli/kanban_db.py`
Expected: EMPTY.

- [ ] **Step 3: Confirm the sqlite paths are byte-identical**

Run: `cd <worktree> && git diff main -- hermes_cli/kanban_board_doctor.py | grep -E "^-" | grep -v "^---"`
Expected: the only removed lines are at the TOP of `run_board_doctor` (the function signature line, replaced by the signature + the new dispatch) — no removals inside the existing sqlite check bodies or `compute_board_liveness`.

- [ ] **Step 4: Finish the branch** — use superpowers:finishing-a-development-branch.

---

## Self-review notes (author)

- **Spec coverage:** PG liveness metrics (Task 1), PG doctor connectivity + 6 logical checks + redacted db_path + reconcile omitted (Task 2), gateway liveness PG branch (Task 3), sqlite byte-identical + kanban_db.py untouched + parity (Tasks 2/3/4). All present.
- **Type consistency:** `compute_board_liveness_pg(cur, board, *, now) -> Liveness`, `_run_board_doctor_pg(*, board, ready_age_seconds, pool=None) -> dict`, `_redacted_pg_dsn()`, `_scalar_pg(cur, sql, params)` used consistently. Issue kinds match the sqlite set exactly.
- **No placeholders:** every code/test step is concrete.
- **Watch in review:** the PG `blocked_with_completed_parents` GROUP BY must include every non-aggregated selected column (`c.id, c.title, c.assignee, c.created_at`) — PG is stricter than sqlite here; `string_agg` replaces sqlite `GROUP_CONCAT`. The `_scalar_pg` dict-vs-tuple row handling (the doctor uses `dict_row`; the liveness test cursor is `dict_row`).
