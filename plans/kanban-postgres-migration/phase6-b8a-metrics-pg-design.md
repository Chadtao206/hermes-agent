# Phase 6 · B8a — PG-ify `kanban metrics` (collect_metrics) (design)

**Status:** approved (design phase). Branch `feat/kanban-pg-phase6-b8a` (worktree `.worktrees/kanban-pg-phase6-b8a`), off `main` `67883db17`.
**Origin:** assessing the paused kanban telemetry crons (this session). `kanban-metrics-snapshot` (cron `5be6814db0e9`) runs `hermes kanban metrics --json --write-snapshot`, which reads the FROZEN sqlite → stale snapshots under PG. B8a fixes the read path. Sibling deferred item: B8b (`sync_kanban_to_telemetry.py`, ops script). The retired `daily-kanban-runtime-reliability-delta-watch` stays paused. See [[kanban-pg-phase6]].

## Problem

`hermes_cli/kanban_metrics.py::collect_metrics(board=...)` resolves `path = kb.kanban_db_path(board)` and reads via `kb.snapshot_connect(path)` (sqlite) — it is NOT backend-aware. Under `kanban.backend=postgres` it reports the **frozen** `~/.hermes/kanban.db` (verified live: `kanban metrics --json` → `db_path: /Users/ctao/.hermes/kanban.db`, stale). The `kanban-metrics-snapshot` cron therefore persists stale, frozen-at-cutover reliability metrics. (It doesn't error — the snapshot WRITE goes to a separate `kanban_metrics_snapshots.db` — so it ran "ok" while silently producing stale data; this is why it was paused.)

## Goal

Under `backend=postgres`, `hermes kanban metrics [--json] [--write-snapshot]` reports the **live PG** board, so the re-enabled cron persists real trend data. sqlite path byte-identical; `kanban_db.py` import-only. Then re-enable the cron + live-verify.

## Architecture (mirrors the B1 doctor/reconciler `_pg` pattern)

`collect_metrics` reads in three places, all sqlite-coupled today:
- `_run_window_metrics(conn, label, cutoff, now)` — `SELECT * FROM task_runs [WHERE COALESCE(started_at,0)>=?]` + `SELECT kind FROM task_events [WHERE COALESCE(created_at,0)>=?]`, then ~60 lines of **pure-Python aggregation** over the rows (counts, durations/percentiles, attempt hotspots, outcome/status/event counts).
- `_current_state_metrics(conn)` — 4 aggregate queries: `tasks GROUP BY status`; `task_runs GROUP BY status,outcome`; a `tasks` SUM-CASE row (running/ready/blocked/current_run_pointers/consecutive_failures); `SELECT COUNT(*) FROM task_runs WHERE status='running'`.
- `collect_metrics` — opens `kb.snapshot_connect(path)` and calls the above per window.

### Changes (`hermes_cli/kanban_metrics.py` only)

1. **Dispatch.** At the top of `collect_metrics`, before the `kb.snapshot_connect` body:
   ```python
   try:
       from hermes_cli.kanban.store import resolve_backend
       if resolve_backend() == "postgres":
           return _collect_metrics_pg(board=board, windows=..., now=..., write_snapshot=..., snapshot_db=...)
   except Exception:
       pass  # backend undecidable -> default/upstream uses the sqlite body
   # ---- existing sqlite body, verbatim ----
   ```
   (`_collect_metrics_pg` called OUTSIDE the catch — the B1/B6 lesson: a PG error must not silently fall through to the frozen-sqlite body. It owns its own errors.)

2. **DRY refactor: split fetch from aggregation in `_run_window_metrics`.** Extract the pure-Python aggregation (lines ~92-159) into `_aggregate_window(rows, event_rows, *, label, cutoff, now) -> dict` (operates on already-fetched row sequences via `row["col"]`). `_run_window_metrics(conn, ...)` becomes: fetch the two row sets via the existing sqlite `conn.execute` queries, then `return _aggregate_window(rows, event_rows, ...)`. **sqlite behavior is identical** (same rows, same aggregation). Both `sqlite3.Row` and psycopg `dict_row` support `row["col"]`, so the aggregation is backend-neutral.

3. **`_collect_metrics_pg(board, ...)`:** lazy `from hermes_cli.kanban import pg_pool` + `from psycopg.rows import dict_row` + reuse `kanban_board_doctor._redacted_pg_dsn`. Resolve `slug = board or kb.get_current_board()`; `db_path = _redacted_pg_dsn()`. Wrap the PG reads in `try/except`→ a redacted error-shaped result (no raise/leak). Board-scoped queries:
   - window fetch: `SELECT * FROM task_runs WHERE board=%s [AND COALESCE(started_at,0) >= %s]` + `SELECT kind FROM task_events WHERE board=%s [AND COALESCE(created_at,0) >= %s]` → feed `_aggregate_window`.
   - current-state: board-scoped variants of the 4 queries (`... WHERE board=%s` on the GROUP BY / SUM-CASE / running-count). Build the same `_current_state_metrics`-shaped dict (extract a pure `_current_state_from_rows` if cleaner, or inline the PG queries returning the same dict).
   Assemble the SAME top-level result dict shape as the sqlite path: `{board: slug, captured_at, schema_version, current_state, windows, health, ok, db_path}`. Compute `health`/`ok` with the SAME logic the sqlite path uses (extract/reuse it). If `write_snapshot`, call the unchanged `write_metrics_snapshot(result, snapshot_db=...)`.

4. **`write_metrics_snapshot` unchanged** — writes the separate `kanban_metrics_snapshots.db` from the result dict; backend-agnostic. (Its `snapshot.db_path` field will now carry the redacted PG DSN instead of a sqlite path — acceptable; it's a provenance string.)

## Constraints / guarantees
- `kanban_db.py`/`kanban_liveness.py`/`kanban_writer_daemon.py`: import-only.
- sqlite path byte-identical: the dispatch is additive; the `_run_window_metrics` refactor preserves identical sqlite output (same fetch + same aggregation); `_current_state_metrics`/`collect_metrics` sqlite bodies unchanged.
- No DSN/secret in logs or the result: `db_path` redacted to `host:port/db`; error path uses `type(exc).__name__` only.
- Default backend stays sqlite in code + tests.
- Result-dict shape (keys + the snapshot DB schema) unchanged, so the snapshot DB + any downstream consumers are unaffected.

## Testing (`tests/hermes_cli/kanban/test_kanban_metrics_pg.py`, docker-PG)
- **Cross-backend parity:** seed a known board state on PG (the `store`/pg fixture: create tasks, claim+complete/block some so there are `task_runs` with outcomes + `task_events`) and the equivalent on sqlite; assert `collect_metrics` returns matching `current_state` (status counts, running/ready/blocked, consecutive-failure sums), matching window `outcome_counts`/`completion_count`/`failure_or_reclaim_count`/`event_counts`, and matching `health`/`ok`. (Drive each backend the appropriate way; the pure aggregation guarantees shape parity.)
- **`--write-snapshot`:** under PG, `collect_metrics(write_snapshot=True, snapshot_db=tmp)` persists a row to the snapshot DB whose metrics reflect the live PG board (not frozen sqlite); `db_path` in the result is the redacted PG DSN.
- **Backend-unavailable:** monkeypatch `pg_pool.get_pool` to raise → `collect_metrics` returns a result shape (ok-false / error) with no raised exception and no host/DSN leak.
- sqlite metrics tests stay green; confirm the `_run_window_metrics` refactor didn't change sqlite output (existing `test_kanban_metrics*` suite).

Test interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`; docker `postgres:16-alpine` via `HERMES_PG_TEST_DSN`; never the live Supabase DB.

## Review
Spec-compliance + code-quality. (Repo code, but read-only metrics — not the gateway/plugin_api/store_postgres adversarial-list. Standard two-stage review; extra care that the fetch/aggregate refactor keeps sqlite byte-identical + the result-dict shape matches.)

## Finish + deploy
Merge to main (ff) + push chad. **No process restart** (the cron runs `hermes kanban metrics` fresh each fire). Then **re-enable the cron**: `hermes cron resume 5be6814db0e9`. Live-verify: run the snapshot once (script directly, or `cron run`) and confirm the persisted snapshot's `db_path` is the PG DSN + the metrics reflect the live board.

## File inventory
- Edit: `hermes_cli/kanban_metrics.py` (dispatch + `_collect_metrics_pg` + `_aggregate_window` refactor + any shared current-state/health extraction).
- Test: `tests/hermes_cli/kanban/test_kanban_metrics_pg.py` (new).

## Out of scope
- **B8b** — `sync_kanban_to_telemetry.py` (ops script) PG-ification (next cycle).
- `daily-kanban-runtime-reliability-delta-watch` — retired (stays paused; redundant with the now-live reconcile watchdog + doctor).
- B4 (Auth/RLS/Realtime + live dashboard), B5 (frozen kanban.db fate), B7-tail (swarm/dispatch/archive --rm).
