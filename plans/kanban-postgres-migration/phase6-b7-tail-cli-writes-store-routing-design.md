# Phase 6 — B7-tail: migrate the remaining sqlite-coupled CLI write commands to the store

**Status:** design (awaiting review)
**Builds on:** B7 (`phase6-b7-cli-write-commands-store-routing-design.md`), Part A dashboard read/write pattern, and the just-committed `a9859bb55` (closeout/failure guards → store).

## Problem

The live board is `kanban.backend: postgres` with `single_writer_daemon: true`. A class of
`hermes kanban` WRITE subcommands still open a direct writable `kb.connect()` /
`kb.connect_closing()` and call `kb.<write>(conn, …)` instead of routing through the
backend-aware `_make_store()`. Under the live config this is double-broken: the
single-writer guard refuses the direct writable connect (`DirectWriteForbidden`), and even
without the guard `kb.connect()` is SQLite → it would write the **frozen** `kanban.db`, not the
live PG board.

B7 migrated **Tier 1** (`wake-arm`, `profile-subs add`, `claim`, `notify subscribe`). B7-tail
covers the rest:

- **Tier 2 — `swarm`** (`_cmd_swarm` → `ks.create_swarm(conn, …)`)
- **Tier 3 — `dispatch`** (`_cmd_dispatch` → `kb.dispatch_once(conn, …)`)
- **Tier 4 — `archive --rm`** (`_cmd_archive` purge path → `kb.delete_archived_task(conn, …)`)

`repair-db` is SQLite-only maintenance (file VACUUM/checkpoint/replace) — **N/A under PG**, out
of scope.

## Common pattern (inherited from B7 / Part A)

1. Route the CLI write through `store = _make_store(); try: … finally: store.close()`.
2. New store-surface methods go on the `KanbanStore` Protocol + **both** impls. PG impls live on
   `PostgresKanbanStore` and mirror the SQLite `kanban_db` semantics; SQLite impls wrap the
   existing `kb.*` helpers.
3. **Forbidden files import-only:** `hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`,
   `hermes_cli/kanban_writer_daemon.py`. Do not edit them.
4. Cross-backend conformance tests (`tests/hermes_cli/kanban/test_store_conformance.py`) +
   CLI-level tests. Adversarial code review for any `store_postgres.py` touch (live-core).
5. SQLite behavior preserved (branch/route, don't rewrite); note where it is *fixed* rather than
   byte-identical (the old direct connect was itself broken under single-writer).

## Tier 2 — `swarm`

**Now:** `_cmd_swarm` opens `kb.connect_closing()` and calls
`kanban_swarm.create_swarm(conn, goal=…, workers=…, …)`. `create_swarm` and its
`latest_blackboard(conn, root)` helper take a SQLite `conn` and call
`kb.create_task/complete_task/link_tasks`.

**Design:**
- Refactor `kanban_swarm.create_swarm(conn, …)` → `create_swarm(store, …)`: replace every
  `kb.create_task/complete_task/link_tasks(conn, …)` with the matching
  `store.create_task/complete_task/link_tasks(…)` (all already on the Protocol).
- Refactor `latest_blackboard(conn, root)` → `latest_blackboard(store, root)`, reading the
  root's latest run metadata via `store.latest_run(root)` (confirm `latest_run` surfaces
  `metadata`; if not, add a tiny read helper). This drives the idempotency/topology-recovery
  branch, so its return shape must match today's dict.
- CLI: `store = _make_store(); try: created = ks.create_swarm(store, …) finally: store.close()`.
- **Blast radius:** only `cli.py:_cmd_swarm` + `tests/hermes_cli/test_kanban_swarm.py` (3 calls)
  call `create_swarm`. Update the test calls to pass a store (parametrize both backends if the
  conformance `store` fixture is reusable; otherwise sqlite store).
- No new Protocol methods.

**Parity risks:** `create_task(idempotency_key=…)` behavior (PG handles it — verified in the
`a9859bb55` audit) and the topology-recovery read must be equivalent across backends.

## Tier 4 — `archive --rm` (purge)

**Now:** the `--rm` purge path opens `kb.connect_closing()` and calls
`kb.delete_archived_task(conn, tid)` (explicit "not in the store protocol; use raw conn"
comment). It **deletes immediately**, archived-only guard.

**Design — store method:**
- Add `delete_archived_task(self, task_id: str) -> bool` to the `KanbanStore` Protocol.
- **SQLite impl:** wrap `kb.delete_archived_task` through the store's own connection (mirror how
  the other sqlite store methods wrap `kb.*`).
- **PG impl** (`PostgresKanbanStore`): board-scoped, single `conn.transaction()`, mirroring
  SQLite's cascade exactly:
  1. `SELECT status FROM tasks WHERE board=%s AND id=%s FOR UPDATE`; if missing or
     `status != 'archived'` → return `False` (no delete).
  2. `DELETE FROM task_links WHERE board=%s AND (parent_id=%s OR child_id=%s)`
  3. `DELETE FROM task_comments WHERE board=%s AND task_id=%s`
  4. `DELETE FROM task_events  WHERE board=%s AND task_id=%s`
  5. `DELETE FROM task_runs    WHERE board=%s AND task_id=%s`
  6. `DELETE FROM kanban_notify_subs WHERE board=%s AND task_id=%s`
  7. `cur = DELETE FROM tasks WHERE board=%s AND id=%s`; return `cur.rowcount == 1`.
  - **Parity choice:** mirror SQLite's exact 6 tables. The PG-only `kanban_profile_event_subs` /
    `_claims` / `_wake_events` are **not** purged → a purged task may leave orphan profile-sub
    rows. Documented as a small known gap (deferred); keeps byte-parity with the SQLite path that
    conformance asserts against.

**Design — destructive safety guard (CLI):**
- Change `archive --rm <ids…>` to **dry-run by default**:
  - Default (no `--confirm`): validate each id via `store.get_task` (must exist and be
    `archived`), print the would-delete plan — each archived id plus the per-table row counts that
    would be deleted, gathered via existing read-only store reads (`list_events` / `list_comments`
    / `list_runs` / `parent_ids`+`child_ids` / `list_notify_subs`) — mutate nothing, and exit 0
    with a `"(dry-run — pass --confirm to permanently delete)"` notice. Ids that are
    missing/not-archived are reported as not-deletable.
  - With `--confirm`: call `store.delete_archived_task(tid)` per id (current delete behavior).
- **Behavior change** from today's immediate delete → documented in `--help` + the kanban docs.
  Acceptable per the explicit decision to add a confirm guard.

**Tests:** conformance `delete_archived_task` on both backends (archived → deleted + cascade
gone; non-archived → refused, no mutation); CLI test for dry-run (no mutation) vs `--confirm`
(deletes). **Verification will NOT run a real delete on the live board without explicit
go-ahead** — exercise via the conformance docker PG / sqlite, or an isolated throwaway PG board.

## Tier 3 — `dispatch`

**Now:** `_cmd_dispatch` opens `kb.connect_closing()` and calls
`kb.dispatch_once(conn, dry_run=…, max_spawn=…, …)` — a **full claim+spawn tick**
(`spawn_fn` defaults to `_kb._default_spawn`). `--dry-run` is a non-mutating preview.

**Design — live tick (non-dry-run):** route through the existing backend-agnostic
`kanban_glue.run_dispatch_tick(store, …)`, copying the gateway's exact wiring
(`gateway/run.py:6987`):

```
store = _make_store()
summary = run_dispatch_tick(
    store, board=<current>,
    spawn_fn=_kb._default_spawn,
    resolve_workspace=_kb.resolve_workspace,
    profile_exists=<hermes_cli.profiles.profile_exists or None>,
    terminate_fn=lambda pid, lock: _kb._terminate_reclaimed_worker(pid, lock, signal_fn=os.kill),
    pid_alive_fn=_kb._pid_alive,
    classify_exit_fn=_kb._classify_worker_exit,
    max_spawn=…, max_in_progress=…, failure_limit=…,
    default_assignee=…, max_in_progress_per_profile=…,
)
```
`run_dispatch_tick` returns a **summary dict** whose int keys match `DispatchResult.summary()`,
so the CLI's existing human/JSON output maps onto it (small adapter; some `dispatch_once`-only
list fields like `skipped_per_profile_capped` may be absent → guard with `.get`).

**Design — `--dry-run` (read-only preview):** `run_dispatch_tick`/`dispatch_plan` always
*claim* (mutate), so the legacy dry-run cannot route through it. Implement a **read-only
preview** that mutates nothing:
- Compute from store reads (`store.list_tasks(status='ready')` + assignee/spawnable filters, plus
  promotable `todo` whose parents are all done), applying the global/per-profile caps against the
  current running count — best-effort.
- Output the candidate tasks the next real tick *would* consider, clearly labeled
  **non-authoritative** (the board can change before the real tick; the live tick is the source
  of truth). It does **not** attempt to perfectly replicate `dispatch_once(dry_run=True)`'s
  internal ordering/cap interactions.
- Implementation choice (settle in the plan): a small read-only `preview_dispatch(...)` store
  method (cross-backend, conformance-tested) **or** compute in the CLI from existing store reads.
  Prefer the store method if the cap logic is non-trivial, for testability.

**Design — double-dispatch guard:** a manual tick races the gateway's embedded dispatcher
(10s here). When `find_gateway_pids()` is non-empty and `kanban.dispatch_in_gateway` is on, print
a **warning** that the gateway is already dispatching and a manual tick may double-spawn
(warn, do not hard-block).

**Tests:** CLI dispatch routes through `run_dispatch_tick` with a mock `spawn_fn` (assert claim +
record_spawn_success path); `--dry-run` preview asserts **no status mutation**; the
gateway-running warning fires.

## Store Protocol additions

- `delete_archived_task(self, task_id: str) -> bool` (Tier 4).
- (Tier 3) optionally `preview_dispatch(...) -> list[…]` (read-only) — or compute in CLI.
- `auto_block_unclosed_worker_turn` already added in `a9859bb55`.

## Sequencing (each: implement → conformance/CLI tests → review)

1. **Tier 2 swarm** — mechanical, isolated, no Protocol change.
2. **Tier 4 archive --rm** — store method + dry-run/`--confirm` guard.
3. **Tier 3 dispatch** — glue wiring + read-only preview + double-dispatch warning (most complex).

## Testing & verification

- Cross-backend conformance: `delete_archived_task` (+ `preview_dispatch` if a store method).
- CLI tests: swarm-via-store; archive `--rm` dry-run vs `--confirm`; dispatch via glue + read-only
  dry-run + gateway-running warning.
- Full kanban suite both backends. **No docker in this env** → PG-param conformance may skip;
  mitigate the PG-specific methods with an **isolated throwaway-board** live-PG smoke
  (board slug `_b7tail_smoke`, cleaned up), and **no live mutation of the `default` board / no
  real `--rm` delete without explicit user go-ahead**.

## Risks / open items

- Tier 3 dry-run preview is **approximate** (not a perfect replica of `dispatch_once(dry_run)`);
  acceptable + documented.
- `archive --rm` becomes dry-run-by-default — **behavior change**; update help + docs.
- PG `delete_archived_task` leaves `kanban_profile_event_*` orphans for purged tasks (parity
  choice) — deferred note.
- Manual `dispatch` vs gateway dispatcher: warn, don't block.

## Out of scope

- `repair-db` (SQLite-only file maintenance).
- Real mutation of the live `default` board during verification.
- Re-homing / deleting the stale `~/.hermes/hermes-agent/scripts/` duplicate (separate B8b-deferred item).
