# Phase 4.5 — `phase-3-tail` Close-out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all six `phase-3-tail` deferrals so the Postgres backend's dispatch/kill/crash-reap reaches parity with SQLite — the Phase-5 cutover preconditions.

**Architecture:** The PG store owns DB-transition logic; OS choreography (full SIGTERM→grace→SIGKILL ladder, reap+rc=0 classification) is injected from the gateway via callbacks and forwarded by the glue. We **reuse existing `kanban_db` OS helpers** (`_terminate_reclaimed_worker`, `reap_worker_zombies`, `_classify_worker_exit`, `_pid_alive`) — `kanban_db.py` is upstream and is **never edited**. The contract change is **additive** (`terminate_fn`/`classify_exit_fn` added alongside the existing `signal_fn` fallback), so every step works and the live sqlite dispatch stays byte-identical (it ignores these callbacks).

**Tech Stack:** Python 3, `psycopg` 3, pytest with the docker-`postgres:16-alpine` conformance fixture (`tests/hermes_cli/kanban/conftest.py`).

---

## Pre-flight (executor)

- Worktree: `.worktrees/kanban-pg-phase45-tail`, branch `feat/kanban-pg-phase45-tail` off `main` @ current HEAD (use superpowers:using-git-worktrees).
- **Test interpreter (mandatory):** `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest …` (only this venv has `psycopg`+`pytest`; `-m pytest` imports the worktree's code). Docker must be running (the `_pg_dsn` session fixture auto-starts `postgres:16-alpine`).
- Design: `plans/kanban-postgres-migration/phase-3-tail-closeout-design.md`.

## Reference facts (verified against `main`)

- **PG reclaim methods** (`hermes_cli/kanban/store_postgres.py`): `_pg_release_stale_claims(*, signal_fn=None)` (~1496), `_pg_enforce_max_runtime(*, signal_fn=None)` (~1555), `_pg_detect_stale_running(*, stale_timeout_seconds=0, signal_fn=None)` (~1634), `_pg_detect_crashed_workers(*, pid_alive_fn=None)` (~1712). `dispatch_plan(..., signal_fn=None, pid_alive_fn=None)` (~1873) wires them at ~1898-1902; pre-spawn defer at ~2011-2020.
- **Reusable PG helpers (already exist):** `_pg_synthesize_ended_run(cur, task_id, *, outcome, summary=None, error=None, metadata=None) -> int`; `_pg_end_run(cur, task_id, *, outcome, …)`; `_emit(cur, task_id, kind, payload=None, run_id=None)`; `record_task_failure(task_id, error, *, outcome, failure_limit=None, failure_limit_is_cap=False, release_claim=True, end_run=True, event_payload_extra=None) -> bool`.
- **SQLite references to mirror** (`hermes_cli/kanban_db.py`, **read-only**): `_record_pre_spawn_validation_failure` (~7386), `_block_systemic_spawn_failure_signature` (~7231), `release_stale_claims` live-pid extension (~4370-4408), `_classify_worker_exit(pid) -> (kind, code)` (~6282), `reap_worker_zombies() -> list[int]` (~6333), `_terminate_reclaimed_worker(pid, claim_lock, *, signal_fn=None) -> dict` (~6421), `_pid_alive` (~6357), `_pre_spawn_validation_errors(task) -> list[str]` (~7373), `_resolve_claim_ttl_seconds(ttl=None) -> int` (default `DEFAULT_CLAIM_TTL_SECONDS = 15*60`), `_synthesize_ended_run` columns (~4033). Constants: `FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT`, `_PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE`, `FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE`, `_SYSTEMIC_SPAWN_FAILURE_GUIDANCE`, `SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD = 3`.
- **Glue** (`hermes_cli/kanban_glue.py`): `run_dispatch_tick(store, *, …, signal_fn=None, pid_alive_fn=None)` (~80) forwards to `dispatch_plan` (~133); `_record_dispatch_spawn_failure` systemic branch (~183-219).
- **Gateway** (`gateway/run.py`): PG dispatch branch ~7065-7097 (`run_dispatch_tick(..., signal_fn=os.kill, pid_alive_fn=_kb._pid_alive, …)`); sqlite branch ~7170 (`signal_fn=None`). Dead helpers `_kanban_advance`/`_kanban_unsub`/`_kanban_rewind`/`_kanban_profile_advance`/`_kanban_profile_rewind`/`_kanban_profile_record_success`/`_kanban_profile_record_failure` ~6320-6573 (keep `_kanban_profile_wake`). Stale test ref: `tests/hermes_cli/test_kanban_notifier_single_writer.py` uses `_kanban_advance`.
- **PG-targeted test fixture:** the conformance `store` fixture parametrizes both backends; A3/A4/A5 assert PG-internal mechanics (callback invocation), so they use a dedicated `pg_store` fixture (build `PostgresKanbanStore` from `_pg_dsn`, per-test board) and skip when PG is unavailable. A1/A2 are cross-backend → use the conformance `store` fixture.

---

# PART A — store + glue (conformance-gated, no gateway/run.py touch)

## Task A1: Pre-spawn validation auto-block (pure DB)

**Files:** Modify `hermes_cli/kanban/store_postgres.py`; Test `tests/hermes_cli/kanban/test_store_conformance.py`.

- [ ] **Step 1: Write the failing conformance test** (append to `test_store_conformance.py`):

```python
def test_pre_spawn_validation_auto_blocks(store, monkeypatch):
    # A ready task whose forced skill cannot be resolved fails pre-spawn
    # validation; both backends must auto-block it (not silently defer).
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda a: True,
                        raising=False)
    tid = store.create_task(title="bad skill", assignee="engineer",
                            skills=["__phase45_missing_skill__"])
    assert store.get_task(tid).status == "ready"
    store.dispatch_plan(profile_exists=lambda a: True, max_spawn=5)
    assert store.get_task(tid).status == "blocked"
    kinds = [e.kind for e in store.list_events(tid)]
    assert "pre_spawn_validation_failed" in kinds
    assert "gave_up" in kinds
    assert "blocked" in kinds
```

- [ ] **Step 2: Run, verify it FAILS on postgres** (sqlite already passes; PG defers):

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py::test_pre_spawn_validation_auto_blocks -v`
Expected: the `postgres` param FAILS (`status == "ready"`, no events); `sqlite` passes.

- [ ] **Step 3: Implement** — add the method to `PostgresKanbanStore` and call it from `dispatch_plan`.

Add the method (near `_pg_synthesize_ended_run`):
```python
    def _pg_record_pre_spawn_validation_failure(self, task_id: str,
                                                errors: list) -> bool:
        """Mirror kanban_db._record_pre_spawn_validation_failure: flip a ready
        task to blocked, synth an ended run, emit the failure/gave_up/blocked
        events. Returns True if it blocked the task."""
        reason = "pre-spawn validation failed: " + "; ".join(errors)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT consecutive_failures, status FROM tasks "
                    "WHERE board=%s AND id=%s", (self.board, task_id))
                row = cur.fetchone()
                if row is None or row["status"] != "ready":
                    return False
                failures = int(row["consecutive_failures"] or 0) + 1
                cur.execute(
                    "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, "
                    "consecutive_failures=%s, last_failure_error=%s "
                    "WHERE board=%s AND id=%s AND status='ready' "
                    "AND claim_lock IS NULL",
                    (failures, reason[:500], self.board, task_id))
                if cur.rowcount != 1:
                    return False
                metadata = {
                    "failure_class": "pre_spawn_validation",
                    "validation_errors": list(errors),
                    "failures": failures,
                    "effective_limit": 1,
                    "limit_source": "pre_spawn_validation",
                }
                run_id = self._pg_synthesize_ended_run(
                    cur, task_id, outcome="spawn_failed", summary=reason,
                    error=reason[:500], metadata=metadata)
                payload = dict(metadata)
                payload["error"] = reason[:500]
                self._emit(cur, task_id, "pre_spawn_validation_failed",
                           payload, run_id=run_id)
                self._emit(cur, task_id, "gave_up", payload, run_id=run_id)
                self._emit(cur, task_id, "blocked", {"reason": reason},
                           run_id=run_id)
                return True
```

In `dispatch_plan`, replace the pre-spawn `phase-3-tail` block (~2012-2020) — keep recording `pre_spawn_blocked`, then auto-block:
```python
            validation_errors = _pre_spawn_validation_errors(task_for_validation)
            if validation_errors:
                reason = "; ".join(validation_errors)
                result.pre_spawn_blocked.append((tid, reason))
                if self._pg_record_pre_spawn_validation_failure(tid, validation_errors):
                    result.auto_blocked.append(tid)
                continue
```

- [ ] **Step 4: Run, verify PASS on both backends:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py::test_pre_spawn_validation_auto_blocks -v`
Expected: both `sqlite` and `postgres` PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban-pg): pre-spawn validation auto-block on PG dispatch_plan"
```

---

## Task A2: Systemic-spawn-failure sibling pre-emptive block

**Files:** Modify `hermes_cli/kanban/store.py` (Protocol), `store_postgres.py`, `store_sqlite.py`, `hermes_cli/kanban_glue.py`; Test `test_store_conformance.py`.

- [ ] **Step 1: Write the failing conformance test:**

```python
def test_block_systemic_spawn_failure_signature(store):
    a = store.create_task(title="a", assignee="engineer")
    b = store.create_task(title="b", assignee="engineer")
    c = store.create_task(title="c", assignee="engineer")
    assert all(store.get_task(t).status == "ready" for t in (a, b, c))
    blocked = store.block_systemic_spawn_failure_signature(
        [a, b, c], failure_signature="boom", error="spawn boom",
        signature_count=3)
    assert set(blocked) == {a, b, c}
    for t in (a, b, c):
        assert store.get_task(t).status == "blocked"
        # sibling block must NOT bump the per-task failure counter
        assert store.get_task(t).consecutive_failures == 0
        kinds = [e.kind for e in store.list_events(t)]
        assert "systemic_failure_signature" in kinds and "gave_up" in kinds \
            and "blocked" in kinds
```

- [ ] **Step 2: Run, verify it FAILS on both** (method missing):

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py::test_block_systemic_spawn_failure_signature -v`
Expected: FAIL — `AttributeError: 'PostgresKanbanStore'/'SqliteKanbanStore' object has no attribute 'block_systemic_spawn_failure_signature'`.

- [ ] **Step 3: Implement** the method on the Protocol + both stores + wire the glue.

Add to `KanbanStore` Protocol in `store.py` (after `record_spawn_failure`):
```python
    def block_systemic_spawn_failure_signature(
        self, task_ids, *, failure_signature: str, error: str,
        signature_count: int,
    ) -> list: ...
```

Add to `SqliteKanbanStore` (delegates to the canonical kanban_db helper on a local writable conn). This method is only reached on the PG path and the **non-single-writer** sqlite path (single-writer prod routes dispatch through the daemon's `dispatch_once`, which blocks siblings internally), so a local writable conn is correct here and **no `kanban_db.py` edit / OP_ALLOWLIST entry is needed**:
```python
    def block_systemic_spawn_failure_signature(self, task_ids, *,
                                               failure_signature, error,
                                               signature_count):
        conn = kb.connect(board=self.board, readonly=False)
        try:
            return kb._block_systemic_spawn_failure_signature(
                conn, list(task_ids), failure_signature=failure_signature,
                error=error, signature_count=signature_count)
        finally:
            conn.close()
```

Add to `PostgresKanbanStore`:
```python
    def block_systemic_spawn_failure_signature(self, task_ids, *,
                                               failure_signature, error,
                                               signature_count):
        """Mirror kanban_db._block_systemic_spawn_failure_signature: block ready
        siblings sharing a spawn-failure signature WITHOUT re-incrementing their
        counters. Returns the ids actually blocked."""
        reason = ("systemic spawn failure: multiple tasks failed with the same "
                  "spawn error signature; platform/profile fix required before retry")
        blocked = []
        seen = list(dict.fromkeys(task_ids))
        for task_id in seen:
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "SELECT status, consecutive_failures FROM tasks "
                        "WHERE board=%s AND id=%s", (self.board, task_id))
                    row = cur.fetchone()
                    if row is None or row["status"] != "ready":
                        continue
                    cur.execute(
                        "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, last_failure_error=%s "
                        "WHERE board=%s AND id=%s AND status='ready' "
                        "AND claim_lock IS NULL",
                        (error[:500], self.board, task_id))
                    if cur.rowcount != 1:
                        continue
                    payload = {
                        "failure_class": kanban_db.FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE,
                        "failure_signature": failure_signature,
                        "signature_count": int(signature_count),
                        "signature_threshold":
                            kanban_db.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD,
                        "failures": int(row["consecutive_failures"] or 0),
                        "effective_limit": 1,
                        "limit_source": "systemic_failure_signature",
                        "trigger_outcome": "spawn_failed",
                        "error": error[:500],
                        "guidance": kanban_db._SYSTEMIC_SPAWN_FAILURE_GUIDANCE,
                    }
                    self._emit(cur, task_id, "systemic_failure_signature", payload)
                    self._emit(cur, task_id, "gave_up", payload)
                    self._emit(cur, task_id, "blocked", {"reason": reason})
                    blocked.append(task_id)
        return blocked
```

Wire the glue: in `kanban_glue.py` `_record_dispatch_spawn_failure`, replace the `phase-3-tail` comment block (~202-213) in the `systemic` branch with a real call after the `record_task_failure`:
```python
            newly = store.block_systemic_spawn_failure_signature(
                group, failure_signature=signature, error=error,
                signature_count=len(group))
            for bid in newly:
                if bid not in glue_auto_blocked:
                    glue_auto_blocked.append(bid)
```
(Keep the `auto` handling that follows: `if auto and task_id not in glue_auto_blocked: glue_auto_blocked.append(task_id)`.)

- [ ] **Step 4: Run, verify PASS on both backends:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_conformance.py::test_block_systemic_spawn_failure_signature tests/hermes_cli/kanban/test_kanban_glue.py -v`
Expected: PASS (both backends; glue tests still green).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store.py hermes_cli/kanban/store_postgres.py hermes_cli/kanban/store_sqlite.py hermes_cli/kanban_glue.py tests/hermes_cli/kanban/test_store_conformance.py
git commit -m "feat(kanban-pg): systemic-spawn-failure sibling pre-emptive block + glue wiring"
```

---

## Task A3: `terminate_fn` full-ladder contract (additive)

**Files:** Modify `store.py` (Protocol), `store_postgres.py`, `store_sqlite.py`, `kanban_glue.py`; Test new `tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py`.

- [ ] **Step 1: Write the failing PG-targeted test** — create `test_store_pg_dispatch_tail.py`:

```python
import os
import shutil
import uuid
import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker")),
    reason="postgres backend unavailable")


@pytest.fixture
def pg_store(_pg_dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    s = PostgresKanbanStore(board=f"tail_{uuid.uuid4().hex[:8]}", pool=pool)
    try:
        yield s
    finally:
        s.close()
        pool.close()


def _running_with_pid(store, *, max_runtime=None):
    tid = store.create_task(title="run", assignee="engineer",
                            max_runtime_seconds=max_runtime)
    store.claim_task(tid, claimer="host-A:123")
    store.record_spawn_success(tid, 4242)
    return tid


def test_enforce_max_runtime_calls_terminate_fn(pg_store, monkeypatch):
    import time as _t
    tid = _running_with_pid(pg_store, max_runtime=1)
    # Force the run to look old enough to time out.
    with pg_store._pool.connection() as c, c.cursor() as cc:
        cc.execute("UPDATE task_runs SET started_at=%s WHERE board=%s AND task_id=%s",
                   (int(_t.time()) - 10, pg_store.board, tid))
    calls = []
    pg_store._pg_enforce_max_runtime(
        terminate_fn=lambda pid, lock: calls.append((pid, lock)))
    assert calls == [(4242, "host-A:123")]
    assert pg_store.get_task(tid).status == "ready"
    assert "timed_out" in [e.kind for e in pg_store.list_events(tid)]
```

- [ ] **Step 2: Run, verify it FAILS** (`terminate_fn` not accepted):

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py -v`
Expected: FAIL — `_pg_enforce_max_runtime() got an unexpected keyword argument 'terminate_fn'`.

- [ ] **Step 3: Implement** — add `terminate_fn` to the three reclaim methods, the PG `dispatch_plan`, the Protocol, the sqlite store signature, and the glue forward. `terminate_fn(pid, claim_lock)` is preferred; `signal_fn(pid, SIGTERM)` is the documented single-shot fallback.

In each of `_pg_enforce_max_runtime`, `_pg_detect_stale_running`, `_pg_release_stale_claims`: add `terminate_fn=None` to the signature, ensure the SELECT includes `claim_lock` (add `t.claim_lock` to the `_pg_enforce_max_runtime` and `_pg_detect_stale_running` SELECTs; `_pg_release_stale_claims` already selects `claim_lock`), and replace the `if signal_fn is not None …: signal_fn(pid, _sig.SIGTERM)` block with this helper call. Add ONE shared helper to the class:
```python
    @staticmethod
    def _invoke_kill(terminate_fn, signal_fn, pid, claim_lock):
        """Prefer the full host-guarded ladder (terminate_fn(pid, claim_lock));
        fall back to a single best-effort SIGTERM (signal_fn(pid, SIGTERM))."""
        if not pid:
            return
        if terminate_fn is not None:
            try:
                terminate_fn(int(pid), claim_lock)
            except Exception:
                pass
        elif signal_fn is not None:
            try:
                import signal as _sig
                signal_fn(int(pid), _sig.SIGTERM)
            except Exception:
                pass
```
Then in each reclaim method, where it currently signals, call:
`self._invoke_kill(terminate_fn, signal_fn, pid, row["claim_lock"])` (for `_pg_enforce_max_runtime`/`_pg_detect_stale_running` use the row's claim_lock now selected; `_pg_release_stale_claims` uses `row["claim_lock"]`).

In `dispatch_plan` signature add `terminate_fn=None` and forward it:
```python
        result.reclaimed = self._pg_release_stale_claims(
            terminate_fn=terminate_fn, signal_fn=signal_fn)
        result.stale = self._pg_detect_stale_running(
            stale_timeout_seconds=stale_timeout_seconds,
            terminate_fn=terminate_fn, signal_fn=signal_fn)
        result.timed_out = self._pg_enforce_max_runtime(
            terminate_fn=terminate_fn, signal_fn=signal_fn)
```
Add `terminate_fn=None` to the Protocol `dispatch_plan` (store.py ~108-122) and to the SqliteKanbanStore `dispatch_plan` signature (accept-and-ignore, like `signal_fn`). In `kanban_glue.py` `run_dispatch_tick`, add `terminate_fn=None` to the signature and pass `terminate_fn=terminate_fn` into `store.dispatch_plan(...)`.

- [ ] **Step 4: Run, verify PASS** (+ glue/conformance still green):

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py tests/hermes_cli/kanban/test_store_conformance.py tests/hermes_cli/kanban/test_kanban_glue.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store.py hermes_cli/kanban/store_postgres.py hermes_cli/kanban/store_sqlite.py hermes_cli/kanban_glue.py tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py
git commit -m "feat(kanban-pg): terminate_fn(pid,claim_lock) full-ladder contract (additive)"
```

---

## Task A4: `classify_exit_fn` — rc=0 protocol-violation vs crashed

**Files:** Modify `store.py`, `store_postgres.py`, `store_sqlite.py`, `kanban_glue.py`; Test `test_store_pg_dispatch_tail.py`.

- [ ] **Step 1: Write the failing test** (append to `test_store_pg_dispatch_tail.py`):

```python
def test_crash_clean_exit_is_protocol_violation(pg_store):
    tid = _running_with_pid(pg_store)
    pg_store._pg_detect_crashed_workers(
        pid_alive_fn=lambda pid: False,
        classify_exit_fn=lambda pid: ("clean_exit", 0))
    t = pg_store.get_task(tid)
    assert t.status == "blocked"   # rc=0 protocol violation caps the breaker
    kinds = [e.kind for e in pg_store.list_events(tid)]
    assert "protocol_violation" in kinds and "gave_up" in kinds


def test_crash_signaled_is_retryable(pg_store):
    tid = _running_with_pid(pg_store)
    pg_store._pg_detect_crashed_workers(
        pid_alive_fn=lambda pid: False,
        classify_exit_fn=lambda pid: ("signaled", 9))
    t = pg_store.get_task(tid)
    assert t.status == "ready"     # genuine crash -> retry
    assert "crashed" in [e.kind for e in pg_store.list_events(tid)]
```

- [ ] **Step 2: Run, verify it FAILS** (`classify_exit_fn` not accepted / no protocol_violation).

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py -k crash -v`
Expected: FAIL.

- [ ] **Step 3: Implement** — add `classify_exit_fn=None` to `_pg_detect_crashed_workers`, `dispatch_plan` (forward it), the Protocol, sqlite store, and the glue forward. Replace the dead-pid branch body so it classifies:

```python
    def _pg_detect_crashed_workers(self, *, pid_alive_fn=None,
                                   classify_exit_fn=None) -> list:
        if pid_alive_fn is None:
            return []
        crashed: list = []
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
                alive = True
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
                error_text = f"pid {pid} not alive ({kind})"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": lock, "exit_kind": kind}
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
                        cur, tid, outcome=("protocol_violation" if protocol_violation
                                           else "crashed"),
                        status=("protocol_violation" if protocol_violation
                                else "crashed"),
                        error=error_text, metadata=event_payload)
                    self._emit(cur, tid, event_kind, event_payload, run_id=run_id)
                    crashed.append(tid)
            # rc=0 protocol violation caps the breaker immediately; genuine
            # crashes use the normal counter so a flaky task can still retry.
            self.record_task_failure(
                tid, error_text, outcome="crashed",
                failure_limit=1 if protocol_violation else None,
                failure_limit_is_cap=protocol_violation,
                release_claim=False, end_run=False,
                event_payload_extra={"exit_kind": kind})
        return crashed
```
Forward `classify_exit_fn` from `dispatch_plan` (`result.crashed = self._pg_detect_crashed_workers(pid_alive_fn=pid_alive_fn, classify_exit_fn=classify_exit_fn)`), add `classify_exit_fn=None` to the Protocol + sqlite `dispatch_plan`, and forward it through `run_dispatch_tick`.

- [ ] **Step 4: Run, verify PASS:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py tests/hermes_cli/kanban/test_store_conformance.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store.py hermes_cli/kanban/store_postgres.py hermes_cli/kanban/store_sqlite.py hermes_cli/kanban_glue.py tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py
git commit -m "feat(kanban-pg): rc=0 protocol-violation classification via classify_exit_fn"
```

---

## Task A5: Live-pid claim-extension

**Files:** Modify `store_postgres.py` (+ forward `pid_alive_fn` to `_pg_release_stale_claims`); Test `test_store_pg_dispatch_tail.py`.

- [ ] **Step 1: Write the failing test:**

```python
def test_stale_claim_extends_when_pid_alive(pg_store):
    import time as _t
    tid = _running_with_pid(pg_store)
    # Expire the claim in the past.
    with pg_store._pool.connection() as c, c.cursor() as cc:
        cc.execute("UPDATE tasks SET claim_expires=%s WHERE board=%s AND id=%s",
                   (int(_t.time()) - 5, pg_store.board, tid))
    n = pg_store._pg_release_stale_claims(pid_alive_fn=lambda pid: True)
    assert n == 0                                   # extended, not reclaimed
    assert pg_store.get_task(tid).status == "running"
    assert "claim_extended" in [e.kind for e in pg_store.list_events(tid)]


def test_stale_claim_reclaims_when_pid_dead(pg_store):
    import time as _t
    tid = _running_with_pid(pg_store)
    with pg_store._pool.connection() as c, c.cursor() as cc:
        cc.execute("UPDATE tasks SET claim_expires=%s WHERE board=%s AND id=%s",
                   (int(_t.time()) - 5, pg_store.board, tid))
    n = pg_store._pg_release_stale_claims(pid_alive_fn=lambda pid: False)
    assert n == 1
    assert pg_store.get_task(tid).status == "ready"
```

- [ ] **Step 2: Run, verify it FAILS** (no extension; both reclaim).

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py -k stale_claim -v`
Expected: `test_stale_claim_extends_when_pid_alive` FAILS (reclaims instead of extends).

- [ ] **Step 3: Implement** — add `pid_alive_fn=None` to `_pg_release_stale_claims`; before reclaiming a stale row, if `pid_alive_fn` says the worker is alive, EXTEND instead. Insert at the top of the `for row in stale:` loop (before the kill/reclaim):

```python
        for row in stale:
            pid = row["worker_pid"]
            if pid_alive_fn is not None and pid:
                try:
                    alive = bool(pid_alive_fn(int(pid)))
                except Exception:
                    alive = False
                if alive:
                    new_expires = now + kanban_db._resolve_claim_ttl_seconds()
                    with self._pool.connection() as conn, \
                            conn.cursor(row_factory=dict_row) as cur:
                        with conn.transaction():
                            cur.execute(
                                "UPDATE tasks SET claim_expires=%s "
                                "WHERE board=%s AND id=%s AND status='running' "
                                "AND claim_expires IS NOT NULL AND claim_expires < %s",
                                (new_expires, self.board, row["id"], now))
                            if cur.rowcount != 1:
                                continue
                            cur.execute(
                                "UPDATE task_runs SET claim_expires=%s "
                                "WHERE board=%s AND task_id=%s AND ended_at IS NULL",
                                (new_expires, self.board, row["id"]))
                            self._emit(cur, row["id"], "claim_extended", {
                                "reason": "pid_alive",
                                "worker_pid": int(pid),
                                "claim_lock": row["claim_lock"],
                                "claim_expires_was": int(row["claim_expires"]),
                                "claim_expires_now": new_expires,
                                "last_heartbeat_at": (
                                    int(row["last_heartbeat_at"])
                                    if row["last_heartbeat_at"] is not None
                                    else None),
                            })
                    continue
            # ... existing kill + reclaim path (now via self._invoke_kill) ...
```
Forward `pid_alive_fn` from `dispatch_plan`: `result.reclaimed = self._pg_release_stale_claims(terminate_fn=terminate_fn, signal_fn=signal_fn, pid_alive_fn=pid_alive_fn)`.

- [ ] **Step 4: Run, verify PASS:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py -k stale_claim -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban/store_postgres.py tests/hermes_cli/kanban/test_store_pg_dispatch_tail.py
git commit -m "feat(kanban-pg): live-pid claim-extension in _pg_release_stale_claims"
```

---

# PART B — gateway wiring + dead-helper cleanup (high-risk, last)

## Task B1: Wire terminate_fn / classify_exit_fn / reap into the gateway PG dispatch branch

**Files:** Modify `gateway/run.py` (PG branch ~7065-7097 only).

- [ ] **Step 1: Implement** — in the `_resolve_backend() == "postgres"` branch, reap before the tick and pass the full-ladder + classifier callbacks. Replace the `run_dispatch_tick(...)` call's OS-callback kwargs:

```python
                store = _kanban_store(board=slug)
                # Reap exited worker children so classify_exit_fn can read rc.
                try:
                    _kb.reap_worker_zombies()
                except Exception:
                    pass
                try:
                    return _glue.run_dispatch_tick(
                        store,
                        board=slug,
                        spawn_fn=_kb._default_spawn,
                        resolve_workspace=_kb.resolve_workspace,
                        profile_exists=_profile_exists,
                        terminate_fn=lambda pid, lock: _kb._terminate_reclaimed_worker(
                            pid, lock, signal_fn=os.kill),
                        pid_alive_fn=_kb._pid_alive,
                        classify_exit_fn=_kb._classify_worker_exit,
                        max_spawn=max_spawn,
                        max_in_progress=max_in_progress,
                        failure_limit=failure_limit,
                        stale_timeout_seconds=stale_timeout_seconds,
                        default_assignee=default_assignee,
                        max_in_progress_per_profile=max_in_progress_per_profile,
                    )
```
(Drop the old `signal_fn=os.kill`; `terminate_fn` supersedes it. The sqlite branch at ~7170 is untouched — it still passes `signal_fn=None` and ignores all of these.)

- [ ] **Step 2: Verify the gateway module imports cleanly + the dispatcher tests pass:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -c "import gateway.run"`
Expected: no error.
Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_notifier_single_writer.py tests/hermes_cli/kanban/test_kanban_glue.py -q`
Expected: PASS (the sqlite single-writer dispatch path is unchanged).

- [ ] **Step 3: Commit**

```bash
git add gateway/run.py
git commit -m "feat(kanban-pg): gateway wires full kill-ladder + reap + exit-classify on the PG dispatch path"
```

---

## Task B2: Remove dead gateway helpers + fix the stale test reference

**Files:** Modify `gateway/run.py`; `tests/hermes_cli/test_kanban_notifier_single_writer.py`.

- [ ] **Step 1: Confirm each helper is unused** (only `_kanban_profile_wake` and the test ref should remain):

Run: `cd <worktree> && grep -rn "_kanban_advance\|_kanban_unsub\|_kanban_rewind\|_kanban_profile_advance\|_kanban_profile_rewind\|_kanban_profile_record_success\|_kanban_profile_record_failure" gateway/ hermes_cli/ tests/`
Expected: definitions in `gateway/run.py` + references only in `tests/hermes_cli/test_kanban_notifier_single_writer.py`. (If any non-test production reference appears, STOP and report — do not delete a live helper.)

- [ ] **Step 2: Remove the seven dead methods** from `gateway/run.py` (the `_kanban_advance`, `_kanban_unsub`, `_kanban_rewind`, `_kanban_profile_advance`, `_kanban_profile_rewind`, `_kanban_profile_record_success`, `_kanban_profile_record_failure` definitions, ~6320-6573). Keep `_kanban_profile_wake`.

- [ ] **Step 3: Fix the stale test** — in `tests/hermes_cli/test_kanban_notifier_single_writer.py`, remove the test(s)/assertions that call the now-deleted `_kanban_advance` (the notifier cursor advance is exercised by the store/glue path now). If a test is wholly about the deleted helper, delete that test; otherwise reroute it through `store.advance_notify_cursor`. Report which you did.

- [ ] **Step 4: Verify import + suites green:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -c "import gateway.run" && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/test_kanban_notifier_single_writer.py tests/hermes_cli/test_kanban_notifier.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/hermes_cli/test_kanban_notifier_single_writer.py
git commit -m "chore(kanban): remove dead gateway notifier helpers superseded by the glue"
```

---

## Task B3: Acceptance + close the phase-3-tail markers + runbook update

**Files:** verify-only + `plans/kanban-postgres-migration/cutover-runbook.md` (mark preconditions closed).

- [ ] **Step 1: Confirm no `phase-3-tail` markers remain** in the store/glue:

Run: `cd <worktree> && grep -rn "phase-3-tail" hermes_cli/`
Expected: EMPTY (all six markers removed/closed). If any remain, they must be closed or explicitly justified.

- [ ] **Step 2: Full kanban suite, both backends:**

Run: `cd <worktree> && /Users/ctao/.hermes/hermes-agent/venv/bin/python -m pytest tests/hermes_cli/kanban/ tests/hermes_cli/test_kanban_db.py -q`
Expected: PASS (conformance both backends; sqlite regression unchanged).

- [ ] **Step 3: Confirm `kanban_db.py` is unedited + sqlite dispatch byte-identical:**

Run: `cd <worktree> && git diff --stat main -- hermes_cli/kanban_db.py`
Expected: EMPTY.

- [ ] **Step 4: Update the cutover runbook** — in `plans/kanban-postgres-migration/cutover-runbook.md`, mark the six `phase-3-tail` BLOCKING preconditions as CLOSED (reference this branch), since they are now implemented + tested. Commit:

```bash
git add plans/kanban-postgres-migration/cutover-runbook.md
git commit -m "docs(kanban-pg): mark phase-3-tail cutover preconditions closed"
```

- [ ] **Step 5: Finish the branch** — use superpowers:finishing-a-development-branch.

---

## Self-review notes (author)

- **Spec coverage:** kill-ladder (A3 contract + B1 wiring), rc=0 protocol-violation (A4 + B1), pre-spawn auto-block (A1), systemic-sibling block (A2), live-pid claim-extension (A5), dead-helper cleanup (B2), runbook close-out (B3). All six items covered.
- **Additive contract:** `terminate_fn`/`classify_exit_fn` are added alongside `signal_fn` (documented fallback), so every task leaves the tree working and the gateway compiles at each step; B1 swaps the gateway to `terminate_fn` and drops `signal_fn=os.kill`.
- **Type consistency:** `block_systemic_spawn_failure_signature`, `_pg_record_pre_spawn_validation_failure`, `_invoke_kill`, `terminate_fn(pid, claim_lock)`, `classify_exit_fn(pid)->(kind,code)` used consistently; Protocol + both stores + glue updated together in A2/A3/A4.
- **Boundaries:** `kanban_db.py` imported (its `_`-helpers reused) but never edited; live sqlite dispatch byte-identical (ignores the new callbacks); default backend unchanged.
- **Known risk to watch in review:** the `_pg_release_stale_claims` task_runs `claim_expires` UPDATE targets the active (un-ended) run via `ended_at IS NULL`; the SQLite reference targets `current_run_id` precisely — confirm a task never has >1 un-ended run on the board so the proxy is exact (it shouldn't, but the reviewer should check).
