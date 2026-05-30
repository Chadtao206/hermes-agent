import threading
import time
from pathlib import Path

from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_writer_client as wc
from hermes_cli import kanban_db as kb


def _start_daemon(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.02)
    return db, sock, server


def test_remote_writer_create_roundtrips(tmp_path):
    db, sock, server = _start_daemon(tmp_path)
    try:
        with wc.RemoteWriter(sock) as w:
            new_id = w.create_task(title="via-client", assignee="engineer")
        conn = kb.connect(db_path=db, readonly=True)
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (new_id,)).fetchone()
        assert row["title"] == "via-client"
    finally:
        server.shutdown()


def test_remote_writer_raises_on_daemon_error(tmp_path):
    db, sock, server = _start_daemon(tmp_path)
    try:
        with wc.RemoteWriter(sock) as w:
            try:
                w.create_task()  # missing required 'title' kwarg -> daemon raises
                assert False, "expected RemoteWriteError"
            except wc.RemoteWriteError as exc:
                assert exc.error_type
    finally:
        server.shutdown()
