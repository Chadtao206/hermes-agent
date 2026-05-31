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


def test_enforce_max_runtime_calls_terminate_fn(pg_store):
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


def test_crash_clean_exit_is_protocol_violation(pg_store):
    tid = _running_with_pid(pg_store)
    pg_store._pg_detect_crashed_workers(
        pid_alive_fn=lambda pid: False,
        classify_exit_fn=lambda pid: ("clean_exit", 0))
    t = pg_store.get_task(tid)
    assert t.status == "blocked"   # rc=0 protocol violation caps the breaker
    kinds = [e.kind for e in pg_store.list_events(tid)]
    assert "protocol_violation" in kinds and "gave_up" in kinds
    import json as _json
    def _payload(ev):
        return ev.payload if isinstance(ev.payload, dict) else _json.loads(ev.payload)
    events = pg_store.list_events(tid)
    pv = [e for e in events if e.kind == "protocol_violation"][-1]
    assert _payload(pv).get("failure_class") == "protocol_violation_clean_exit"
    gu = [e for e in events if e.kind == "gave_up"][-1]
    assert _payload(gu).get("failure_class") == "protocol_violation_clean_exit"


def test_crash_signaled_is_retryable(pg_store):
    tid = _running_with_pid(pg_store)
    pg_store._pg_detect_crashed_workers(
        pid_alive_fn=lambda pid: False,
        classify_exit_fn=lambda pid: ("signaled", 9))
    t = pg_store.get_task(tid)
    assert t.status == "ready"     # genuine crash -> retry
    assert "crashed" in [e.kind for e in pg_store.list_events(tid)]


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
    import time as _t2
    with pg_store._pool.connection() as c, c.cursor() as cc:
        cc.execute("SELECT claim_expires FROM tasks WHERE board=%s AND id=%s",
                   (pg_store.board, tid))
        assert cc.fetchone()[0] > int(_t2.time())


def test_stale_claim_reclaims_when_pid_dead(pg_store):
    import time as _t
    tid = _running_with_pid(pg_store)
    with pg_store._pool.connection() as c, c.cursor() as cc:
        cc.execute("UPDATE tasks SET claim_expires=%s WHERE board=%s AND id=%s",
                   (int(_t.time()) - 5, pg_store.board, tid))
    n = pg_store._pg_release_stale_claims(pid_alive_fn=lambda pid: False)
    assert n == 1
    assert pg_store.get_task(tid).status == "ready"
    assert "reclaimed" in [e.kind for e in pg_store.list_events(tid)]
