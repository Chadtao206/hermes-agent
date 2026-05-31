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
