# Phase 4.5 — `phase-3-tail` close-out — design

- **Date:** 2026-05-31
- **Status:** Approved design; implementation plan to follow.
- **Sequenced as:** Phase 4.5 (closes the `phase-3-tail` deferrals so the Phase-5
  live cutover's BLOCKING preconditions are met). Builds on Phases 1–4 (all merged
  to `main`; Phase 4 migrator at `e09035376`).
- **Parent designs:** `plans/kanban-postgres-migration/design.md`,
  `phase-3-glue-extraction.md`, `phase-4-design.md`.

## Goal

Close all six `phase-3-tail` items so the **Postgres** backend's dispatch /
kill / crash-reap behavior reaches parity with the canonical SQLite backend —
these are the cutover preconditions the Phase-4 runbook gates on.

**Hard boundaries**
- Default backend stays `sqlite`; the **live sqlite dispatch path is byte-identical**
  (`dispatch_once` runs its own inline ladder/reap and *ignores* the injected OS
  callbacks, so extending them affects only the PG path).
- `hermes_cli/kanban_db.py` is **upstream**: read/imported, **never edited**. We
  reuse its generic OS helpers rather than reinventing them.
- **Single-host PG** assumed (the live deployment + foreseeable cutover is one
  gateway host); host-local filtering is a defensive guard, not a multi-host
  arbiter. Non-single-writer sqlite is **not** a supported prod config, so no glue
  machinery is built to drive the sqlite path's kills.
- No live cutover (that is human-driven Phase 5).

## Root cause (one fact behind almost every gap)

Postgres cannot perform OS operations server-side — reaping / exit-codes,
pid-liveness, signals, host-local filtering. SQLite's `dispatch_once` runs *inside*
the single host process and owns all of this inline. The PG store (Phase 2/3)
delegates OS ops to injected callbacks but left the host-side **choreography**
unbuilt. Phase 4.5 builds that choreography by **wiring existing `kanban_db` OS
helpers into the PG path** and adding the PG store's DB-transition SQL.

## Architecture — store / glue / gateway seam

- **Store** (`hermes_cli/kanban/store_postgres.py`) — DB-transition orchestrator.
  Each reclaim method: find candidates → invoke an injected OS callback → apply the
  DB flip + emit events (mirroring SQLite's kill-then-flip ordering). Owns the
  pure-DB blocks (pre-spawn, systemic-sibling) outright.
- **Glue** (`hermes_cli/kanban_glue.py`) — thin forwarder of the OS callbacks +
  calls the new systemic-sibling store method in its systemic branch. Stays
  backend-agnostic.
- **Gateway** (`gateway/run.py`) — OS owner. Constructs the callbacks from existing
  `kanban_db` helpers and reaps each tick. (This is where Phase-3 B3 already wires
  `signal_fn`/`pid_alive_fn`; Phase 4.5 upgrades that wiring.)

### Callback-contract changes (Protocol + both backends; sqlite accepts-and-ignores)

- Replace `signal_fn(pid, sig)` with **`terminate_fn(pid, claim_lock) -> dict`** —
  runs the full host-guarded `SIGTERM → 5s grace → SIGKILL` ladder. Gateway wires it
  to `kanban_db._terminate_reclaimed_worker(pid, claim_lock, signal_fn=os.kill)`
  (which already does the host-prefix guard + ladder + zombie-aware liveness).
- Add **`classify_exit_fn(pid) -> tuple[str, Optional[int]]`** to crash detection.
  Gateway wires `kanban_db._classify_worker_exit`; gateway calls
  `kanban_db.reap_worker_zombies()` at the top of each dispatch tick to populate the
  `_recent_worker_exits` registry before classification. `pid_alive_fn` stays
  (wired to `kanban_db._pid_alive`).
- These pass `store.dispatch_plan(...) → reclaim methods`. SQLite's `dispatch_plan`
  accepts the new kwargs and ignores them (it delegates to `dispatch_once`, whose
  inline ladder/reap is untouched).

Because `terminate_fn` runs a blocking grace poll (≤5s per worker), the dispatch
tick blocks during termination — this exactly mirrors the SQLite reference
(`enforce_max_runtime` blocks the same way), so it is accepted behavior.

## The six items

### 1. Kill-ladder (SIGTERM → grace → SIGKILL)  [critical]
`_pg_enforce_max_runtime`, `_pg_detect_stale_running`, `_pg_release_stale_claims`
call `terminate_fn(pid, claim_lock)` (full ladder) instead of one best-effort
SIGTERM. The store still does the DB flip (`→ ready`, close run with the existing
outcomes `timed_out`/`stale`/`reclaimed`) after the ladder returns.

### 2. Crash rc=0 → protocol-violation  [important]
`_pg_detect_crashed_workers(pid_alive_fn, classify_exit_fn)`: for a running task
whose `worker_pid` is **not** alive, call `classify_exit_fn(pid)`:
- `clean_exit` (rc=0) → emit a `protocol_violation` event (payload incl.
  `failure_class = FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT` + guidance) and
  cap-block via `record_task_failure(..., outcome="crashed", failure_limit=1,
  failure_limit_is_cap=True, release_claim=False, end_run=False)`.
- `nonzero_exit` / `signaled` / `unknown` → emit `crashed`, record failure
  (counter-only), flip `→ ready` for retry.
When `classify_exit_fn` is None, fall back to the current liveness-only crashed path.

### 3. Pre-spawn validation auto-block  [parity — pure DB]
In `dispatch_plan`, when `_pre_spawn_validation_errors(task)` is non-empty, call a
new `_pg_record_pre_spawn_validation_failure(tid, errors)` mirroring the SQLite
`_record_pre_spawn_validation_failure`: flip `ready → blocked` (guarded on
`status='ready' AND claim_lock IS NULL`), bump `consecutive_failures`, set
`last_failure_error`, synth an ended run (`outcome='spawn_failed'`), emit
`pre_spawn_validation_failed` → `gave_up` → `blocked`. Append to
`result.auto_blocked`. (Today PG just defers the task with no block/event.)

### 4. Systemic-spawn-failure sibling pre-emptive block  [optimization]
New store method `block_systemic_spawn_failure_signature(task_ids, *,
failure_signature, error, signature_count) -> list[str]` (Protocol + both backends):
block each `ready`/unclaimed sibling **without** re-incrementing its counter, emit
`systemic_failure_signature` → `gave_up` → `blocked`. PG implements the SQL; SQLite
delegates to the existing `kanban_db._block_systemic_spawn_failure_signature`. The
glue's `_record_dispatch_spawn_failure` already computes the systemic grouping
(threshold `SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD = 3`); its systemic branch
now calls the store method and merges the result into `auto_blocked`.

### 5. Live-pid claim-extension  [anti-duplicate-spawn]
`_pg_release_stale_claims(pid_alive_fn=...)`: for a TTL-expired claim whose
`worker_pid` is **still alive** (host-local), **extend** the claim (new
`claim_expires`, emit `claim_extended`) instead of reclaiming — preventing a
duplicate spawn of a slow-but-alive worker. Dead-pid / non-local claims reclaim as
today. Mirrors SQLite `release_stale_claims`.

### 6. Dead gateway helper cleanup  [hygiene]
Remove the now-unused gateway helpers `_kanban_advance`, `_kanban_unsub`,
`_kanban_rewind`, `_kanban_profile_advance`, `_kanban_profile_rewind`,
`_kanban_profile_record_success`, `_kanban_profile_record_failure` from
`gateway/run.py` (keep `_kanban_profile_wake` — still the live wake hook). Update
the single stale reference in `tests/.../test_kanban_notifier_single_writer.py`
(uses `_kanban_advance`) to the store/glue path or drop it.

## Task structure (Phase-3-style A/B)

**Part A — store + glue + Protocol (conformance-gated, low-risk).** Driven by
injected fakes; no `gateway/run.py` touch:
- A1: `_pg_record_pre_spawn_validation_failure` + wire into `dispatch_plan`.
- A2: `block_systemic_spawn_failure_signature` (Protocol + PG SQL + sqlite delegate)
  + glue systemic-branch call.
- A3: `terminate_fn(pid, claim_lock)` contract across the three reclaim methods
  (replace `signal_fn`) + Protocol/`dispatch_plan` signature + glue forwarding.
- A4: `classify_exit_fn` crash path in `_pg_detect_crashed_workers` (protocol-violation
  cap-block vs crashed) + Protocol/`dispatch_plan` + glue forwarding.
- A5: live-pid claim-extension in `_pg_release_stale_claims`.

**Part B — gateway wiring (high-risk, sequenced last, gateway-test-covered).**
- B1: in the gateway dispatcher tick, call `kanban_db.reap_worker_zombies()` and
  construct `terminate_fn`/`classify_exit_fn`/`pid_alive_fn` from the `kanban_db`
  helpers; pass through `run_dispatch_tick → dispatch_plan`. (PG path only;
  sqlite path unaffected.)
- B2: remove the 7 dead helpers + fix the one test reference.
- B3: acceptance (conformance both backends; gateway dispatcher/notifier/single-writer
  suites green; sqlite dispatch byte-identical; default backend unchanged).

## Testing

- **Conformance suite** (`tests/hermes_cli/kanban/`, both backends) drives Part A via
  **injected fakes**: `terminate_fn` records `(pid, claim_lock)` calls;
  `classify_exit_fn` returns a scripted `(kind, code)`; `pid_alive_fn` returns
  scripted liveness. Assert the DB transitions + event sequences: protocol-violation
  cap-block, pre-spawn `blocked`+events, systemic-sibling block (siblings blocked, no
  counter re-bump), claim-extension (extend vs reclaim), kill-ladder invocation.
- The **real** OS helpers (`_terminate_reclaimed_worker`, `reap_worker_zombies`,
  `_classify_worker_exit`, `_pid_alive`) are already covered by `kanban_db` tests, so
  Part B is covered by **gateway tests** (the wiring) + existing
  single-writer/glue/notifier suites staying green.
- Existing kanban tests stay green on the default sqlite backend.

## Risks & mitigations

- **`gateway/run.py` touch (live core).** Mitigate: PG-path-only wiring + dead-code
  removal; sqlite dispatch byte-identical (ignores the callbacks); sequence Part B
  last; cover with gateway tests. (Same risk profile as Phase-3 Part B, which landed
  cleanly.)
- **Tick blocks during the kill-ladder grace (≤5s/worker).** Accepted — mirrors the
  SQLite reference exactly.
- **Reaching into `kanban_db` `_`-prefixed helpers.** Accepted: the callers
  (glue/gateway) are fork-owned and the helpers are generic OS utilities, not
  sqlite-specific. (Alternative — hoist into a fork module — was considered and
  declined to avoid duplication and an upstream edit.)
- **Contract change ripples** (`signal_fn → terminate_fn`, new `classify_exit_fn`).
  Mitigate: update the Protocol + both backends in one Part-A task each; sqlite
  accepts-and-ignores; conformance proves no behavioral drift.

## Success criteria

- All six `phase-3-tail` markers removed; the behaviors they described are live on
  the PG backend and proven by the conformance suite (with injected fakes) +
  gateway tests.
- Default backend unchanged; live sqlite dispatch byte-identical; `kanban_db.py`
  unedited.
- The Phase-5 cutover runbook's BLOCKING precondition list is satisfied (update the
  runbook to mark these closed).
