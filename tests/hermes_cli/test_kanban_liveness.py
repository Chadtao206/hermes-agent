"""WS6 Task 1: read-only board-liveness signals + threshold evaluation."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_liveness as liv


def _conn(tmp_path):
    return kb.connect(db_path=tmp_path / "kanban.db", readonly=False, _bootstrap=True)


def test_oldest_ready_age_flagged(tmp_path):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    conn.execute(
        "UPDATE tasks SET status='ready', created_at=1000 WHERE id=?", (tid,)
    )
    conn.commit()
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert snap.oldest_ready_age_seconds == 9000
    breaches = liv.evaluate(snap, thresholds={"oldest_ready_age_seconds": 600})
    assert any(b.dimension == "oldest_ready_age_seconds" for b in breaches)


def test_healthy_board_no_breach(tmp_path):
    conn = _conn(tmp_path)
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert liv.evaluate(snap, thresholds={"oldest_ready_age_seconds": 600}) == []


def test_blocked_with_done_parents_flagged(tmp_path):
    conn = _conn(tmp_path)
    parent = kb.create_task(conn, title="p", assignee="engineer")
    child = kb.create_task(conn, title="c", assignee="engineer")
    kb.link_tasks(conn, parent_id=parent, child_id=child, relation_type="dependency")
    # Parent done; child blocked but now unblockable (every dep parent terminal).
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    conn.execute(
        "UPDATE tasks SET status='blocked', created_at=2000 WHERE id=?", (child,)
    )
    conn.commit()
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert snap.oldest_blocked_done_parents_age_seconds == 8000


def test_blocked_with_open_parent_not_flagged(tmp_path):
    conn = _conn(tmp_path)
    parent = kb.create_task(conn, title="p", assignee="engineer")
    child = kb.create_task(conn, title="c", assignee="engineer")
    kb.link_tasks(conn, parent_id=parent, child_id=child, relation_type="dependency")
    # Parent still running → child is legitimately blocked, not a stall.
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
    conn.execute(
        "UPDATE tasks SET status='blocked', created_at=2000 WHERE id=?", (child,)
    )
    conn.commit()
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert snap.oldest_blocked_done_parents_age_seconds == 0


def test_stale_running_uses_heartbeat(tmp_path):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    conn.execute(
        "UPDATE tasks SET status='running', last_heartbeat_at=3000 WHERE id=?", (tid,)
    )
    conn.commit()
    snap = liv.compute_board_liveness(conn, now=10_000)
    assert snap.oldest_stale_running_age_seconds == 7000


def test_evaluate_flags_subsystem_disabled():
    snap = liv.Liveness(notifier_enabled=False, writer_daemon_disabled=True)
    dims = {b.dimension for b in liv.evaluate(snap, thresholds={})}
    assert {"notifier_disabled", "writer_daemon_disabled"} <= dims
    # Dispatch stalls surface via the age-based oldest_ready breach (which is
    # agnostic to WHERE the dispatcher runs), not a gateway-local binary
    # "dispatcher_disabled" signal — that would false-page deployments whose
    # dispatcher legitimately runs outside the gateway (kanban.dispatch_in_gateway
    # =false + an external `hermes kanban dispatch`).
    assert "dispatcher_disabled" not in dims
