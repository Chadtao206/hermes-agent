# Kanban → Postgres (Supabase) migration — design

- **Date:** 2026-05-30
- **Status:** Approved design; implementation plan to follow.
- **Scope (this project):** the kanban board only — `kanban.db` plus its sidecars
  `kanban_metrics_snapshots.db` and `kanban_notifier_heartbeats.db`. Other Hermes
  databases (`state.db`, `memory_store.db`, `control_center.db`, `ylopo_kg.db`) are
  out of scope.

## Context & motivation

A week of incidents on the kanban SQLite board — recurring `disk I/O error`
storms, durable corruption from concurrent writers, read-after-write "task not
found" causing **duplicate task creation**, a WAL sidecar split-brain causing
silent write-loss — all trace to two SQLite properties: it is an embedded
file database with no server arbitrating access, and its WAL mode coordinates
across processes via on-disk `-wal`/`-shm` sidecars that are fragile under many
short-lived worker processes (especially on macOS APFS).

The mitigations built during that week — the single-writer daemon,
`write_session`/RemoteWriter, `OP_ALLOWLIST`, `snapshot_connect`,
corruption-recovery, IOERR/`malformed` retries, dashboard write-routing — are
SQLite life-support. A client-server database (Postgres) with MVCC removes the
entire class of problems natively and lets most of that machinery be deleted.

Postgres will also be **cloud-hosted (Supabase)** to enable a future
web-accessible dashboard.

### Fork / upstream constraint (the design driver)

`hermes_cli/kanban_db.py` is upstream (NousResearch) code; this fork (`chad`)
regularly integrates upstream (`integrate/hermes-origin-*`). The fork has already
diverged heavily on `kanban_db.py` (single-writer daemon + recent fixes), making
it a merge-conflict hot spot. **Decision: isolate and own kanban as a fork
subsystem behind a stable interface.** All Postgres code lives in NEW fork files;
the upstream SQLite logic is retained as one backend; the rest of Hermes keeps
merging upstream cleanly.

## Decisions (locked)

1. **Scope:** kanban only (board + metrics + heartbeat sidecars).
2. **Architecture:** Approach 1 — a `KanbanStore` interface with two adapters
   (`SqliteKanbanStore`, `PostgresKanbanStore`), backend chosen by config.
3. **Host:** Supabase (managed Postgres + transaction pooler + Auth/Realtime for
   the future web dashboard).
4. **Cutover:** migrate existing data; maintenance-window cutover; dry-run first;
   keep the SQLite file as a short-window rollback.

## Goals / non-goals

**Goals**
- Eliminate the SQLite concurrency/corruption/visibility failure classes for kanban.
- Keep non-kanban Hermes upstream-mergeable; quarantine kanban behind an interface.
- Preserve all current board data and behavior (semantics unchanged for callers).
- Land in cloud Postgres to unlock a future web dashboard.

**Non-goals (deferred)**
- Web-dashboard hosting, Supabase Auth/RLS, and Realtime (replacing the polling
  notifier) — a follow-on project once Postgres is the backend.
- Migrating `state.db`, `memory_store.db`, `control_center.db`, `ylopo_kg.db`.
- Zero-downtime cutover (single-box system; a maintenance window is acceptable).

## Architecture

### Module layout (merge-friendliness)

```
hermes_cli/kanban/                  # NEW fork-owned package
  store.py            # KanbanStore Protocol (stable interface) + kanban_store() factory
  store_sqlite.py     # SqliteKanbanStore — thin adapter delegating to kanban_db.py
  store_postgres.py   # PostgresKanbanStore — psycopg + Supabase
  pg_schema.sql       # Postgres DDL (translated from SCHEMA_SQL)
  pg_pool.py          # psycopg_pool setup + Supabase connection config
  migrate_sqlite_to_pg.py   # export / transform / load + verify + dry-run
hermes_cli/kanban_glue.py         # NEW: dispatcher/notifier glue extracted from gateway/run.py
```

- `hermes_cli/kanban_db.py` is retained as the **SQLite backend** (kept
  upstream-mergeable; minimal edits). All Postgres code is in new files → zero
  upstream conflict.
- Backend selected by config `kanban.backend: sqlite | postgres` (default
  `sqlite`, so upstream and other deployments are unaffected).
- `gateway/run.py`'s kanban integration shrinks to a thin call into
  `kanban_glue`, turning the existing merge hot spot into a small stable hook.

### `KanbanStore` interface

A `Protocol` exposing the **external** kanban operations with **no `conn`
argument** (the store owns its connection/pool):

- Task CRUD + lifecycle: `create_task`, `get_task`, `list_tasks`,
  `complete_task`, `block_task`, `unblock_task`, `schedule_task`, `archive_task`,
  `assign_task`, `reassign_task`, `reclaim_task`, `set_status_direct`,
  `set_task_priority`, `edit_task_fields`, `delete_task`.
- Links & comments: `link_tasks`, `unlink_tasks`, `add_comment`, list/read.
- Notifications: `add_notify_sub`, `remove_notify_sub`, `list_notify_subs`,
  `claim_unseen_events_for_sub`, cursor ops.
- Profile-event subs/claims/wake: add/remove/list + claim.
- Dispatch support: ready promotion (`recompute_ready`), atomic claim, run
  bookkeeping (`_end_run`-equivalent), stale/crash reclaim, heartbeats.
- Metrics snapshots.

Callers (agent tools, dashboard, CLI, glue) obtain a store from
`kanban_store(board=…)` and call `store.create_task(...)`. The ~60 internal
helpers in `kanban_db.py` (`_end_run`, `_append_event`, migrations) stay private
to the SQLite backend.

### Postgres backend, concurrency, and removed machinery

- **Connection:** `psycopg` 3 against the Supabase **transaction pooler**
  (handles the worker-process connection storm). A small `psycopg_pool` per
  process. New dependency: `psycopg[binary,pool]`. Choose a Supabase region near
  the gateway to bound per-query latency. No `LISTEN/NOTIFY` is used, so
  transaction-mode pooling is safe.
- **Schema (`pg_schema.sql`):** `t_…` ids stay `TEXT`; epoch-int timestamps stay
  `BIGINT`; `task_runs.id` autoincrement → `BIGINT GENERATED BY DEFAULT AS
  IDENTITY`; JSON payload columns → `JSONB`; UTF-8 enforced by Postgres (no more
  torn non-UTF-8 rows). 10 tables: `tasks`, `task_comments`, `task_events`,
  `task_links`, `task_runs`, `kanban_notify_subs`, `kanban_profile_event_subs`,
  `kanban_profile_event_claims`, `kanban_profile_wake_events`,
  `kanban_notifier_heartbeats` (+ metrics snapshots).
- **Atomic dispatch:** the `claim_lock`/`claim_expires` CAS becomes
  `UPDATE tasks SET … WHERE id IN (SELECT id FROM tasks WHERE status='ready' …
  FOR UPDATE SKIP LOCKED) RETURNING …` — native, race-free, no WAL-lock reliance.
- **Removed under `backend=postgres`** (unused; retained only behind
  `backend=sqlite`): single-writer daemon, `write_session`/RemoteWriter,
  `OP_ALLOWLIST`, `snapshot_connect`, corruption-recovery, IOERR/`malformed`
  retry, dashboard write-routing. MVCC provides correct concurrent access and
  read-after-write consistency for free.

## Migration & cutover

`migrate_sqlite_to_pg.py`:
- Read the SQLite board read-only; transform rows; bulk-load into Postgres in FK
  order (`tasks` → `task_links`/`task_comments`/`task_events`/`task_runs`/subs/
  claims), preserving `t_…` ids and epoch timestamps; set the `task_runs.id`
  IDENTITY sequence to `max+1`.
- **Dry-run** into a throwaway Supabase schema; verify per-table row counts +
  spot integrity + a `doctor`-equivalent pass.
- **Cutover runbook:** `hermes gateway stop` (quiesce) → final export → load →
  flip `kanban.backend=postgres` → restart gateway + dashboard → verify.
- **Rollback:** keep the SQLite file; flip back to `sqlite` — valid only in the
  window before Postgres takes divergent writes.
- **Sidecars:** heartbeats are ephemeral → start fresh in Postgres; metrics
  snapshots → start fresh (history low-value) unless migration is requested.

## `gateway/run.py` glue extraction (highest-risk item)

Move the dispatcher-loop and notifier-watcher **kanban bodies** into
`hermes_cli/kanban_glue.py`, exposing `run_dispatch_tick(store)` /
`run_notifier_tick(store, adapters)`. `gateway/run.py` keeps only the asyncio
task scheduling and calls the glue — shrinking the merge hot spot to a thin hook
and making dispatch/notify backend-agnostic. This is the most code-movement and
the riskiest part; it is sequenced after the store + PG backend are proven.

## Testing

- **Store conformance suite:** one behavioral test set parametrized over *both*
  `SqliteKanbanStore` and `PostgresKanbanStore`, asserting identical semantics —
  create / claim (`SKIP LOCKED`) / complete / idempotency / links / subs /
  event-claiming / `recompute_ready`. This proves Postgres matches SQLite.
- **PG test backend:** local Postgres via docker/testcontainers in CI, plus a
  Supabase test schema for integration.
- Existing kanban tests stay green on the default `sqlite` backend.
- **Migration-parity test:** fixture SQLite → migrate → assert equality.

## Risks & mitigations

- **Per-query latency to cloud Postgres** vs local SQLite. Mitigate: transaction
  pooler, near region, reduce round-trips (batch reads in chatty paths like the
  dispatcher). Measure before/after.
- **Connection storms from worker processes.** Mitigate: Supabase pooler +
  bounded `psycopg_pool`; prefer routing worker writes/reads through a pool
  rather than a fresh connection per process.
- **Behavioral drift between backends.** Mitigate: the conformance suite is the
  gate; no cutover until it is green against Postgres.
- **Glue extraction touches a core file.** Mitigate: sequence it last; keep the
  gateway change to a thin hook; cover with the existing gateway tests.
- **Cutover data loss.** Mitigate: dry-run + row-count/integrity verification;
  quiesce before final export; keep SQLite as rollback.

## Phasing (each independently shippable)

1. **Store interface + `SqliteKanbanStore`**; route all kanban callers through
   it — zero behavior change on the sqlite backend (biggest refactor; de-risked
   by the conformance suite).
2. **PG schema + `PostgresKanbanStore` + pool**; conformance suite green vs PG.
3. **Glue extraction** (backend-agnostic dispatcher/notifier).
4. **Migration tooling + dry-run**.
5. **Cutover** (maintenance window) + verify + rollback window.
6. *(Later)* retire the SQLite-only life-support once Postgres is proven; begin
   the web-dashboard phase (Supabase Auth/RLS/Realtime).

## Success criteria

- All kanban callers go through `KanbanStore`; conformance suite passes against
  both backends.
- Live board runs on Supabase with no `disk I/O error`/corruption/duplicate
  classes; read-after-write is consistent.
- Non-kanban upstream merges remain clean; Postgres code is entirely in new
  files; `gateway/run.py` kanban surface is a thin hook.
- All existing board data migrated with verified parity.
