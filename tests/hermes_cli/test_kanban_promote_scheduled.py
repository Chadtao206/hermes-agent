"""WS4 Task 2: promote_cleared_scheduled un-parks scheduled/active_pr tasks once
the PR guard clears, and leaves time/operator scheduled parks alone."""
from hermes_cli import kanban_db as kb


def _conn(tmp_path):
    return kb.connect(db_path=tmp_path / "kanban.db", readonly=False, _bootstrap=True)


def _park_active_pr(conn, tid):
    """Reproduce the dispatcher's active_pr park: scheduled + marker event."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        kb._append_event(
            conn, tid, "scheduled",
            {"reason": "respawn guard: recent PR URL detected",
             "respawn_guard": "active_pr"},
        )


def test_promotes_when_guard_cleared(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    _park_active_pr(conn, tid)
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda *a, **k: False)
    assert kb.promote_cleared_scheduled(conn) == 1
    assert kb.get_task(conn, tid).status == "ready"


def test_not_promoted_when_guard_still_holds(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    _park_active_pr(conn, tid)
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda *a, **k: True)
    assert kb.promote_cleared_scheduled(conn) == 0
    assert kb.get_task(conn, tid).status == "scheduled"


def test_time_based_scheduled_park_untouched(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    kb.schedule_task(conn, tid, reason="waiting on a cron window")  # no active_pr marker
    # Even with the guard reporting clear, a non-active_pr park is left alone.
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda *a, **k: False)
    assert kb.promote_cleared_scheduled(conn) == 0
    assert kb.get_task(conn, tid).status == "scheduled"


def test_emits_ready_event_on_promotion(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    _park_active_pr(conn, tid)
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda *a, **k: False)
    kb.promote_cleared_scheduled(conn)
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "ready" in kinds
