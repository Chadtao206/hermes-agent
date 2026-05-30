# Kanban Postgres Migration — Phase 3: close phase-2-tail + extract backend-agnostic dispatcher/notifier glue

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This is the **highest-risk phase** (touches `gateway/run.py`, 21k lines, the live deployment's dispatch/notify core). Be conservative; the existing gateway + dispatch + notifier test suites are the acceptance gate.

**Goal:** (A) Bring `PostgresKanbanStore` to real parity on the dispatcher/notifier data-operations Phase 2 deferred (the `phase-2-tail` items), proven by extending the conformance suite; then (B) extract the kanban dispatcher + notifier tick bodies from `gateway/run.py` into a new backend-agnostic `hermes_cli/kanban_glue.py` (`run_dispatch_tick(store, spawn_fn, …)` / `run_notifier_tick(store, adapters, …)`), so dispatch/notify run identically on sqlite and postgres — with **zero behavior change** on the default sqlite backend.

**Architecture (locked in brainstorming):**
1. **Dispatch boundary:** the glue orchestrates; the store exposes the DB data-ops. `kanban_db.dispatch_once` stays UNTOUCHED (the standalone `hermes kanban daemon` CLI + its ~40 `test_dispatch_*` tests keep using it). The gateway switches from calling `dispatch_once` to calling `kanban_glue.run_dispatch_tick(store, spawn_fn)`, which does: store reclaim/recompute → `store.dispatch_plan()` (returns tasks-to-spawn, already claimed) → glue spawns each via `spawn_fn` → `store.record_spawn_success/ failure(...)`. The SQLite store's new data-ops **delegate to the existing `kanban_db` primitives** (no logic duplication).
2. **`complete_task`:** full DB-level gates (hallucinated-cards, PR-head, external-handoff), closeout packet, prose phantom-scan, failure-counter clear, recompute — implemented in `PostgresKanbanStore` to parity. OS side-effects (`_cleanup_workspace`: dir rmtree + tmux kill) factored into a **glue/caller hook**, not store SQL, for BOTH backends.
3. **Notifier heartbeat:** keep the board-independent **SQLite sidecar** (`kanban_notifier_heartbeats.db`) for both backends; `PostgresKanbanStore.record/list_notifier_heartbeat` delegate to the same sidecar (telemetry stays out of the board store).

**Tech Stack:** Python 3.11, psycopg 3 (postgres backend from Phase 2), existing `hermes_cli.kanban` package + conformance suite, `gateway/run.py` (asyncio gateway).

**Reference spec:** `plans/kanban-postgres-migration/design.md` (§"gateway/run.py glue extraction"). Phases 1–2 merged to `main`. Surface map: see the Phase-3 surface analysis (dispatcher tick `gateway/run.py:7221`, notifier tick `gateway/run.py:5777`, `dispatch_once` `kanban_db.py:7643`, `complete_task` `kanban_db.py:4742`).

---

## Boundary: what becomes a store method vs stays in glue

**Store (`KanbanStore`) data-ops** (new in Part A; SQLite delegates to `kanban_db`, Postgres reimplements):
- Dispatch DB-ops: `dispatch_plan(*, max_spawn, max_in_progress, failure_limit, stale_timeout_seconds, default_assignee, max_in_progress_per_profile, ttl_seconds) -> DispatchPlan` (does reclaim-stale + detect-crashed + detect-stale + max-runtime-DB-side + promote-cleared-scheduled + recompute_ready + ready-scan + per-task respawn-guard/validation + **claim**, returning the list of `(task, workspace_kind)` to spawn — NO process spawn), `record_spawn_success(task_id, pid)`, `record_spawn_failure(task_id, error, *, failure_limit)`, plus the individual reclaim ops if the glue needs them (`release_stale_claims`, `detect_crashed_workers`, `detect_stale_running`, `enforce_max_runtime_db(task_ids)` — the DB mutation only; the SIGTERM/SIGKILL stays in glue).
- `complete_task` full semantics (gates/closeout/prose-scan/failure-clear), with an injected `on_cleanup(task_id)` hook for OS workspace cleanup.
- `_record_task_failure`-equivalent: `record_task_failure(task_id, error, *, outcome, failure_limit, release_claim, end_run) -> bool` (breaker/gave_up).
- `heartbeat_worker(task_id, *, note, expected_run_id, min_event_interval_seconds) -> bool`.
- Notifier-heartbeat sidecar: `record_notifier_heartbeat(**kw)`, `list_notifier_heartbeats(**kw)` (delegate to the SQLite sidecar in BOTH backends).
- Event-claiming cursor mutation: `advance_notify_cursor`, `rewind_notify_cursor` (chat); `advance_profile_event_cursor`, `rewind_profile_event_cursor` (profile). (`claim_unseen_events_for_sub`/`_profile_sub`, `list_notify_subs`, `list_profile_event_subs`, `remove_notify_sub` already exist from Phases 1–2.)
- Profile-wake recording: `record_profile_wake_success(**kw) -> int`, `record_profile_wake_failure(**kw) -> int`, `list_profile_wake_events(**kw) -> list[dict]`.

**Glue (`kanban_glue.py`)** — orchestration only, NO SQL: config resolution, single-writer daemon routing (`_wd.lookup_daemon(...).execute(...)` vs direct store call), `spawn_fn` (`subprocess.Popen` of the worker), `enforce_max_runtime` SIGTERM/SIGKILL signalling, OS workspace cleanup hook, adapter `.send(...)` + artifact uploads, per-event-kind message formatting, profile-wake `subprocess.Popen`, `sub_fail_counts`/`MAX_SEND_FAILURES`, zombie reap, prompt construction.

**Gateway (`gateway/run.py`)** — keeps: the asyncio watcher loops + sleep slices, board enumeration, SQLite fingerprint/hot-replacement detection + `disabled_boards`/`disabled_db_paths` quarantine maps (SQLite-specific), health telemetry, auto-decompose. It builds a `store` per board and calls the glue tick functions.

---

## File structure

- Modify: `hermes_cli/kanban/store.py` — add the new Protocol methods (Part A).
- Modify: `hermes_cli/kanban/store_sqlite.py` — add delegations to `kanban_db` for the new methods (+ `OP_ALLOWLIST` entries in `hermes_cli/kanban_writer_daemon.py`).
- Modify: `hermes_cli/kanban/store_postgres.py` — reimplement the new methods (close phase-2-tail).
- Modify: `tests/hermes_cli/kanban/test_store_conformance.py` — extend coverage for each new method (runs vs both backends).
- Create: `hermes_cli/kanban_glue.py` — `run_dispatch_tick`, `run_notifier_tick`, the OS hooks.
- Create: `tests/hermes_cli/test_kanban_glue.py` — glue unit tests against both backends (fake `spawn_fn`/adapters).
- Modify: `gateway/run.py` — `_kanban_dispatcher_watcher` + `_kanban_notifier_watcher` delegate their tick bodies to the glue (keep loops/quarantine/config in the gateway).
- Not touched: `kanban_db.py` internals (SQLite backend), `kanban_db.dispatch_once` (stays for the CLI daemon).

---

## PART A — Close phase-2-tail (PostgresKanbanStore parity)

> Each Part-A task: (1) add the Protocol method(s) to `store.py`; (2) add the SqliteKanbanStore delegation (`_write`/`_read` → `kanban_db.<fn>`) + any `OP_ALLOWLIST` entry; (3) reimplement in PostgresKanbanStore mirroring the named `kanban_db` function's exact behavior/SQL (read it in `kanban_db.py`); (4) extend `test_store_conformance.py` with a test that runs vs BOTH backends; (5) commit. The conformance suite is the parity gate — a new test failing on sqlite means the test or a SqliteKanbanStore delegation is wrong; failing on postgres means fix the PG impl. Never weaken a test.

### Task A1: heartbeat_worker + notifier-heartbeat sidecar + profile-wake-events read
**Methods:** `heartbeat_worker` (`kanban_db.py:6477`), `record_notifier_heartbeat` (`kanban_db.py:9891`, sidecar), `list_notifier_heartbeats` (`kanban_db.py:9932`, sidecar), `list_profile_wake_events` (`kanban_db.py:9830`).
- Protocol: declare all four (some already declared as phase-2-tail stubs — implement them).
- SqliteKanbanStore: delegate (`heartbeat_worker` via `_write`; the sidecar + wake-events reads via `_read`/direct since sidecar ignores conn). Add `"heartbeat_worker"` to OP_ALLOWLIST if absent.
- PostgresKanbanStore: `heartbeat_worker` = real board-table write (UPDATE tasks.last_heartbeat_at WHERE status='running' [AND current_run_id]; UPDATE task_runs.last_heartbeat_at; throttled "heartbeat" event). `record_notifier_heartbeat`/`list_notifier_heartbeats` = delegate to the SAME SQLite sidecar (`hermes_cli.kanban_notifier_sidecar` or whatever `kanban_db` uses — import + call it; do NOT write a Postgres table). `list_profile_wake_events` = board-scoped SELECT (table exists in pg_schema).
- Conformance: `test_heartbeat_worker` (create→claim→heartbeat returns True; non-running→False), `test_notifier_heartbeat_roundtrip` (record then list shows it), `test_list_profile_wake_events_empty` (returns list).

### Task A2: profile-wake recording + cursor mutation ops
**Methods:** `record_profile_wake_success` (`kanban_db.py:9714`), `record_profile_wake_failure` (`kanban_db.py:9752`), `rewind_profile_event_cursor` (`kanban_db.py:9652`), `advance_profile_event_cursor`, `advance_notify_cursor`, `rewind_notify_cursor` (the cursor-mutation halves the notifier uses post-delivery — find their `kanban_db` names; some may be folded into `claim_*`/`remove_notify_sub`).
- Implement in both backends (PG: mirror the CAS UPDATE + `kanban_profile_event_claims` DELETE + wake-event INSERT semantics from the map). 
- Conformance: `test_profile_wake_success_advances_and_clears` (add profile sub, claim events, record_success advances cursor + INSERTs a 'success' wake event), `test_profile_wake_failure_rewinds` (record_failure CAS-rewinds cursor + bumps wake_failure_count + INSERTs 'failed' wake event).

### Task A3: complete_task full parity (gates + closeout + prose-scan + cleanup hook)
**Method:** `complete_task` (`kanban_db.py:4742`) — read it fully. Implement in PostgresKanbanStore the deferred parts: terminal-run idempotency gate (`_terminal_run_already_closed`), PR-head gate (`_enforce_review_pr_head_gate` → raises/emits `completion_blocked_pr_head`), external-handoff gate, hallucinated-cards gate (`_verify_created_cards` → `HallucinatedCardsError` + `completion_blocked_hallucination` event), closeout packet (`_metadata_with_closeout_packet`), prose phantom scan (`suspected_hallucinated_references`), failure-counter clear, recompute. Remove the `created_cards` NotImplementedError.
- **OS-cleanup hook:** `complete_task` takes an optional `on_cleanup: Callable[[str], None] | None = None` param; if provided, call it AFTER commit (the glue passes a function that does `_cleanup_workspace`-equivalent: rmtree + tmux kill). The store NEVER does filesystem cleanup itself. Apply the same hook param to SqliteKanbanStore (it can pass through to a wrapper; kanban_db.complete_task already does cleanup internally — so for SQLite, document that cleanup stays in kanban_db and the hook is a no-op/ignored on sqlite, OR thread it — decide during impl to preserve zero behavior change on sqlite. SAFEST: leave SqliteKanbanStore.complete_task delegating to kanban_db as today (cleanup included); add the `on_cleanup` param to the Protocol as optional, ignored by sqlite. Postgres uses the hook since it has no internal cleanup.).
- Reuse the typed exceptions `kanban_db.HallucinatedCardsError` / `PRHeadGateError` (import them) so callers' `except` clauses match across backends.
- Conformance: `test_complete_hallucinated_cards_rejected` (created_cards with a phantom id → raises HallucinatedCardsError + task stays in-flight), `test_complete_with_verified_cards` (real created-by card → success), `test_complete_closeout_packet_present` (completed event payload has the closeout structure). Run vs both backends.
- **Risk:** the PR-head/external-handoff gates read parent/run metadata — confirm they're DB/metadata-only (no live `gh`/git calls); if a gate genuinely needs external state, factor that into a caller-provided predicate rather than the store. Flag during impl.

### Task A4: record_task_failure (breaker/gave_up)
**Method:** `_record_task_failure` (`kanban_db.py:7024`). Expose as a public store method `record_task_failure(task_id, error, *, outcome, failure_limit=None, failure_limit_is_cap=False, release_claim=True, end_run=True, event_payload_extra=None) -> bool` (True = breaker tripped → blocked/gave_up; False = retry → ready). PG: mirror the counter logic (effective_limit from max_retries/arg/DEFAULT_FAILURE_LIMIT=2), the blocked-vs-ready UPDATE, `_end_run`(outcome=gave_up|outcome), and the `gave_up`/outcome event. SQLite: delegate.
- Conformance: `test_record_failure_breaker_trips` (failure_limit=1 → first failure → blocked + gave_up event; returns True), `test_record_failure_retry` (failure_limit=3 → returns False, status ready).

### Task A5: dispatch DB-ops + dispatch_plan + spawn-result recording
**Methods:** `release_stale_claims`, `detect_crashed_workers`, `detect_stale_running`, `promote_cleared_scheduled`, the DB-side of `enforce_max_runtime` (the UPDATE+event, NOT the signalling), and a new `dispatch_plan(...)` + `record_spawn_success(task_id, pid)` + `record_spawn_failure(task_id, error, *, failure_limit)`.
- `dispatch_plan` does (mirroring `dispatch_once` steps 2–11 MINUS the spawn): reclaim/crashed/stale/max-runtime-DB/promote-cleared/recompute → ready-scan with caps (max_spawn/max_in_progress/per-profile) → per-task default-assignee + profile_exists + pre-spawn-validation + respawn-guard (blocker_auth→blocked, active_pr→scheduled) → `claim_task` → `set_workspace_path`(resolved by a glue-provided `resolve_workspace` callback, OR return workspace_kind and let glue resolve) → returns a `DispatchPlan` dataclass: `spawned: list[ClaimedTask]`, plus counts/diagnostics matching `DispatchResult` fields the gateway logs. **Workspace resolution** (`resolve_workspace`) touches the filesystem → pass it in as a glue callback; the store calls it then persists the path via `set_workspace_path`. `profile_exists`/validation that read disk → also glue-injected predicates or accept the Phase-2 simplification (document).
- SQLite: `dispatch_plan` delegates to the existing `kanban_db` primitives (call `release_stale_claims`/`detect_crashed_workers`/`recompute_ready`/`claim_task`/etc. directly — NO new logic). Postgres: reimplement each primitive (most are simple UPDATEs; mirror the SQL).
- Conformance: `test_dispatch_plan_claims_ready` (create ready task with a real-ish assignee → dispatch_plan returns it claimed/running; second plan doesn't re-return it), `test_record_spawn_failure_breaker` (record_spawn_failure with failure_limit=1 → task blocked). Keep these backend-parametrized. NOTE: `profile_exists`/workspace are glue concerns — in the conformance test, inject trivial callbacks (always-valid profile, workspace=tmp) so the test exercises the DB path.
- **This is the largest Task A item.** If `dispatch_plan` proves too big for one task, split: A5a (the reclaim/crashed/stale/max-runtime-DB/promote primitives + conformance) and A5b (`dispatch_plan` composition + spawn-result + conformance).

### Task A6: Part-A acceptance — full conformance vs both backends
Run `pytest tests/hermes_cli/kanban/ -q` → all green on sqlite + postgres. Run the existing sqlite suites (`test_kanban_db.py`, `test_kanban_tools_write_session.py`, `test_kanban_notify.py`, `tests/plugins/`, gateway notifier tests) → green (the new SqliteKanbanStore delegations + OP_ALLOWLIST additions must not regress). Confirm no remaining `phase-2-tail` NotImplementedError that the glue (Part B) will call. Commit a Part-A marker.

---

## PART B — Extract the backend-agnostic glue

### Task B1: `kanban_glue.run_dispatch_tick`
Create `hermes_cli/kanban_glue.py`. Implement:
```python
def run_dispatch_tick(store, *, spawn_fn, resolve_workspace, profile_exists,
                      max_spawn, max_in_progress, failure_limit,
                      stale_timeout_seconds, default_assignee,
                      max_in_progress_per_profile, ttl_seconds=None,
                      enforce_runtime_kill=None) -> dict:
    """One backend-agnostic dispatch tick. Calls store.dispatch_plan(...) with the
    glue-provided fs/profile callbacks, spawns each planned task via spawn_fn,
    records spawn success/failure back into the store. Returns a summary dict
    (spawned ids, counts) matching what the gateway logs today. NO asyncio, NO
    SQLite-specific logic, NO board enumeration (caller passes one store/board)."""
```
- Port the per-board tick body from `gateway/run.py:_tick_once_for_board` MINUS the SQLite fingerprint/quarantine/daemon-routing (those stay in the gateway) and MINUS direct kb calls (use `store`). `enforce_max_runtime` SIGTERM/SIGKILL → `enforce_runtime_kill` callback.
- Test (`tests/hermes_cli/test_kanban_glue.py`, both backends via the conformance `store` fixture or a local one): a ready task with a fake `spawn_fn` (records pid) → tick spawns it, task goes running with worker_pid; a `spawn_fn` that raises → `record_spawn_failure` path → task blocked/ready per breaker.

### Task B2: `kanban_glue.run_notifier_tick`
Implement:
```python
def run_notifier_tick(store, adapters, *, notifier_profile, render_chat_event,
                      wake_profile_fn, build_wake_prompt, ...) -> dict:
    """One backend-agnostic notifier tick for ONE board's store. Reads subs,
    claims unseen events via the store, sends chat deliveries via adapters,
    advances/rewinds cursors via the store, orchestrates profile wakes via
    wake_profile_fn, records wake success/failure via the store. NO asyncio,
    NO board enumeration, NO SQLite specifics."""
```
- Port the notifier `_collect` + delivery body (`gateway/run.py:5942`–`6400`+) MINUS board enumeration/heartbeat-sidecar-loop/daemon-routing (gateway keeps those) and MINUS direct kb calls. Adapter `.send` and the wake `subprocess.Popen` are injected (`adapters`, `wake_profile_fn`). Message formatting → `render_chat_event` callback (or inline a port of the per-kind formatter).
- Test: a sub + a completed event → tick calls a fake adapter's send + advances the cursor; a send failure → rewind/unsub per `MAX_SEND_FAILURES`; a profile sub + event → `wake_profile_fn` invoked + `record_profile_wake_success` advances cursor. Both backends.

### Task B3: rewire `gateway/run.py` dispatcher to call the glue
In `_kanban_dispatcher_watcher` / `_tick_once_for_board`, replace the body that calls `_kb.dispatch_once(conn, ...)` (or `daemon.execute("dispatch_once", ...)`) with: build a `store = kanban_store(board=slug)` (or route via daemon as today for the single-writer SQLite path), then call `kanban_glue.run_dispatch_tick(store, spawn_fn=_default_spawn, resolve_workspace=..., profile_exists=..., <config>, enforce_runtime_kill=...)`. KEEP: the asyncio loop, sleep slices, board enumeration, fingerprint/quarantine maps, health telemetry, auto-decompose, zombie reap. 
- **Single-writer SQLite nuance:** today the daemon executes `dispatch_once` server-side. With the glue, dispatch orchestration moves client-side but writes still route through the store (which routes through the daemon per-op). Confirm this preserves the single-writer invariant (each store write op still goes through `write_session`/daemon) — the spawn happens in the gateway process (as today). Validate against `tests/hermes_cli/test_kanban_notify.py` dispatcher tests + `tests/gateway/`.
- Gate: `pytest tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_per_profile_cap.py tests/hermes_cli/test_kanban_default_assignee.py tests/hermes_cli/test_kanban_dispatch_promotes_scheduled.py tests/hermes_cli/test_kanban_no_review_dispatch.py -q` stays green (these cover dispatch behavior). The ~40 `test_dispatch_*` in test_kanban_db.py test `kanban_db.dispatch_once` directly (UNTOUCHED) so they stay green regardless.

### Task B4: rewire `gateway/run.py` notifier to call the glue
In `_kanban_notifier_watcher`, replace the `_collect` body + delivery with: per board, `store = kanban_store(board=slug)`, `kanban_glue.run_notifier_tick(store, self.adapters, notifier_profile=..., wake_profile_fn=self._kanban_profile_wake, build_wake_prompt=..., render_chat_event=...)`. KEEP: board enumeration, the heartbeat-sidecar record loop (or move into the tick via `store.record_notifier_heartbeat`), daemon routing, corruption quarantine maps, asyncio.
- Gate: `pytest tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_single_writer.py tests/hermes_cli/test_kanban_notify.py -q` stays green. These instantiate `GatewayRunner` and drive `_kanban_notifier_watcher`; the watcher still exists (it now delegates its tick to the glue), so the tests exercise the glue end-to-end. If a test is too tightly coupled to the old internal structure, adapt it minimally (don't weaken assertions on `adapter.sent` / cursor state).

### Task B5: Phase-3 acceptance gate
- `pytest tests/hermes_cli/kanban/ -q` → green both backends.
- Full kanban + gateway + plugin regression (the Phase-2 acceptance list + all dispatch/notifier tests) → green on sqlite; only the known pre-existing failures (3 in test_kanban_core_functionality.py) remain.
- Confirm default backend still sqlite; the gateway dispatch/notify path works identically (manually inspect: a dry-run dispatch tick + a notifier tick against a tmp sqlite board).
- Empty Phase-3 marker commit.

---

## Risks & notes
- **Highest-risk file:** `gateway/run.py` (21k lines). Tasks B3/B4 must keep the asyncio loops + quarantine maps intact and only swap the tick BODY. Use full spec + code-quality + (adversarial) review on B1–B4. The live gateway/dashboard import from `main`; default backend stays sqlite so behavior is unchanged until cutover.
- **`dispatch_plan` workspace/profile callbacks:** filesystem + profile-on-disk are glue concerns injected as callbacks so the store stays pure-data and backend-agnostic. Conformance tests inject trivial callbacks.
- **complete_task gates reading external state:** verify the PR-head/external-handoff gates are DB/metadata-only; if any needs live git/gh, inject it as a caller predicate (don't put I/O in the store).
- **Single-writer invariant:** moving dispatch orchestration client-side must keep each write routing through the daemon (per-op `write_session`); the spawn stays in the gateway process. Validate with the single-writer dispatcher tests.
- **Deferral discipline:** anything still not at parity must remain a loud `NotImplementedError("phase-3-tail: …")`, tracked for pre-cutover.

## Then: Phase 4 (migration tooling) + Phase 5 (cutover) — separate plans
Phase 4 = `migrate_sqlite_to_pg.py` (export/transform/load in FK order, preserve ids/epochs, set IDENTITY seq to max+1, dry-run into a throwaway schema, row-count + integrity verification) + the cutover runbook. Phase 5 = the live maintenance-window cutover, executed **with the human** (needs a provisioned Supabase DSN + go-ahead; not autonomous).
