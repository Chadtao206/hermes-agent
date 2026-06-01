# Phase 6 · B2 — PG crash-lane parity (M1/M2/M3 + claim_lock) (design)

**Status:** approved (design phase). Branch `feat/kanban-pg-phase6-b2` (worktree `.worktrees/kanban-pg-phase6-b2`), off `main` `3cefc4798`.
**Predecessor:** [[kanban-pg-phase45-tail-closeout]] flagged the 3 residual PG-crash-lane divergences (M1/M2/M3) as "Phase-2-era SIMPLIFIED PG crash detection" leftovers; [[kanban-pg-dashboard-readpath]] flagged the `claim_task(claimer=None)→claim_lock=NULL` gap. B2 closes all four.

## Problem

`PostgresKanbanStore._pg_detect_crashed_workers` and `claim_task` diverge from the sqlite reference (`kanban_db.detect_crashed_workers` / `claim_task`) in four ways. All are PG-only, single-host, rare, but they break crash-storm capping and event/state parity:

- **M1 — no systemic-crash fingerprint cap-block.** sqlite computes `_fp_counts` over all crash error-texts in a tick and, for a genuine (non-protocol-violation) crash whose fingerprint count `>= SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD (3)`, caps the breaker immediately (`is_systemic`, `failure_limit=1`, `failure_limit_is_cap=True`). PG caps only on `protocol_violation`; a crash storm of identical genuine failures retries via the normal counter instead of capping.
- **M2 — genuine-crash error text + payload divergence.** sqlite: `nonzero_exit`→`"pid N exited with code C"`, `signaled`→`"pid N killed by signal C"`, `unknown`→`"pid N not alive"`, with `event_payload` carrying `exit_kind`+`exit_code` (when `code is not None and kind != "unknown"`). PG collapses all to `error_text=f"pid {pid} not alive ({kind})"` and `payload={"pid","claimer","exit_kind"}` (no `exit_code`). This also starves M1's fingerprinting of the sqlite-shaped error text.
- **M3 — no `skip_unknown` deferral.** sqlite `detect_crashed_workers(skip_unknown=...)`; `dispatch_once` passes `skip_unknown=True` (verified `kanban_db.py:7758`), so a dead pid that classifies `unknown` is LEFT for the TTL/heartbeat stale lane rather than reclaimed immediately. PG has no `skip_unknown` and reclaims `unknown` dead pids right away (the conservative-deferral behavior is lost).
- **claim_lock=NULL.** sqlite `claim_task` resolves `lock = claimer or _claimer_id()` (`kanban_db.py:4213`) → `host:pid`. PG `claim_task(claimer=None)` stores `claim_lock=NULL` and emits a `claimed` event with `lock=None`; downstream `crashed`-event `claimer` is `""`.

## Goal

Close all four in `PostgresKanbanStore` so PG crash detection + claim behavior matches sqlite, pinned by cross-backend conformance tests. PG-only; sqlite byte-identical; `kanban_db.py`/`kanban_liveness.py`/`kanban_writer_daemon.py` import-only. Single-host assumed.

## Scope decision (settled)

Full crash-lane parity = **M1 + M2 + M3 + claim_lock=host:pid** in one cycle. The sqlite **host-local crash filter** (`claim_lock.startswith(host_prefix)`) is **deferred** (user decision): it's a no-op on the single-host live deployment and the conformance suite is single-host, so it would be untested/dead. Documented as a deferred multi-host parity item. The separate `claim_task` TTL-resolution divergence (PG `int(ttl) if ttl else 900` vs sqlite `_resolve_claim_ttl_seconds`) is also out of scope (pre-existing phase-2-tail item, not a crash-lane gap).

## Changes (all in `hermes_cli/kanban/store_postgres.py`)

### 1. claim_lock = host:pid (`claim_task`, ~line 1605)
At the top of `claim_task`, before the INSERT/UPDATE: `claimer = claimer or kanban_db._claimer_id()` (mirrors sqlite `kanban_db.py:4213`). The existing INSERT `task_runs.claim_lock`, `UPDATE tasks SET claim_lock`, and the `_emit("claimed", {"lock": claimer, …})` then all use the resolved `host:pid`. No other change. (TTL stays as-is — out of scope.)

### 2. M2 — genuine-crash error text + payload parity (`_pg_detect_crashed_workers`, ~line 2127)
In the non-protocol-violation branch, replace:
```python
error_text = f"pid {pid} not alive ({kind})"
event_kind = "crashed"
event_payload = {"pid": pid, "claimer": lock, "exit_kind": kind}
```
with the sqlite shapes (`kanban_db.py:6918-6929`):
```python
protocol_violation = False
if kind == "nonzero_exit":
    error_text = f"pid {pid} exited with code {code}"
elif kind == "signaled":
    error_text = f"pid {pid} killed by signal {code}"
else:
    error_text = f"pid {pid} not alive"
event_kind = "crashed"
event_payload = {"pid": pid, "claimer": lock}
if code is not None and kind != "unknown":
    event_payload["exit_kind"] = kind
    event_payload["exit_code"] = code
```
(The protocol-violation/clean_exit branch is unchanged.)

### 3. M1 — systemic-crash fingerprint cap-block (`_pg_detect_crashed_workers`)
Restructure from per-row inline `record_task_failure` to two passes (mirror sqlite `kanban_db.py:6935-7021`):
- **Pass 1 (reclaim):** for each dead-pid running task, do the existing per-task reclaim (`UPDATE … status='ready' …` + `_pg_end_run` + `_emit(event_kind, event_payload)`, each in its own `conn.transaction()`). Collect `crash_details.append((tid, pid, lock, protocol_violation, error_text))` for each row whose reclaim UPDATE `rowcount==1`. Do NOT call `record_task_failure` in this pass.
- **Pass 2 (failure accounting, after the reclaim loop):**
  ```python
  if crash_details:
      _fp_counts = {}
      for _, _, _, _, err in crash_details:
          fp = kanban_db._error_fingerprint(err)
          _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
      for tid, pid, lock, protocol_violation, error_text in crash_details:
          fp = kanban_db._error_fingerprint(error_text)
          is_systemic = (not protocol_violation
                         and _fp_counts.get(fp, 0) >= kanban_db.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD)
          extra = {"pid": pid, "claimer": lock}
          if protocol_violation:
              extra["failure_class"] = kanban_db.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT
              extra["guidance"] = kanban_db._PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE
          self.record_task_failure(
              tid, error_text, outcome="crashed",
              failure_limit=1 if (protocol_violation or is_systemic) else None,
              failure_limit_is_cap=bool(protocol_violation or is_systemic),
              release_claim=False, end_run=False, event_payload_extra=extra)
  ```
  This matches sqlite's `extra`/`failure_limit`/`is_cap` exactly (`kanban_db.py:7004-7018`). `record_task_failure` already accepts `failure_limit_is_cap` (Phase 4.5). Each reclaim stays in its own txn (Pass 1) and each `record_task_failure` opens its own txn (Pass 2) — same transaction granularity as today; only the fingerprint-aware cap decision is new.

### 4. M3 — skip_unknown (`_pg_detect_crashed_workers` + `dispatch_plan`)
- Add `skip_unknown: bool = False` to `_pg_detect_crashed_workers`'s signature. After classifying, `if kind == "unknown" and skip_unknown: continue` (skip before reclaim — leave for the stale/TTL lane), mirroring sqlite `kanban_db.py:6882-6887`.
- In `dispatch_plan` (~line 2337), pass `skip_unknown=True` to the `_pg_detect_crashed_workers(...)` call, mirroring sqlite `dispatch_once` (`kanban_db.py:7758`).

## Reuses (import-only from `kanban_db`; add the missing ones to the existing import block)
`_error_fingerprint`, `SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD`, `_claimer_id`, `FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT`, `_PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE`. (store_postgres already imports several kanban_db helpers; extend that block — do not redefine.)

## Constraints / guarantees
- `kanban_db.py`, `kanban_liveness.py`, `kanban_writer_daemon.py`: import-only, zero edits.
- sqlite path byte-identical (every change is PG-only, inside `PostgresKanbanStore`).
- Default backend stays sqlite in code + tests.
- No DSN/secret in logs (this code logs nothing new; `_pg_detect_crashed_workers` stays read-then-write).
- Single-host: the sqlite host-local `claim_lock.startswith(host_prefix)` crash filter is NOT added (deferred multi-host item). The claim_lock fix populates `host:pid` for parity of the stored lock + event `claimer`/`lock` fields, not to drive host filtering.

## Testing (cross-backend conformance; docker-PG `store` fixture; extend `tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py` or a new `test_store_crash_lane_parity.py`)

Crash detection is exercised PG-side by injecting `pid_alive_fn`/`classify_exit_fn` into `_pg_detect_crashed_workers` (the same callbacks `dispatch_plan` injects). For true cross-backend parity, the sqlite side monkeypatches `kanban_db._classify_worker_exit` to return the same `(kind, code)` and seeds `worker_pid` + `claim_lock=host:pid` + `status='running'`, then runs `kb.detect_crashed_workers(conn)`.

- **claim_lock**: `store.claim_task(task_id, claimer=None)` → the running row's `claim_lock` starts with `f"{socket.gethostname()}:"` (or `_claimer_id().split(':')[0]`) on both backends; the `claimed` event payload `lock` == `_claimer_id()`-shaped. (Today PG would store NULL → test fails pre-fix.)
- **M2**: dead pid + `classify_exit_fn→("nonzero_exit",7)` → `crashed` event payload has `exit_kind="nonzero_exit"`, `exit_code=7`; the ended run's `error` == `"pid {pid} exited with code 7"`. Same for `("signaled",9)` → `"pid {pid} killed by signal 9"`. `("unknown",None)` → `"pid {pid} not alive"`, no `exit_code` in payload. Cross-check the strings/payload against sqlite with the monkeypatched classifier.
- **M1**: seed 3 running tasks, all dead, `classify_exit_fn→("nonzero_exit",7)` (same fingerprint) → after detect, all 3 are `blocked` with a `gave_up` event (systemic cap) on **both** backends. Seed only 2 → neither systemic-caps (status returns to `ready`/retry, `consecutive_failures` incremented, not capped). A mixed tick (2 of fingerprint A + 1 of B) → none cap (each below threshold).
- **M3**: dead pid + `classify_exit_fn→("unknown",None)`: with `skip_unknown=True` → NOT reclaimed (task stays `running`); with `skip_unknown=False` (default) → reclaimed to `ready`. Assert `dispatch_plan` calls detect with `skip_unknown=True` (a dead-unknown running task survives a `dispatch_plan` tick's crash phase). Protocol-violation (`clean_exit`) still caps immediately (regression).
- sqlite crash-lane suite (`test_kanban_db.py` crash/dispatch tests) stays green.

Test interpreter: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`; docker `postgres:16-alpine` via `HERMES_PG_TEST_DSN`; never the live Supabase DB.

## Review

Adversarial (live-core `store_postgres.py` — the dispatcher crash lane). Focus: the two-pass restructure preserves per-reclaim atomicity + can't double-reclaim or double-count a task; `is_systemic` cap decision matches sqlite exactly (threshold, `not protocol_violation`); `skip_unknown` skips before any write; M2 strings/payload byte-match sqlite; claim_lock resolution doesn't change the claim CAS or break the `claimed` event consumers; no sqlite/forbidden-file drift.

## File inventory

- Edit: `hermes_cli/kanban/store_postgres.py` (`claim_task`, `_pg_detect_crashed_workers`, the `dispatch_plan` crash-detect call site, + the kanban_db import block).
- Test: extend `tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py` or add `tests/hermes_cli/kanban/test_store_crash_lane_parity.py`.

## Out of scope (deferred)
- Host-local crash filter (multi-host parity; single-host no-op).
- `claim_task` TTL-resolution parity (pre-existing phase-2-tail).
- B4 (Auth/RLS/Realtime + live dashboard), B5 (frozen kanban.db fate) — separate.
