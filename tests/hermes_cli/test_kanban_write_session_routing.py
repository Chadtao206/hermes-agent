import threading, time
from pathlib import Path
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_db as kb


def _wait(sock):
    for _ in range(100):
        if Path(sock).exists(): return
        time.sleep(0.02)


def test_write_session_routes_to_registered_daemon(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait(sock)
    wd.register_daemon(db, server)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db)
    try:
        with kb.write_session() as w:
            nid = w.create_task(title="ws", assignee="engineer")
        ro = kb.connect(db_path=db, readonly=True)
        assert ro.execute("SELECT COUNT(*) c FROM tasks WHERE title='ws'").fetchone()["c"] == 1
    finally:
        wd.unregister_daemon(db); server.shutdown()
