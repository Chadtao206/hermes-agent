import socket
import threading
import time
from pathlib import Path

from hermes_cli import kanban_writer_protocol as proto
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_db as kb


def _client_call(sock_path, op, **kwargs):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(sock_path))
    s.sendall(proto.encode(proto.Request("t", op, kwargs).to_wire()))
    resp = proto.read_frame(lambda n: s.recv(n))
    s.close()
    return resp


def _wait_for_sock(sock, tries=100):
    for _ in range(tries):
        if Path(sock).exists():
            return
        time.sleep(0.02)


def test_daemon_serves_create_task(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        _wait_for_sock(sock)
        resp = _client_call(sock, "create_task", title="hello", assignee="engineer")
        assert resp["ok"] is True
        new_id = resp["result"]
        conn = kb.connect(db_path=db, readonly=True)
        row = conn.execute("SELECT title, assignee FROM tasks WHERE id = ?", (new_id,)).fetchone()
        assert row["title"] == "hello"
        assert row["assignee"] == "engineer"
    finally:
        server.shutdown()


def test_daemon_rejects_unknown_op(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        _wait_for_sock(sock)
        resp = _client_call(sock, "DROP_TABLE", x=1)
        assert resp["ok"] is False
        assert resp["error_type"] == "UnknownOp"
    finally:
        server.shutdown()


def test_daemon_serves_many_sequential_clients_distinct_threads(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        _wait_for_sock(sock)
        ids = []
        for i in range(10):
            resp = _client_call(sock, "create_task", title=f"t{i}", assignee="engineer")
            assert resp["ok"] is True, resp
            ids.append(resp["result"])
        assert len(set(ids)) == 10
        conn = kb.connect(db_path=db, readonly=True)
        assert conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 10
    finally:
        server.shutdown()
