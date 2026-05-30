"""WS1 acceptance gate: with the single-writer daemon, many concurrent clients
— and a client SIGKILL'd mid-write — cannot corrupt the board, because no client
holds a writable DB handle; only the daemon's one writer thread writes.
"""
import multiprocessing as mp
import os
import signal
import threading
import time
from pathlib import Path

from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_writer_client as wc
from hermes_cli import kanban_db as kb


def _spam(sock_path, n, label):
    with wc.RemoteWriter(Path(sock_path)) as w:
        for i in range(n):
            w.create_task(title=f"{label}-{i}", assignee="engineer")


def test_many_concurrent_clients_then_kill_one_keeps_db_healthy(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.wait_until_serving(timeout=5)
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.02)
    try:
        threads = [
            threading.Thread(target=_spam, args=(str(sock), 40, f"c{n}"))
            for n in range(6)
        ]
        for t in threads:
            t.start()
        # Kill a client mid-flight: it holds NO writable DB handle, so the board
        # cannot be torn — only the daemon's writer thread writes.
        victim = mp.Process(target=_spam, args=(str(sock), 1000, "victim"))
        victim.start()
        time.sleep(0.2)
        os.kill(victim.pid, signal.SIGKILL)
        victim.join(timeout=10)
        for t in threads:
            t.join(timeout=30)

        ro = kb.connect(db_path=db, readonly=True)
        try:
            assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # The 6 in-process clients each committed 40 rows.
            count = ro.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
            assert count >= 240
        finally:
            ro.close()
    finally:
        server.shutdown()
