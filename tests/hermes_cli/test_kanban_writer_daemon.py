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


def test_daemon_execute_in_process_returns_raw(tmp_path):
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait_for_sock(sock)
    try:
        new_id = server.execute("create_task", title="ip", assignee="engineer")
        assert isinstance(new_id, str)
        conn = kb.connect(db_path=db, readonly=True)
        assert conn.execute("SELECT title FROM tasks WHERE id=?", (new_id,)).fetchone()["title"] == "ip"
    finally:
        server.shutdown()


def test_registry_register_lookup_unregister(tmp_path):
    db = tmp_path / "kanban.db"
    server = wd.WriterDaemon(db_path=db, socket_path=tmp_path / ".s")
    wd.register_daemon(db, server)
    assert wd.lookup_daemon(db) is server
    wd.unregister_daemon(db)
    assert wd.lookup_daemon(db) is None


def test_second_daemon_refuses_when_one_owns_board(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    pid = tmp_path / ".kanban-writer.pid"
    s1 = wd.WriterDaemon(db_path=db, socket_path=sock, pid_path=pid)
    assert s1.acquire_singleton() is True
    s2 = wd.WriterDaemon(db_path=db, socket_path=sock, pid_path=pid)
    assert s2.acquire_singleton() is False  # first owner holds the board
    s1.release_singleton()
    # After release, a new daemon can acquire it.
    s3 = wd.WriterDaemon(db_path=db, socket_path=sock, pid_path=pid)
    assert s3.acquire_singleton() is True
    s3.release_singleton()


def test_daemon_heals_on_corruption_signal_and_retries(tmp_path, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_db as kb
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    server.enable_auto_recovery(backup_dir=tmp_path, keep=2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait_for_sock(sock)
    try:
        # Make the FIRST create_task raise a corruption signal, then behave normally.
        real = kb.create_task
        calls = {"n": 0}
        def flaky(conn, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real(conn, **kw)
        monkeypatch.setattr(kb, "create_task", flaky)
        # The op should heal (checkpoint rung on the healthy file) + retry + succeed.
        new_id = server.execute("create_task", title="post-heal", assignee="engineer")
        assert isinstance(new_id, str)
        assert server.health()["disabled"] is False
        assert server.health()["last_recovery"] is not None  # recovery ran
    finally:
        server.shutdown()


def test_daemon_disables_when_recovery_exhausted(tmp_path, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_db as kb
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    server.enable_auto_recovery(backup_dir=tmp_path, keep=2)
    server._fail_recovery_for_test = True  # force recover_board -> exhausted
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait_for_sock(sock)
    try:
        def boom(conn, **kw):
            raise sqlite3.DatabaseError("database disk image is malformed")
        monkeypatch.setattr(kb, "create_task", boom)
        with __import__("pytest").raises(Exception):
            server.execute("create_task", title="x", assignee="engineer")
        assert server.health()["disabled"] is True
    finally:
        server.shutdown()


def test_force_backup_now_creates_snapshot(tmp_path):
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    server.enable_auto_recovery(backup_dir=tmp_path, keep=2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait_for_sock(sock)
    try:
        server.execute("create_task", title="snap", assignee="engineer")
        snap = server.force_backup_now()
        assert snap is not None and snap.exists()
    finally:
        server.shutdown()


def test_daemon_executes_dispatch_once_dry_run(tmp_path):
    import time
    from pathlib import Path
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(100):
        if Path(sock).exists(): break
        time.sleep(0.02)
    try:
        server.execute("create_task", title="d", assignee="engineer")
        # Route the actual dispatcher op through the daemon's writer thread.
        # dry_run=True so it computes a tick without spawning any worker.
        result = server.execute("dispatch_once", dry_run=True)
        assert result is not None  # a DispatchResult came back, no error raised
    finally:
        server.shutdown()
