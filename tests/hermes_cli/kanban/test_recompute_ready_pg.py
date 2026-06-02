"""Postgres-only recompute_ready tests.

These cover the two things that are intentionally NOT cross-backend:
  * blocked tasks are left untouched on PG (the deferred sticky-block parity
    gap — sqlite WOULD promote a non-sticky blocked task, so this can't be a
    parametrized conformance assertion);
  * the 'promoted' event shape emitted by the set-based path.

Both use parent-less roots driven via set_status_direct, which does NOT trigger
an internal recompute for 'todo'/'blocked' (only 'done'/'ready' do) — so the
explicit recompute_ready() call is the sole promoter and is directly observable.

Skips automatically when no Postgres is available (docker / HERMES_PG_TEST_DSN).
"""
from uuid import uuid4

import pytest


@pytest.fixture
def pg_store(_pg_dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=f"test_{uuid4().hex[:8]}", pool=pool)
    try:
        yield store
    finally:
        store.close()
        pool.close()


def test_recompute_leaves_blocked_untouched(pg_store):
    # recompute_ready only promotes status='todo'; a blocked task must be left
    # alone on PG (sticky-block re-promotion is the deferred parity gap).
    t = pg_store.create_task(title="root", assignee="engineer")
    assert pg_store.set_status_direct(t, "blocked") is True
    pg_store.recompute_ready()
    assert pg_store.get_task(t).status == "blocked"


def test_recompute_promoted_event_shape(pg_store):
    t = pg_store.create_task(title="root", assignee="engineer")
    assert pg_store.set_status_direct(t, "todo") is True
    assert pg_store.recompute_ready() == 1
    promoted = [e for e in pg_store.list_events(t) if e.kind == "promoted"]
    assert len(promoted) == 1
    assert promoted[0].run_id is None
    assert promoted[0].payload is None
