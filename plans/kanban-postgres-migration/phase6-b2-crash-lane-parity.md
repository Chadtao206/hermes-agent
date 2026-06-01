# Phase 6 · B2 — PG crash-lane parity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `PostgresKanbanStore` crash-detection + claim behavior to parity with sqlite for four residual gaps — M1 (systemic-crash fingerprint cap-block), M2 (genuine-crash error-text + exit_code payload), M3 (`skip_unknown` deferral), and `claim_task(claimer=None)→host:pid`.

**Architecture:** PG-only changes inside `hermes_cli/kanban/store_postgres.py`: resolve a default claimer in `claim_task`; rewrite `_pg_detect_crashed_workers` to a two-pass (reclaim → fingerprint-aware failure accounting) mirroring sqlite `detect_crashed_workers`, with sqlite-shaped error text/payload and a `skip_unknown` param; pass `skip_unknown=True` from `dispatch_plan`. Reuse `kanban_db` constants/helpers (import-only). sqlite path byte-identical; single-host (host-local filter deferred).

**Tech Stack:** Python, psycopg 3, pytest with the docker `postgres:16-alpine` `store`/`_pg_dsn` fixtures.

---

## Ground rules (apply to EVERY task)

- **Never edit** `hermes_cli/kanban_db.py`, `hermes_cli/kanban_liveness.py`, `hermes_cli/kanban_writer_daemon.py` — import only.
- **sqlite byte-identical:** every change is inside `PostgresKanbanStore`; the sqlite path is untouched.
- **No DSN/secret in logs:** this code logs nothing new.
- **Test interpreter:** `cd .worktrees/kanban-pg-phase6-b2 && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest`. Export `HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban"` before any pytest. NEVER the live Supabase DB; only pytest (fixtures point at the local container); do NOT run the gateway/dashboard/`hermes kanban` CLI.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Reference (read before implementing)

- `hermes_cli/kanban_db.py::detect_crashed_workers` (lines ~6830-7027) — the sqlite reference for the crash lane. Note: error-text by kind (6918-6924), payload `exit_kind`/`exit_code` gate (6927-6929), `skip_unknown` (6882-6887), two-pass fingerprint `is_systemic` (6988-7019), `extra={"pid","claimer"}`+protocol fields (7004-7009), `failure_limit=1 if (protocol_violation or is_systemic)` / `failure_limit_is_cap` (7014-7015). `_claimer_id()` (2575) = `f"{host}:{pid}"`. `claim_task` default `lock = claimer or _claimer_id()` (4213). `dispatch_once` passes `detect_crashed_workers(conn, skip_unknown=True)` (7758). `_error_fingerprint` (6819). `SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD = 3` (6117).
- `hermes_cli/kanban/store_postgres.py::_pg_detect_crashed_workers` (~2127-2207, current SIMPLIFIED version), `claim_task` (~1605-1634), `record_task_failure` (~1638, already supports `failure_limit_is_cap`), `_pg_end_run`, `dispatch_plan` crash-detect call (~2337). The kanban_db import block is near the top (~lines 14-40).

---

## Task 1: `claim_task(claimer=None)` resolves `host:pid`

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py` (`claim_task`; kanban_db import block)
- Test: `tests/hermes_cli/kanban/test_store_crash_lane_parity.py` (create)

**Review:** adversarial (live-core; touches the claim CAS).

- [ ] **Step 1: Write the failing test** — create `tests/hermes_cli/kanban/test_store_crash_lane_parity.py`:

```python
"""Cross-backend parity for the PG crash lane + claim_lock default."""
import socket
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


def _host_prefix():
    return f"{kb._claimer_id().split(':', 1)[0]}:"


def test_claim_task_default_claimer_is_host_pid(store):
    tid = store.create_task(title="claim me")
    claimed = store.claim_task(tid)            # claimer=None
    assert claimed is not None
    # the running row carries a host:pid claim_lock (not NULL), matching sqlite
    t = store.get_task(tid)
    assert t.claim_lock is not None
    assert t.claim_lock.startswith(_host_prefix())
    # the 'claimed' event records the same lock
    claimed_events = [e for e in store.list_events(tid) if e.kind == "claimed"]
    assert claimed_events
    assert str(claimed_events[-1].payload.get("lock", "")).startswith(_host_prefix())
```

(If `Task` has no `claim_lock` attribute, read it via a board-scoped `SELECT claim_lock` using the store's pool for PG and `kb.connect_closing()` for sqlite — but the `Task` dataclass should expose it; check `kanban_db.Task` fields. The `claimed` event `lock` assertion is backend-agnostic and sufficient if the column read is awkward.)

- [ ] **Step 2: Run — verify the postgres param fails**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_crash_lane_parity.py::test_claim_task_default_claimer_is_host_pid -v`
Expected: sqlite PASS; `postgres` FAIL (`claim_lock is None` / `lock` is `None`).

- [ ] **Step 3: Implement.** Ensure `_claimer_id` is importable, then resolve the default claimer. In the kanban_db import block near the top of `store_postgres.py`, add `_claimer_id` to the `from hermes_cli.kanban_db import (...)` list if absent (alongside `_canonical_assignee` etc.). In `claim_task`, add one line after `ttl = ...`:

```python
    def claim_task(self, task_id, *, ttl_seconds=None, claimer=None):
        now = int(time.time())
        ttl = int(ttl_seconds) if ttl_seconds else 900
        claimer = claimer or _claimer_id()   # sqlite parity: default lock = host:pid
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            ...  # unchanged; INSERT/UPDATE/_emit already use `claimer`
```
(Use the imported `_claimer_id`; or `kanban_db._claimer_id()` if you prefer the qualified form. Do not change the TTL logic.)

- [ ] **Step 4: Run — verify pass both backends + claim suite green**

Run: `... -m pytest tests/hermes_cli/kanban/test_store_crash_lane_parity.py::test_claim_task_default_claimer_is_host_pid -v` → PASS (sqlite + postgres).
Run: `... -m pytest tests/hermes_cli/kanban/test_store_conformance.py -k claim -q` → existing claim conformance PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_crash_lane_parity.py
git commit -m "feat(kanban-pg): claim_task resolves default claimer to host:pid (sqlite parity)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: rewrite `_pg_detect_crashed_workers` (M1 + M2 + M3) + dispatch_plan skip_unknown

**Files:**
- Modify: `hermes_cli/kanban/store_postgres.py` (`_pg_detect_crashed_workers`, the `dispatch_plan` crash-detect call, kanban_db import block)
- Test: `tests/hermes_cli/kanban/test_store_crash_lane_parity.py` (extend)

**Review:** adversarial (live-core; dispatcher crash lane).

- [ ] **Step 1: Write the failing tests** — append to `test_store_crash_lane_parity.py`. A helper seeds a running task with a dead worker_pid and runs crash detection the backend-appropriate way (PG: inject callbacks; sqlite: monkeypatch `kb._pid_alive`/`kb._classify_worker_exit` then call `kb.detect_crashed_workers`):

```python
def _seed_running_with_pid(store, pid, title="run"):
    tid = store.create_task(title=title)
    assert store.claim_task(tid) is not None      # ready -> running (+run)
    store.record_spawn_success(tid, pid)          # sets worker_pid
    return tid


def _detect(store, monkeypatch, *, kind, code, skip_unknown=False):
    """Run crash detection with a forced (kind, code) classification + dead pid."""
    if isinstance(store, PostgresKanbanStore):
        return store._pg_detect_crashed_workers(
            pid_alive_fn=lambda p: False,
            classify_exit_fn=lambda p: (kind, code),
            skip_unknown=skip_unknown)
    monkeypatch.setattr(kb, "_pid_alive", lambda p: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda p: (kind, code))
    with kb.connect_closing() as conn:
        return kb.detect_crashed_workers(conn, skip_unknown=skip_unknown)


def _events(store, tid):
    return [(e.kind, e.payload) for e in store.list_events(tid)]


def test_m2_nonzero_exit_text_and_payload(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483645)
    crashed = _detect(store, monkeypatch, kind="nonzero_exit", code=7)
    assert tid in crashed
    crash_ev = [p for (k, p) in _events(store, tid) if k == "crashed"]
    assert crash_ev and crash_ev[-1].get("exit_kind") == "nonzero_exit"
    assert crash_ev[-1].get("exit_code") == 7
    # task back to ready (single crash, below systemic threshold, not capped)
    assert store.get_task(tid).status == "ready"


def test_m2_signaled_text_and_payload(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483644)
    crashed = _detect(store, monkeypatch, kind="signaled", code=9)
    assert tid in crashed
    crash_ev = [p for (k, p) in _events(store, tid) if k == "crashed"]
    assert crash_ev[-1].get("exit_kind") == "signaled"
    assert crash_ev[-1].get("exit_code") == 9


def test_m2_unknown_no_exit_code(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483643)
    crashed = _detect(store, monkeypatch, kind="unknown", code=None)
    assert tid in crashed                          # reclaimed (skip_unknown=False default)
    crash_ev = [p for (k, p) in _events(store, tid) if k == "crashed"]
    assert "exit_code" not in crash_ev[-1]
    assert "exit_kind" not in crash_ev[-1]


def test_m3_skip_unknown_defers(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483642)
    crashed = _detect(store, monkeypatch, kind="unknown", code=None, skip_unknown=True)
    assert tid not in crashed                       # left for the stale/TTL lane
    assert store.get_task(tid).status == "running"  # untouched


def test_m1_systemic_three_same_fingerprint_cap_block(store, monkeypatch):
    pids = [2147483600, 2147483601, 2147483602]
    tids = [_seed_running_with_pid(store, p, title=f"t{p}") for p in pids]
    crashed = _detect(store, monkeypatch, kind="nonzero_exit", code=7)
    assert set(tids) <= set(crashed)
    # 3 identical-fingerprint genuine crashes in one tick => systemic cap-block
    for tid in tids:
        assert store.get_task(tid).status == "blocked"
        assert any(k == "gave_up" for (k, _p) in _events(store, tid))


def test_m1_two_below_threshold_not_capped(store, monkeypatch):
    pids = [2147483610, 2147483611]
    tids = [_seed_running_with_pid(store, p, title=f"t{p}") for p in pids]
    _detect(store, monkeypatch, kind="nonzero_exit", code=7)
    for tid in tids:                                # below threshold (3) => retry, not capped
        assert store.get_task(tid).status == "ready"
        assert not any(k == "gave_up" for (k, _p) in _events(store, tid))


def test_protocol_violation_caps_immediately(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483630)
    _detect(store, monkeypatch, kind="clean_exit", code=0)
    assert store.get_task(tid).status == "blocked"     # rc=0 protocol violation caps on first
    kinds = [k for (k, _p) in _events(store, tid)]
    assert "protocol_violation" in kinds and "gave_up" in kinds
```

NOTES for the implementer:
- Verify `record_spawn_success(tid, pid)` is the right way to set `worker_pid` on both backends (Protocol: `record_spawn_success(self, task_id, pid)`). If a freshly-claimed task isn't `running` with `worker_pid` set after these two calls, inspect `claim_task`/`record_spawn_success` and adjust the seed (the goal: `status='running'`, `worker_pid=pid`, `claim_lock=host:pid`).
- The sqlite branch of `_detect` monkeypatches module-level `kb._pid_alive` and `kb._classify_worker_exit`; confirm those are the names `detect_crashed_workers` calls (it calls `_pid_alive(row["worker_pid"])` at 6876 and `_classify_worker_exit(pid)` at 6881). The sqlite-store fixture sets `HERMES_KANBAN_DB`, so `kb.connect_closing()` hits the test DB.
- These tests run on BOTH backends via the `store` fixture, giving true cross-backend parity for M1/M2/M3.

- [ ] **Step 2: Run — verify failures**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_crash_lane_parity.py -v`
Expected: sqlite params mostly PASS (sqlite already has the behavior); `postgres` params FAIL — M2 payload lacks `exit_code`, M1 doesn't cap 3-same-fingerprint, M3 has no `skip_unknown` (TypeError on the kwarg or no deferral).

- [ ] **Step 3: Implement — replace `_pg_detect_crashed_workers` with the two-pass version.** Add `_error_fingerprint` and `SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD` to the kanban_db import block (or reference them as `kanban_db._error_fingerprint` / `kanban_db.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD`). Replace the whole method body with:

```python
    def _pg_detect_crashed_workers(self, *, pid_alive_fn=None,
                                   classify_exit_fn=None, skip_unknown=False) -> list:
        """Liveness-based crash reclaim with rc=0 protocol-violation classification
        + systemic-crash fingerprint cap-block (sqlite detect_crashed_workers
        parity). pid_alive_fn None => [] (no server-side OS liveness). skip_unknown
        leaves dead pids that classify 'unknown' for the stale/TTL lane (mirrors
        dispatch_once). Single-host: the sqlite host_prefix(claim_lock) filter is
        intentionally NOT applied (deferred multi-host parity item)."""
        if pid_alive_fn is None:
            return []
        crashed: list = []
        crash_details: list = []  # (tid, pid, lock, protocol_violation, error_text)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, worker_pid, claim_lock FROM tasks "
                "WHERE board=%s AND status='running' AND worker_pid IS NOT NULL",
                (self.board,))
            rows = cur.fetchall()
        for row in rows:
            pid = int(row["worker_pid"])
            try:
                alive = bool(pid_alive_fn(pid))
            except Exception:
                alive = True  # be conservative: don't reclaim on probe error
            if alive:
                continue
            tid = row["id"]
            lock = row["claim_lock"] or ""
            kind, code = ("unknown", None)
            if classify_exit_fn is not None:
                try:
                    kind, code = classify_exit_fn(pid)
                except Exception:
                    kind, code = ("unknown", None)
            if kind == "unknown" and skip_unknown:
                continue
            protocol_violation = (kind == "clean_exit")
            if protocol_violation:
                error_text = ("worker exited cleanly (rc=0) without calling "
                              "kanban_complete or kanban_block — protocol violation")
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid, "claimer": lock, "exit_code": code,
                    "failure_class":
                        kanban_db.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT,
                    "guidance": kanban_db._PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE,
                }
            else:
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
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "UPDATE tasks SET status='ready', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL "
                        "WHERE board=%s AND id=%s AND status='running' "
                        "AND worker_pid=%s", (self.board, tid, pid))
                    if cur.rowcount != 1:
                        continue
                    run_id = self._pg_end_run(
                        cur, tid, outcome="crashed", status="crashed",
                        error=error_text, metadata=event_payload)
                    self._emit(cur, tid, event_kind, event_payload, run_id=run_id)
                    crashed.append(tid)
                    crash_details.append(
                        (tid, pid, lock, protocol_violation, error_text))
        # Pass 2: fingerprint-aware failure accounting (sqlite parity). A genuine
        # (non-protocol-violation) crash whose error fingerprint recurs >= the
        # systemic threshold within this tick caps the breaker immediately.
        if crash_details:
            _fp_counts: dict = {}
            for _, _, _, _, err in crash_details:
                fp = kanban_db._error_fingerprint(err)
                _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
            for tid, pid, lock, protocol_violation, error_text in crash_details:
                fp = kanban_db._error_fingerprint(error_text)
                is_systemic = (
                    not protocol_violation
                    and _fp_counts.get(fp, 0)
                    >= kanban_db.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD)
                extra = {"pid": pid, "claimer": lock}
                if protocol_violation:
                    extra["failure_class"] = \
                        kanban_db.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT
                    extra["guidance"] = \
                        kanban_db._PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE
                self.record_task_failure(
                    tid, error_text, outcome="crashed",
                    failure_limit=1 if (protocol_violation or is_systemic) else None,
                    failure_limit_is_cap=bool(protocol_violation or is_systemic),
                    release_claim=False, end_run=False, event_payload_extra=extra)
        return crashed
```

Then in `dispatch_plan` (the reclaim phase, ~line 2337) pass `skip_unknown=True`:
```python
        result.crashed = self._pg_detect_crashed_workers(
            pid_alive_fn=pid_alive_fn, classify_exit_fn=classify_exit_fn,
            skip_unknown=True)
```

- [ ] **Step 4: Run — verify pass both backends + regression**

Run: `... -m pytest tests/hermes_cli/kanban/test_store_crash_lane_parity.py -v` → all PASS (sqlite + postgres).
Run: `... -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py -q` → existing PG dispatch-tail PASS.
Run: `... -m pytest tests/hermes_cli/test_kanban_db.py -k "crash or dispatch" -q` → sqlite crash/dispatch PASS (untouched).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_crash_lane_parity.py
git commit -m "feat(kanban-pg): crash-lane parity — M1 systemic cap, M2 exit text/code, M3 skip_unknown

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Verification — byte-identical sqlite + forbidden files

**Files:** none (verification only). Fix only if a gap is found.

- [ ] **Step 1: Forbidden files untouched**

Run: `git diff --stat main -- hermes_cli/kanban_db.py hermes_cli/kanban_liveness.py hermes_cli/kanban_writer_daemon.py`
Expected: empty.

- [ ] **Step 2: Only `store_postgres.py` changed (+ the new test)**

Run: `git diff --stat main -- hermes_cli plugins`
Expected: only `hermes_cli/kanban/store_postgres.py`. (sqlite path untouched — the change is entirely inside `PostgresKanbanStore`.)

- [ ] **Step 3: Full crash/dispatch + conformance suite, both backends**

Run: `export HERMES_PG_TEST_DSN="postgresql://postgres:postgres@127.0.0.1:55432/kanban" && venv/bin/python -m pytest tests/hermes_cli/kanban tests/hermes_cli/test_kanban_db.py -q`
Expected: all green on sqlite + postgres params.

- [ ] **Step 4: Commit (only if a verification-driven fix was needed)** — otherwise no-op.

---

## Self-review (plan author, before handoff)

- **Spec coverage:** claim_lock=host:pid (Task 1 + test) ✓; M2 error-text+exit_code (Task 2, `test_m2_*`) ✓; M3 skip_unknown + dispatch_plan (Task 2, `test_m3_skip_unknown_defers` + the dispatch_plan call) ✓; M1 systemic cap (Task 2, `test_m1_systemic_*` + `test_m1_two_below_threshold_*`) ✓; protocol-violation regression (`test_protocol_violation_caps_immediately`) ✓; forbidden files + byte-identical (Task 3) ✓; reuses import-only ✓; host-filter deferred (documented in the method docstring) ✓.
- **Placeholders:** none — full method body + full test code provided; the only judgment calls (the `record_spawn_success` seed shape, the sqlite monkeypatch target names) are flagged with verify-and-adjust notes citing exact sqlite line numbers.
- **Type/name consistency:** `_pg_detect_crashed_workers(..., skip_unknown=False)` signature matches the `dispatch_plan` call (`skip_unknown=True`) and the test calls; `record_task_failure(..., failure_limit_is_cap=...)` matches the existing PG method; reuses (`_error_fingerprint`, `SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD`, `_claimer_id`, `FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT`, `_PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE`) are all real `kanban_db` names (verified against the sqlite source).
