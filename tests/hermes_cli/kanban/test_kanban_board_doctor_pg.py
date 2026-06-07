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
    board = f"doc_{uuid.uuid4().hex[:8]}"
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def test_doctor_pg_detects_defects_and_redacts_dsn(pg):
    from hermes_cli import kanban_board_doctor as kdoc
    s, pool, board = pg
    # orphan task_link: link to a non-existent child
    a = s.create_task(title="a", assignee="engineer")
    with pool.connection() as c:
        c.execute("INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                  "VALUES (%s,%s,%s,'dependency')", (board, a, "t_ghostchild"))
        # old ready task: backdate well past the 900s threshold
        c.execute("UPDATE tasks SET created_at=created_at-100000 WHERE board=%s AND id=%s",
                  (board, a))
    res = kdoc._run_board_doctor_pg(board=board, ready_age_seconds=900, pool=pool)
    kinds = {i["kind"] for i in res["issues"]}
    assert "orphan_task_link" in kinds
    assert "old_ready_task" in kinds
    assert res["ok"] is False
    # db_path is the redacted postgres identifier, NOT a password
    assert res["db_path"].startswith("postgres://")
    assert "@" not in res["db_path"]   # no user:password@ userinfo may leak into db_path


def test_doctor_pg_unreachable_is_critical(pg):
    from hermes_cli import kanban_board_doctor as kdoc
    s, pool, board = pg

    class _BadPool:  # .connection() raises -> connectivity probe fails fast
        def connection(self, *a, **k):
            raise RuntimeError("pool down")

    res = kdoc._run_board_doctor_pg(board=board, ready_age_seconds=900, pool=_BadPool())
    assert res["ok"] is False
    assert any(i["severity"] == "critical" and i["kind"] == "pg_unreachable"
               for i in res["issues"])
    # connectivity failure short-circuits: no logical-check issues mixed in
    assert all(i["kind"] == "pg_unreachable" for i in res["issues"])


def test_doctor_pg_unresolvable_dsn_degrades(pg, monkeypatch):
    from hermes_cli import kanban_board_doctor as kdoc
    from hermes_cli.kanban import pg_pool
    s, pool, board = pg
    def _boom(*a, **k):
        raise RuntimeError("kanban backend=postgres but no DSN configured")
    monkeypatch.setattr(pg_pool, "get_pool", _boom)
    # no pool arg -> _run_board_doctor_pg falls back to pg_pool.get_pool(), which raises
    res = kdoc._run_board_doctor_pg(board=board, ready_age_seconds=900)
    assert res["ok"] is False
    assert any(i["kind"] == "pg_unreachable" and i["severity"] == "critical"
               for i in res["issues"])
    assert "reconcile_summary" in res  # shape uniformity


def test_doctor_pg_surfaces_dependency_chain_decision_flag_and_superseded_duplicate(pg):
    from hermes_cli import kanban_board_doctor as kdoc

    s, pool, board = pg
    human_gate = s.create_task(title="human architecture gate", assignee="ops")
    s.claim_task(human_gate)
    s.block_task(human_gate, reason="human decision required")

    blocked_parent = s.create_task(title="waiting on human", assignee="reviewer", parents=[human_gate])
    decision_packet = s.create_task(title="decision packet", assignee="ops")
    s.claim_task(decision_packet)
    s.complete_task(decision_packet, summary="done", metadata={"chad_decision_required": True})

    downstream = s.create_task(
        title="downstream review",
        assignee="reviewer",
        parents=[decision_packet, blocked_parent],
    )
    canonical = s.create_task(title="publish gate", assignee="ops")
    duplicate = s.create_task(title="publish gate", assignee="ops")
    with pool.connection() as c:
        c.execute(
            "INSERT INTO task_links (board, parent_id, child_id, relation_type) VALUES (%s,%s,%s,'supersedes')",
            (board, canonical, duplicate),
        )

    res = kdoc._run_board_doctor_pg(board=board, ready_age_seconds=900, pool=pool)
    matching = {issue["kind"]: issue for issue in res["issues"]}

    assert matching["todo_with_completed_parents_blocked_by_ancestor"]["task_id"] == downstream
    assert human_gate in matching["todo_with_completed_parents_blocked_by_ancestor"]["blocked_ancestors"]
    assert matching["completed_closeout_decision_flag_without_gate"]["task_id"] == decision_packet
    assert matching["superseded_duplicate_task"]["task_id"] == duplicate
    assert matching["superseded_duplicate_task"]["superseded_by"] == canonical
    assert "reconcile_summary" in res
