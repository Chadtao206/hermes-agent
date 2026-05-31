# Kanban dashboard read-path → Postgres — design

- **Date:** 2026-05-31
- **Status:** Approved design; implementation plan to follow.
- **Context:** Post-Phase-5 cutover hardening, **Part A**. The board is live on
  Supabase Postgres (`kanban.backend=postgres`); the gateway dispatcher/notifier,
  the board doctor/liveness, the worker in-agent kanban tools, and the
  `hermes kanban` CLI all read Postgres now (read-path completion, commit
  `195a0fc65`). The **web dashboard plugin API** (`plugins/kanban/dashboard/plugin_api.py`)
  was NOT migrated: its browser-facing reads still open the **frozen**
  `<HERMES_HOME>/kanban.db` (writes to it stopped ~09:50 on 2026-05-31; Postgres
  authoritative since). So the dashboard shows a stale, frozen board.
- **Symptom:** the dashboard "running"/in-progress column is empty while
  `hermes kanban list --status running` shows live workers. The running column is
  populated entirely from `GET /board`'s `columns` (tasks with `status='running'`),
  and `/board` reads the frozen sqlite file.

## Goal

Under `kanban.backend=postgres`, the dashboard plugin API **reads and resolves the
live Postgres board** for every browser-facing read, and its create/PATCH/bulk
**writes land in Postgres** (the other writes already do). Under `sqlite` it
behaves **exactly** as today.

## Why the dashboard process already resolves Postgres (no propagation layer)

Unlike dispatcher-spawned workers (which run under a profile-scoped `HERMES_HOME`
whose config lacks `backend`/`dsn`, and so needed the Phase-readpath env-export),
the dashboard runs as `hermes dashboard --port 9119` under the **root**
`HERMES_HOME=<runtime>` and loads the root `config.yaml` directly. Therefore
`resolve_backend()` → `postgres` and `pg_pool.resolve_dsn()` → the configured DSN
in-process. The endpoints that already use the backend-aware `_store()`
(`/stats`, `/assignees`, `/runs/{id}`, comments, links, reclaim, reassign,
home-channels, profile-subs, delete) therefore already read/write Postgres after a
dashboard restart. The split-brain is confined to handlers that bypass the store
and use `_conn()` / `_readonly_snapshot_conn()` / direct SQL.

## Architecture — Approach 2 (chosen): a dashboard-local Postgres read module

The dashboard's hard reads are not entity lookups (those have clean store methods);
they are **display-shaped aggregates and tails** with no store method:
per-task link/comment counts, the parent→child progress rollup, distinct
tenants/assignees, `latest_event_id`, the `task_events` "since cursor" tail, the
active-workers join, wake-health aggregates, notifier health, and the
diagnostics row-fetch.

We do **not** bloat the gateway/worker/CLI-shared `KanbanStore` protocol with these
display shapes. Instead, following the proven precedent of
`kanban_board_doctor._run_board_doctor_pg` (board-scoped SQL via
`pg_pool.get_pool()` + `dict_row`), we add a new fork-owned module
**`plugins/kanban/dashboard/pg_reads.py`** that holds the Postgres translation of
the dashboard's existing direct-sqlite reads, scoped `WHERE board=%s`.

**The seam:** every browser-facing DB handler in `plugin_api.py` gets one branch:

```python
if resolve_backend() == "postgres":
    <new PG path: _store() methods for entities + pg_reads helpers for aggregates>
else:
    <existing sqlite body, byte-identical>
```

`_store()` (existing) supplies first-class entity reads (`list_tasks`,
`latest_summaries`, `get_task`, `list_comments`, `list_events`, `list_runs`,
`list_profile_wake_events`, `list_notifier_heartbeats`, `board_stats`,
`known_assignees`, `parent_ids`/`child_ids`). `pg_reads` supplies the rest. The PG
path assembles the **identical response dict** the sqlite path returns.

## Hard boundaries

- `hermes_cli/kanban_db.py` is **not edited** (upstream merge hot-spot); its
  helpers are imported/reused only. Same for `hermes_cli/kanban_liveness.py`.
- The **sqlite path is byte-identical**. Every change is
  `if resolve_backend()=="postgres": <new> else: <existing-verbatim>`.
- **No secret leakage** — `pg_reads` uses `pg_pool.get_pool()` and never sees the
  DSN literal; PG read failures are logged with the connection target **redacted
  to `host:port/db`** (no password), and surfaced as a controlled 503 mirroring
  the existing `_raise_kanban_db_unavailable` pattern. The DSN lives only in the
  root config + the gateway process env.
- Default backend in code/tests stays `sqlite`; the live flip is config-only
  (already done).

## Components touched

### Reads (branch added in `plugin_api.py`; PG path in `_store()`/`pg_reads`)

| Endpoint | PG path |
|---|---|
| `GET /board` ⭐ | `store.list_tasks(tenant=, include_archived=, workflow_template_id=, current_step_key=)` + `store.latest_summaries(task_ids)`; `pg_reads`: `link_counts`, `comment_counts`, `child_progress`, `diagnostics_rows`→`kanban_diagnostics.compute_task_diagnostics` (backend-agnostic), `latest_event_id`, `distinct_tenants`, `distinct_assignees`, `wake_health`, `notifier_health`. Same payload dict + column bucketing. |
| `WS /events` ⭐ | `_fetch_new` branches: `pg_reads.events_since(board, cursor, 200)` + `store.list_profile_wake_events(since_id=wake_cursor, limit=200)` |
| `GET /tasks/{id}` ⭐ | `store.get_task`, `store.latest_summary`, `store.list_comments`, `store.list_events`, `store.list_runs`, `pg_reads.links_for` (or `parent_ids`/`child_ids`), `pg_reads.diagnostics_rows([id])` |
| `GET /workers/active` | `pg_reads.active_workers(board)` (task_runs ⋈ tasks, `ended_at IS NULL AND worker_pid IS NOT NULL AND t.status='running'`) |
| `GET /diagnostics` | `pg_reads.diagnostics_rows(board)` + task-title/status/assignee fetch (via `store.list_tasks` or pg_reads) |
| `GET /wake-health/details` | `pg_reads.wake_health` + `pg_reads.wake_health_rows` |
| `GET /tasks/{id}/log` + `WS /tasks/{id}/log/stream` | existence check via `store.get_task`; log content stays filesystem (board-scoped path, backend-independent) |
| `GET /boards` `_board_counts` | active/`default` board via `store.board_stats()`/`pg_reads`; non-default on-disk boards → `{}` (single-board on PG) |
| `GET /reconcile` | **graceful no-op** under PG: empty actions + a "reconcile not yet available on postgres" note; do NOT call sqlite-only `run_reconciler`. Real PG reconciler deferred to Phase 6. |
| `GET /doctor` | **unchanged** — `run_board_doctor` already routes to `_run_board_doctor_pg`. |

### Writes (route through `_store()` under PG; sqlite byte-identical)

| Endpoint | PG path |
|---|---|
| `POST /tasks` | `store.create_task(...)` then read-back `store.get_task`; dispatcher-presence warning probe unchanged |
| `PATCH /tasks/{id}` | open **one** store; per-branch store methods (`assign_task`/`complete_task`/`block_task`/`schedule_task`/`unblock_task`/`set_status_direct`/`archive_task`/`set_task_priority`/`edit_task_fields`); existence/status reads via `store.get_task`; the 409 blocking-parents enrichment via `pg_reads.parents_blocking_ready` |
| `POST /tasks/bulk` | open one store; loop ids calling store methods; pre-batch state via `store.get_task` |

Already backend-aware via `_store()` (no change): `DELETE /tasks/{id}`,
`/tasks/{id}/comments`, `/links` (POST/DELETE), `/tasks/{id}/reclaim`,
`/tasks/{id}/reassign`, `/runs/{id}/terminate`, `/tasks/{id}/home-subscribe`,
`/tasks/{id}/profile-subs`, `/stats`, `/assignees`, `/runs/{id}`,
`/runs/{id}/inspect`.

Out of scope (left as-is, documented): `POST /dispatch` (single-writer no-op note;
the gateway dispatches), boards CRUD writes (filesystem/multi-board), profiles &
orchestration (config/filesystem). `POST /tasks/{id}/specify` and `/decompose`
route through separate `kanban_specify`/`kanban_decompose` modules that may still
be sqlite-coupled internally — flagged as a Phase-6 follow-up, not Part A.

## Data flow

`/board` and `/tasks/{id}` build the **identical** response dict from PG sources,
so the frontend (which does a debounced **full `/board` refetch** on every
`/events` message — no delta patching) renders live data unchanged. `/events`
keeps the 0.3 s poll loop, one pooled read per tick (same cadence the sqlite
snapshot used; acceptable against the transaction pooler). Writes: store method →
commit to PG → the next `/board` refetch shows it.

## Error handling / edge cases

- **`list_runs` state filters:** `PostgresKanbanStore.list_runs(state_type=, state_name=)`
  raises `NotImplementedError` (phase-2-tail). `GET /tasks/{id}` must return a
  clean **400** ("run state filtering not yet supported on postgres") when
  `run_state_type`/`run_state_name` are passed under PG, not a 500. The frontend
  does not pass them by default.
- **`list_tasks` ordering:** confirm `PostgresKanbanStore.list_tasks` default order
  matches sqlite (priority DESC, created_at ASC) so column ordering is identical;
  `get_board` does not pass `order_by` (PG `list_tasks(order_by=)` is NotImplemented).
- **notifier_health:** confirm where notifier heartbeats live under PG. If they are
  not in the PG board, `pg_reads.notifier_health` degrades to the existing
  "unavailable" severity (the sqlite path already has this branch) rather than
  erroring. Resolve in the plan.
- **Single-board:** PG scopes to `default`; `?board=` beyond `default` is not
  honored on the PG path (documented limitation, consistent with the read-path
  completion). `_resolve_board(None)`/`default` is fine; a stale non-default slug
  from the frontend's `localStorage` may 404 via the filesystem `board_exists`
  check — acceptable single-board, flagged.
- sqlite-only corruption helpers (`_is_corrupt_db_error`, `_lenient_text_factory`,
  `_raise_kanban_db_unavailable`) stay on the sqlite branch; the PG branch has its
  own redacted-503 wrapper.

## Testing

- **`tests/plugins/conftest.py`** (new): a session-scoped `_pg_dsn` fixture
  (uses `HERMES_PG_TEST_DSN` if set, else a throwaway `postgres:16-alpine`
  container) so plugin tests can request a PG board — the existing fixture lives
  under `tests/hermes_cli/kanban/` and is not visible to `tests/plugins/`.
- **`tests/plugins/test_kanban_dashboard_plugin_pg.py`** (new, docker-PG): under
  `backend=postgres`, assert `GET /board`, `GET /tasks/{id}`, `WS /events`,
  `GET /workers/active`, `GET /diagnostics`, `GET /wake-health/details` return
  **live PG** data; create/PATCH/bulk land in PG; `/reconcile` returns the graceful
  no-op; `/board` running column reflects a PG `running` task.
- **Parity test:** seed identical task graphs in sqlite + PG, assert `GET /board`
  and `GET /tasks/{id}` JSON are identical modulo ids/timestamps (drift defense —
  the technique `build_worker_context` used).
- **Regression:** existing `tests/plugins/test_kanban_dashboard_plugin{,_api}.py`
  stay green (sqlite byte-identical) — 140 tests at baseline.
- **Boundaries:** `git diff main -- hermes_cli/kanban_db.py` empty; no DSN/`pooler.supabase`
  literal in the diff; default backend stays sqlite.
- **Interpreter:** `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`;
  PG via the docker fixture / `HERMES_PG_TEST_DSN`. Never the live Supabase DB.

## Risks & mitigations

- **Payload drift between backends** → the sqlite↔PG `/board` + `/tasks/{id}`
  parity test fails loudly if the assembled dicts diverge.
- **`pg_reads` is a second place running board SQL** (not the store) → accepted;
  it mirrors the doctor/liveness precedent and confines dashboard-only display
  shapes to one bounded fork-owned module, keeping the shared store protocol lean.
- **Live `plugin_api.py` is import-time core for the dashboard** → all changes are
  additive `if pg:` branches with a verbatim sqlite `else:`; activated only after a
  dashboard restart on the PG backend.
- **Secret leakage via PG error text** → redact connection targets to `host:port/db`;
  never log the DSN.

## Success criteria

- Under `kanban.backend=postgres`, after a dashboard restart: the board view (incl.
  the running column), task drawer, live `/events` updates, workers/diagnostics/
  wake-health views all reflect the **live PG board**; create/PATCH/bulk writes land
  in PG and appear on the next refetch.
- `/reconcile` degrades gracefully; `/doctor` already works.
- sqlite path byte-identical; `kanban_db.py` + `kanban_liveness.py` unedited; no DSN
  in any output/log.
- sqlite↔PG `/board`+`/tasks/{id}` parity test green; existing dashboard tests green
  on both backends.
