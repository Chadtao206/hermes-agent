# Phase 6 · B8a — PG-ify `kanban metrics` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under `kanban.backend=postgres`, `hermes kanban metrics [--json] [--write-snapshot]` reports the **live PG** board (not the frozen sqlite), so the re-enabled `kanban-metrics-snapshot` cron persists real trend data.

**Architecture:** `collect_metrics` already gets its `health`/`ok` from `run_board_doctor` + `run_reconciler` (both backend-aware since B1). The ONLY sqlite-coupled part is the `current_state` + `windows` snapshot read + the `db_path` string. So: extract the pure window aggregation, add board-scoped PG read helpers, and branch ONLY the read-acquisition block in `collect_metrics` (doctor/reconcile/health/result-assembly/snapshot stay shared). sqlite path byte-identical.

**Tech Stack:** Python, psycopg 3 (`dict_row`), pytest with the docker `postgres:16-alpine` fixtures.

> **Plan note (refines the design doc):** the design proposed a full `_collect_metrics_pg` returning the whole result dict. During planning I found `run_board_doctor`/`run_reconciler` (the `health`/`ok` source) are already PG-aware, so duplicating them is unnecessary. This plan instead branches only the `(current_state, windows, db_path)` acquisition — strictly less code, same goal.

---

## Ground rules (apply to EVERY task)

- **Never edit** `hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`, `hermes_cli/kanban_writer_daemon.py` — import only (reuse `kb.kanban_db_path`, `kb.snapshot_connect`, `kb.get_current_board`).
- **sqlite path byte-identical** — the dispatch is additive; the window refactor preserves identical sqlite output.
- **No DSN/secret in logs or the result** — under PG, `db_path` is the redacted `host:port/db` (reuse `kanban_board_doctor._redacted_pg_dsn`); error paths surface nothing DSN-bearing.
- **Test interpreter:** `cd .worktrees/kanban-pg-phase6-b8a && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Export `HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban"` before any pytest. NEVER the live Supabase DB; only pytest (fixtures monkeypatch the pool to the local container); never run the gateway/dashboard/live CLI against the real config.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Reference (read before implementing)

`hermes_cli/kanban_metrics.py`: `_run_window_metrics` (~72-159: sqlite fetch at 79-90, then PURE aggregation 92-159), `_current_state_metrics` (~162-200: 4 aggregate queries), `collect_metrics` (~256-335: `kb.snapshot_connect` read of current_state+windows at 271-290, then `kdoc.run_board_doctor` + `krec.run_reconciler` + result assembly 291-335; `db_path` at 303; `write_metrics_snapshot` at 330). `_DEFAULT_WINDOWS` is the list of `(label, seconds)`. `kanban_board_doctor._redacted_pg_dsn()` → `postgres://host:port/db`.

---

## Task 1: Extract the pure window aggregation (refactor, sqlite byte-identical)

**Files:**
- Modify: `hermes_cli/kanban_metrics.py` (`_run_window_metrics`)

**Review:** code-quality (pure refactor under existing green tests).

- [ ] **Step 1: Run the existing metrics tests to establish the green baseline**

Run: `venv/bin/python -m pytest tests/hermes_cli -k "metrics" -q`
Expected: PASS (record the count). This is the behavior we must preserve.

- [ ] **Step 2: Extract `_aggregate_window` and make `_run_window_metrics` fetch-then-aggregate.** Replace `_run_window_metrics` with:

```python
def _aggregate_window(rows, event_rows, *, label, cutoff, now):
    """Pure aggregation over already-fetched task_runs rows + task_events kind
    rows. Backend-neutral: rows support row["col"] (sqlite3.Row or psycopg dict_row)."""
    total_runs = len(rows)
    outcome_counts = _count_map(rows, "outcome")
    status_counts = _count_map(rows, "status")
    attempts_by_task: dict[str, int] = {}
    attempts_by_profile: dict[str, int] = {}
    failure_or_reclaim_count = 0
    completion_count = 0
    blocked_count = 0
    error_count = 0
    durations: list[int] = []

    for row in rows:
        task_id = str(row["task_id"] or "")
        profile = str(row["profile"] or "unassigned")
        if task_id:
            attempts_by_task[task_id] = attempts_by_task.get(task_id, 0) + 1
        attempts_by_profile[profile] = attempts_by_profile.get(profile, 0) + 1
        outcome = str(row["outcome"] or row["status"] or "unknown")
        if outcome == "completed":
            completion_count += 1
        if outcome == "blocked":
            blocked_count += 1
        if outcome in _FAILURE_OUTCOMES or row["error"]:
            failure_or_reclaim_count += 1
        if row["error"]:
            error_count += 1
        started = int(row["started_at"] or 0)
        ended = int(row["ended_at"] or now)
        if started > 0 and ended >= started:
            durations.append(ended - started)

    event_counts: dict[str, int] = {}
    failure_event_counts: dict[str, int] = {}
    for row in event_rows:
        kind = str(row["kind"] or "unknown")
        event_counts[kind] = event_counts.get(kind, 0) + 1
        if kind in _FAILURE_EVENT_KINDS:
            failure_event_counts[kind] = failure_event_counts.get(kind, 0) + 1

    durations.sort()
    tasks_attempted = len(attempts_by_task)
    max_attempts = max(attempts_by_task.values()) if attempts_by_task else 0
    return {
        "label": label,
        "cutoff": cutoff,
        "total_runs": total_runs,
        "tasks_attempted": tasks_attempted,
        "avg_attempts_per_task": round(total_runs / tasks_attempted, 2) if tasks_attempted else 0.0,
        "max_attempts_per_task": max_attempts,
        "task_attempt_hotspots": _top_counts(attempts_by_task, limit=10),
        "attempts_by_profile": _top_counts(attempts_by_profile, limit=10),
        "outcome_counts": outcome_counts,
        "status_counts": status_counts,
        "completion_count": completion_count,
        "completion_rate": _rate(completion_count, total_runs),
        "blocked_count": blocked_count,
        "blocked_rate": _rate(blocked_count, total_runs),
        "failure_or_reclaim_count": failure_or_reclaim_count,
        "failure_or_reclaim_rate": _rate(failure_or_reclaim_count, total_runs),
        "error_count": error_count,
        "duration_seconds": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else 0,
        },
        "event_counts": dict(sorted(event_counts.items(), key=lambda item: (-item[1], item[0]))),
        "failure_event_counts": dict(sorted(failure_event_counts.items(), key=lambda item: (-item[1], item[0]))),
    }


def _run_window_metrics(conn, *, label, cutoff, now):
    if cutoff is None:
        rows = conn.execute("SELECT * FROM task_runs").fetchall()
        event_rows = conn.execute("SELECT kind FROM task_events").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE COALESCE(started_at, 0) >= ?", (cutoff,),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT kind FROM task_events WHERE COALESCE(created_at, 0) >= ?", (cutoff,),
        ).fetchall()
    return _aggregate_window(rows, event_rows, label=label, cutoff=cutoff, now=now)
```
(The aggregation body is moved verbatim — the type hints `sqlite3.Connection` on `_run_window_metrics` stay; `_aggregate_window` is untyped/backend-neutral.)

- [ ] **Step 3: Run the metrics tests — identical to the Step 1 baseline**

Run: `venv/bin/python -m pytest tests/hermes_cli -k "metrics" -q`
Expected: PASS, same count as Step 1 (behavior preserved).

- [ ] **Step 4: Commit**

```bash
git add hermes_cli/kanban_metrics.py
git commit -m "refactor(kanban): split _run_window_metrics fetch from pure aggregation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: PG read path + `collect_metrics` dispatch

**Files:**
- Modify: `hermes_cli/kanban_metrics.py` (add `_current_state_metrics_pg`, `_window_metrics_pg`, `_read_metrics_pg`; branch the read block in `collect_metrics`)
- Test: `tests/hermes_cli/kanban/test_kanban_metrics_pg.py` (create)

**Review:** spec-compliance + code-quality.

- [ ] **Step 1: Write the failing test** — `tests/hermes_cli/kanban/test_kanban_metrics_pg.py`:

```python
"""kanban metrics reads the live PG board under backend=postgres."""
import uuid
import pytest

from hermes_cli import kanban_metrics as kmet
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


@pytest.fixture
def pg(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"met_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, board
    finally:
        s.close(); pool.close()


def _seed(s):
    # 2 completed + 1 blocked -> task_runs with outcomes + terminal events
    a = s.create_task(title="a"); s.claim_task(a); s.complete_task(a, summary="ok")
    b = s.create_task(title="b"); s.claim_task(b); s.complete_task(b, summary="ok")
    c = s.create_task(title="c"); s.claim_task(c); s.block_task(c, reason="needs review")
    return a, b, c


def test_metrics_reads_live_pg_current_state(pg):
    s, board = pg
    _seed(s)
    r = kmet.collect_metrics(board=board)
    assert r["db_path"].startswith("postgres://")          # live PG, not frozen sqlite
    assert "postgres:postgres@" not in r["db_path"]          # redacted, no creds
    cs = r["current_state"]
    assert cs["task_status_counts"].get("done") == 2
    assert cs["task_status_counts"].get("blocked") == 1
    assert cs["blocked_tasks"] == 1
    # health comes from the already-PG-aware doctor/reconcile
    assert "health" in r and "reconcile_ok" in r["health"]


def test_metrics_window_outcomes_live_pg(pg):
    s, board = pg
    _seed(s)
    r = kmet.collect_metrics(board=board)
    allw = next(w for w in r["windows"] if w["cutoff"] is None)  # the all-time window
    assert allw["outcome_counts"].get("completed") == 2
    assert allw["completion_count"] == 2
    assert allw["blocked_count"] == 1


def test_metrics_write_snapshot_pg(pg, tmp_path):
    s, board = pg
    _seed(s)
    snap = tmp_path / "snap.db"
    r = kmet.collect_metrics(board=board, write_snapshot=True, snapshot_db=snap)
    assert r["persisted_snapshot"]["id"]
    assert r["persisted_snapshot"]["db_path"].startswith("postgres://")
    assert snap.exists()


def test_metrics_backend_unavailable_no_leak(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: "default")
    class _BadPool:
        def connection(self, *a, **k): raise RuntimeError("conn to secret-host:5432 failed")
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: _BadPool())
    r = kmet.collect_metrics(board="default")
    assert "secret-host" not in str(r)                       # no raw exception / DSN
    assert r["db_path"].startswith("postgres://")            # redacted
    assert r["current_state"] is not None                    # degraded shape, no raise
```
VERIFY while writing: that `s.complete_task(a, summary=...)` and `s.block_task` produce `task_runs` rows with `outcome='completed'`/`'blocked'` and the corresponding `task_events` on the PG board (inspect PostgresKanbanStore.complete_task/block_task if the counts are off — the run's `outcome` column is what `_aggregate_window` reads). `claim_task` creates the run; complete/block sets its outcome + ends it.

- [ ] **Step 2: Run — verify FAIL**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_metrics_pg.py -v`
Expected: FAIL — `collect_metrics` reads the default sqlite (`db_path` is a local path, current_state empty/from frozen sqlite).

- [ ] **Step 3: Add the PG read helpers + branch `collect_metrics`.** Add near `_current_state_metrics`:

```python
def _current_state_metrics_pg(cur, board) -> dict[str, Any]:
    cur.execute("SELECT status, COUNT(*) AS count FROM tasks WHERE board=%s "
                "GROUP BY status ORDER BY status", (board,))
    task_status_counts = {str(r["status"]): int(r["count"]) for r in cur.fetchall()}
    cur.execute("SELECT status, outcome, COUNT(*) AS count FROM task_runs WHERE board=%s "
                "GROUP BY status, outcome ORDER BY status, outcome", (board,))
    run_status_counts = {f"{r['status']}:{r['outcome'] or 'none'}": int(r["count"])
                         for r in cur.fetchall()}
    cur.execute(
        "SELECT "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_tasks, "
        "SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS spawnable_pending_tasks, "
        "SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked_tasks, "
        "SUM(CASE WHEN current_run_id IS NOT NULL THEN 1 ELSE 0 END) AS current_run_pointers, "
        "COALESCE(MAX(consecutive_failures),0) AS max_consecutive_failures, "
        "COALESCE(SUM(consecutive_failures),0) AS total_consecutive_failures "
        "FROM tasks WHERE board=%s", (board,))
    row = cur.fetchone() or {}
    cur.execute("SELECT COUNT(*) AS c FROM task_runs WHERE board=%s AND status='running'", (board,))
    running_run_rows = int((cur.fetchone() or {}).get("c") or 0)
    return {
        "task_status_counts": task_status_counts,
        "run_status_counts": run_status_counts,
        "running_tasks": int(row.get("running_tasks") or 0),
        "spawnable_pending_tasks": int(row.get("spawnable_pending_tasks") or 0),
        "blocked_tasks": int(row.get("blocked_tasks") or 0),
        "current_run_pointers": int(row.get("current_run_pointers") or 0),
        "running_run_rows": running_run_rows,
        "max_consecutive_failures": int(row.get("max_consecutive_failures") or 0),
        "total_consecutive_failures": int(row.get("total_consecutive_failures") or 0),
    }


def _window_metrics_pg(cur, board, *, label, cutoff, now) -> dict[str, Any]:
    if cutoff is None:
        cur.execute("SELECT * FROM task_runs WHERE board=%s", (board,)); rows = cur.fetchall()
        cur.execute("SELECT kind FROM task_events WHERE board=%s", (board,)); event_rows = cur.fetchall()
    else:
        cur.execute("SELECT * FROM task_runs WHERE board=%s AND COALESCE(started_at,0) >= %s",
                    (board, cutoff)); rows = cur.fetchall()
        cur.execute("SELECT kind FROM task_events WHERE board=%s AND COALESCE(created_at,0) >= %s",
                    (board, cutoff)); event_rows = cur.fetchall()
    return _aggregate_window(rows, event_rows, label=label, cutoff=cutoff, now=now)


def _empty_current_state() -> dict[str, Any]:
    return {
        "task_status_counts": {}, "run_status_counts": {}, "running_tasks": 0,
        "spawnable_pending_tasks": 0, "blocked_tasks": 0, "current_run_pointers": 0,
        "running_run_rows": 0, "max_consecutive_failures": 0, "total_consecutive_failures": 0,
    }


def _read_metrics_pg(board, *, since_epoch, now):
    """Return (db_path, current_state, windows) from the live PG board. On a
    connectivity error, degrade to a redacted db_path + empty metrics (no raise,
    no leak); the shared doctor/reconcile calls in collect_metrics already
    surface ok=False on an unreachable backend."""
    from hermes_cli.kanban import pg_pool
    from psycopg.rows import dict_row
    from hermes_cli.kanban_board_doctor import _redacted_pg_dsn
    db_path = _redacted_pg_dsn()
    try:
        pool = pg_pool.get_pool()
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current_state = _current_state_metrics_pg(cur, board)
            windows = [
                _window_metrics_pg(cur, board, label=label,
                                   cutoff=None if seconds is None else now - int(seconds), now=now)
                for label, seconds in _DEFAULT_WINDOWS
            ]
            if since_epoch is not None:
                windows.append(_window_metrics_pg(cur, board, label="since",
                                                  cutoff=int(since_epoch), now=now))
        return db_path, current_state, windows
    except Exception:
        return db_path, _empty_current_state(), []
```

Then branch the read-acquisition in `collect_metrics` (replace lines ~266-290 — the `path = ...` through the `kb.snapshot_connect` block — with the dispatch; keep everything from `doctor = kdoc.run_board_doctor(...)` onward UNCHANGED, but use `db_path` instead of `str(path)` in the result):

```python
    as_of = int(now if now is not None else time.time())
    board_name = board or kb.get_current_board()
    _backend = "sqlite"
    try:
        from hermes_cli.kanban.store import resolve_backend
        _backend = resolve_backend()
    except Exception:
        _backend = "sqlite"
    if _backend == "postgres":
        db_path, current_state, windows = _read_metrics_pg(
            board_name, since_epoch=since_epoch, now=as_of)
    else:
        path = kb.kanban_db_path(board=board)
        db_path = str(path)
        with kb.snapshot_connect(path) as conn:
            current_state = _current_state_metrics(conn)
            windows = [
                _run_window_metrics(
                    conn, label=label,
                    cutoff=None if seconds is None else as_of - int(seconds), now=as_of,
                )
                for label, seconds in _DEFAULT_WINDOWS
            ]
            if since_epoch is not None:
                windows.append(_run_window_metrics(
                    conn, label="since", cutoff=int(since_epoch), now=as_of))
    doctor = kdoc.run_board_doctor(
        board=board, ready_age_seconds=max(1, int(ready_age_seconds or 1)),
    )
    # ... rest unchanged ...
```
In the result dict, change `"db_path": str(path),` → `"db_path": db_path,`. Everything else (doctor/reconcile/health/windows/result/write_snapshot) is unchanged.

- [ ] **Step 4: Run — verify pass + sqlite metrics green**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_kanban_metrics_pg.py -v` → PASS.
Run: `venv/bin/python -m pytest tests/hermes_cli -k "metrics" -q` → existing sqlite metrics tests PASS (db_path still a local path on sqlite; behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_metrics.py tests/hermes_cli/kanban/test_kanban_metrics_pg.py
git commit -m "feat(kanban-pg): kanban metrics reads the live PG board under backend=postgres

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Verification

**Files:** none (verification only).

- [ ] **Step 1: Forbidden files untouched** — `git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_liveness.py hermes_cli/kanban_writer_daemon.py` → empty.
- [ ] **Step 2: Only `kanban_metrics.py` changed** — `git diff --stat main -- hermes_cli plugins` → only `hermes_cli/kanban_metrics.py`.
- [ ] **Step 3: DSN-leak grep** — `git diff main | grep -iE '^\+' | grep -iE 'dsn|password|postgres://' | grep -viE 'redact|host:port|_redacted_pg_dsn|postgresql://postgres:postgres@127|startswith\("postgres'` → no real leak.
- [ ] **Step 4: Full kanban + metrics suite, both backends** — `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban tests/hermes_cli/test_kanban_db.py -q` → all green.

---

## Self-review (plan author, before handoff)

- **Spec coverage:** PG read of current_state+windows (Task 2 helpers + dispatch) ✓; db_path redacted under PG (Task 2 + tests) ✓; health/ok reused from the already-PG-aware doctor/reconcile (unchanged — Task 2 keeps the shared tail) ✓; `--write-snapshot` unchanged + persists live-PG metrics (test) ✓; backend-unavailable no-raise/no-leak (`_read_metrics_pg` except + test) ✓; sqlite byte-identical (Task 1 refactor under green tests + the additive dispatch + Task 3) ✓; window aggregation reused via `_aggregate_window` (Task 1) ✓.
- **Placeholders:** none — full code for the refactor, the 4 PG helpers, the dispatch, and the tests; the one verify-note (complete_task/block_task produce the expected run outcomes) cites exactly what to confirm.
- **Type/name consistency:** `_aggregate_window(rows, event_rows, *, label, cutoff, now)` used by both `_run_window_metrics` (Task 1) and `_window_metrics_pg` (Task 2); `_current_state_metrics_pg`/`_read_metrics_pg`/`_empty_current_state` names consistent; `db_path` variable replaces `str(path)` in the result; reuses `_redacted_pg_dsn`, `_DEFAULT_WINDOWS`, `_count_map`/`_top_counts`/`_rate`/`_percentile`, `_FAILURE_OUTCOMES`/`_FAILURE_EVENT_KINDS` (all existing module-level).

## Finish + deploy (after the 3 tasks)
finishing-a-development-branch → ff-merge to main + push chad. **No process restart.** Re-enable the cron: `hermes cron resume 5be6814db0e9`. Live-verify: run the snapshot once (the script `/Users/ctao/.hermes/scripts/kanban_metrics_snapshot.sh` directly, stdout only) + confirm the persisted snapshot row's `db_path` is the PG DSN and `current_state` reflects the live board (or `hermes kanban metrics --json` shows `db_path: postgres://…`).
