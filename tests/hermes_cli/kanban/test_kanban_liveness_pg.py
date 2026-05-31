import os, shutil, uuid
import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker")),
    reason="postgres backend unavailable")


@pytest.fixture
def pg(_pg_dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    board = f"liv_{uuid.uuid4().hex[:8]}"
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def test_compute_board_liveness_pg(pg):
    from hermes_cli import kanban_liveness as liv
    from psycopg.rows import dict_row
    s, pool, board = pg
    now = 1_000_000
    # oldest ready: a ready task backdated 5000s
    r = s.create_task(title="r", assignee="engineer")
    with pool.connection() as c:
        c.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                  (now - 5000, board, r))
    # blocked-with-done-parents: parent completed, child blocked, dep link, backdated 7000s
    parent = s.create_task(title="p", assignee="engineer")
    s.claim_task(parent, claimer="w1"); s.complete_task(parent, summary="done")
    child = s.create_task(title="c", assignee="engineer")
    s.link_tasks(parent, child)
    s.block_task(child, reason="x")
    with pool.connection() as c:
        c.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                  (now - 7000, board, child))
    with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
        snap = liv.compute_board_liveness_pg(cur, board, now=now)
    assert snap.oldest_ready_age_seconds == 5000
    assert snap.oldest_blocked_done_parents_age_seconds == 7000
    assert snap.oldest_stale_running_age_seconds == 0

    # stale-running positive case: a running task whose last heartbeat is 3000s old
    runt = s.create_task(title="run", assignee="engineer")
    s.claim_task(runt, claimer="w2")
    with pool.connection() as c:
        c.execute("UPDATE tasks SET last_heartbeat_at=%s WHERE board=%s AND id=%s",
                  (now - 3000, board, runt))
    with pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
        snap2 = liv.compute_board_liveness_pg(cur, board, now=now)
    assert snap2.oldest_stale_running_age_seconds == 3000
