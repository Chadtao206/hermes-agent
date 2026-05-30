"""A write-gate exception raised inside a daemon-run write op must reach the
caller as its original typed exception (with attributes) on BOTH the in-process
``execute`` path and the ``RemoteWriter`` socket path — not flattened to a
generic RuntimeError/RemoteWriteError."""
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli.kanban_writer_client import RemoteWriter


def _serve(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.wait_until_serving(timeout=5)
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.02)
    return db, sock, server


def _assert_hallucinated(exc):
    """Assert ``exc`` is a HallucinatedCardsError carrying the phantom list.

    Matched by type *name* + attributes rather than `isinstance`: other tests
    in the suite ``del sys.modules['hermes_cli.kanban_db']`` to force a
    re-import, so the class object can diverge across module instances. In
    production the module is imported once and `except kb.HallucinatedCardsError`
    matches directly (verified when these tests run in isolation)."""
    assert type(exc).__name__ == "HallucinatedCardsError", exc
    assert getattr(exc, "phantom", None) == ["t_phantom_xyz"]


def test_execute_reraises_typed_gate_error_in_process(tmp_path):
    db, sock, server = _serve(tmp_path)
    try:
        tid = server.execute("create_task", title="c", assignee="worker")
        try:
            server.execute(
                "complete_task", task_id=tid, summary="done",
                created_cards=["t_phantom_xyz"],
            )
            raise AssertionError("expected a typed gate error")
        except Exception as exc:
            _assert_hallucinated(exc)
    finally:
        server.shutdown()


def test_execute_wraps_unknown_error_as_runtimeerror(tmp_path):
    """Non-gate errors keep the generic RuntimeError contract (the dispatcher's
    `except RuntimeError` backoff relies on it)."""
    db, sock, server = _serve(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            # Missing the required `title` kwarg → TypeError inside the write fn,
            # not a registered gate error → must surface as RuntimeError so the
            # dispatcher's `except RuntimeError` backoff still applies.
            server.execute("create_task", assignee="worker")
    finally:
        server.shutdown()


def test_remote_writer_reraises_typed_gate_error(tmp_path, monkeypatch):
    db, sock, server = _serve(tmp_path)
    try:
        tid = server.execute("create_task", title="c", assignee="worker")
        with RemoteWriter(sock) as w:
            try:
                w.complete_task(
                    task_id=tid, summary="done", created_cards=["t_phantom_xyz"],
                )
                raise AssertionError("expected a typed gate error")
            except Exception as exc:
                # Must be the reconstructed gate error, NOT a flat RemoteWriteError.
                from hermes_cli.kanban_writer_client import RemoteWriteError
                assert not isinstance(exc, RemoteWriteError), exc
                _assert_hallucinated(exc)
    finally:
        server.shutdown()
