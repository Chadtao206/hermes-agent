"""WS1 Task 7: kanban tool write handlers route through kb.write_session, so a
worker (a client process, no registered daemon) writes via RemoteWriter instead
of a direct writable connect (which DirectWriteForbidden would block under the
single-writer flag). Reads use a snapshot connection (consistent read-after-write
under WAL/SHM churn; a live mode=ro open can miss freshly-committed WAL writes).
"""
import json
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_daemon as wd
import tools.kanban_tools as kt


def _serve_unregistered(tmp_path, monkeypatch):
    """Run a writer daemon's socket server WITHOUT registering it in this
    process, so write_session resolves to RemoteWriter (the worker path)."""
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"  # == db.parent/.kanban-writer.sock
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    # Bootstrap the schema once (the gateway/owner does this in production).
    kb.connect(db_path=db, _bootstrap=True).close()
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.wait_until_serving(timeout=5)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    return db, sock, server


def test_create_handler_uses_daemon_under_flag(tmp_path, monkeypatch):
    db, sock, server = _serve_unregistered(tmp_path, monkeypatch)
    try:
        res = json.loads(kt._handle_create({"title": "t-flag", "assignee": "engineer"}))
        assert res.get("ok") is True
        tid = res["task_id"]
        ro = kb.connect(db_path=db, readonly=True)
        try:
            row = ro.execute(
                "SELECT title FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["title"] == "t-flag"
        finally:
            ro.close()
    finally:
        server.shutdown()


def test_complete_gate_error_preserved_under_flag(tmp_path, monkeypatch):
    """HallucinatedCardsError raised inside complete_task on the daemon must
    reach _handle_complete as the typed exception so its structured guidance
    (mentioning the phantom ids) is produced — not a generic error."""
    db, sock, server = _serve_unregistered(tmp_path, monkeypatch)
    try:
        tid = server.execute("create_task", title="parent", assignee="worker")
        res = json.loads(kt._handle_complete({
            "task_id": tid, "summary": "done",
            "created_cards": ["t_phantom_zzz"],
        }))
        assert "error" in res
        assert "t_phantom_zzz" in res["error"]
        assert "do not exist" in res["error"]
        # Task must NOT have been completed (gate ran before the write).
        ro = kb.connect(db_path=db, readonly=True)
        try:
            status = ro.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()["status"]
            assert status != "done"
        finally:
            ro.close()
    finally:
        server.shutdown()


def test_simple_write_handlers_under_flag(tmp_path, monkeypatch):
    db, sock, server = _serve_unregistered(tmp_path, monkeypatch)
    try:
        tid = server.execute("create_task", title="c", assignee="worker")
        # comment + link + block + unblock must all route without
        # DirectWriteForbidden.
        c = json.loads(kt._handle_comment({"task_id": tid, "body": "a note"}))
        assert c.get("ok") is True
        child = server.execute("create_task", title="child", assignee="worker")
        lk = json.loads(kt._handle_link({"parent_id": tid, "child_id": child}))
        assert lk.get("ok") is True
        b = json.loads(kt._handle_block({"task_id": tid, "reason": "need input"}))
        assert b.get("ok") is True
        u = json.loads(kt._handle_unblock({"task_id": tid}))
        assert u.get("ok") is True
    finally:
        server.shutdown()


def test_list_handler_recomputes_ready_under_flag(tmp_path, monkeypatch):
    """_handle_list calls recompute_ready (a write). Under the flag the list
    connection is read-only, so the promotion must route through the writer
    instead of attempting a write on the ro conn — which raised
    'attempt to write a readonly database' and surfaced as a tool_error on
    EVERY kanban_list call (the B1 regression). The routed write also has to
    cross the socket, so recompute_ready must be on OP_ALLOWLIST or _dispatch
    would reject it. The cleared dependency must be visible as ready."""
    db, sock, server = _serve_unregistered(tmp_path, monkeypatch)
    try:
        parent = server.execute("create_task", title="parent", assignee="worker")
        child = server.execute("create_task", title="child", assignee="worker")
        server.execute("link_tasks", parent_id=parent, child_id=child)
        server.execute("complete_task", task_id=parent, summary="done")

        res = json.loads(kt._handle_list({}))
        assert "error" not in res, res  # no readonly-write error; no allowlist rejection
        statuses = {t["id"]: t["status"] for t in res["tasks"]}
        assert statuses.get(child) == "ready", statuses
        assert isinstance(res["promoted"], int)  # field intact; count is 0 here
    finally:
        server.shutdown()


def test_read_handler_uses_readonly_under_flag(tmp_path, monkeypatch):
    db, sock, server = _serve_unregistered(tmp_path, monkeypatch)
    try:
        tid = server.execute("create_task", title="readable", assignee="worker")
        res = json.loads(kt._handle_show({"task_id": tid}))
        # _handle_show must not raise DirectWriteForbidden under the flag.
        assert "error" not in res or "readable" in json.dumps(res)
        assert res.get("task_id") == tid or res.get("id") == tid or "readable" in json.dumps(res)
    finally:
        server.shutdown()


def test_connect_uses_snapshot_reader_under_single_writer(monkeypatch):
    """Agent-tool reads must use a snapshot connection (not a live mode=ro open)
    under single-writer, so a just-committed write is visible on read-back. A
    live mode=ro open can miss WAL-resident commits during churn, which caused
    'task not found' right after create -> duplicate task creation."""
    import contextlib

    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True)
    used = {"snapshot": 0}

    @contextlib.contextmanager
    def fake_snapshot(board=None):
        used["snapshot"] += 1
        class _C:
            pass
        yield _C()

    def fake_connect(*a, **k):
        raise AssertionError(
            "agent reads must not open a live connection under single-writer; "
            "use snapshot_connect"
        )

    monkeypatch.setattr(kb, "snapshot_connect", fake_snapshot)
    monkeypatch.setattr(kb, "connect", fake_connect)

    _kb, conn = kt._connect(board=None)
    conn.close()
    assert used["snapshot"] == 1
