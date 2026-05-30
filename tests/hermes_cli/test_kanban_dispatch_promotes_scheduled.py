"""WS4 Task 3: dispatch_once auto-promotes a cleared active_pr scheduled task
within the tick when the flag is on, and is a no-op when the flag is off."""
from hermes_cli import kanban_db as kb


def _conn(tmp_path):
    return kb.connect(db_path=tmp_path / "kanban.db", readonly=False, _bootstrap=True)


def _park_active_pr(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        kb._append_event(
            conn, tid, "scheduled",
            {"reason": "respawn guard", "respawn_guard": "active_pr"},
        )


def test_dispatch_once_promotes_cleared_scheduled(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    _park_active_pr(conn, tid)
    monkeypatch.setattr(kb, "_promote_scheduled_enabled", lambda: True)
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda *a, **k: False)
    kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    # Promoted out of scheduled within the tick (then ready, or running if the
    # ready scan claimed+spawned it).
    assert kb.get_task(conn, tid).status in ("ready", "running")


def test_dispatch_once_leaves_scheduled_when_flag_off(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    _park_active_pr(conn, tid)
    monkeypatch.setattr(kb, "_promote_scheduled_enabled", lambda: False)
    monkeypatch.setattr(kb, "active_pr_guard_holds", lambda *a, **k: False)
    kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    assert kb.get_task(conn, tid).status == "scheduled"
