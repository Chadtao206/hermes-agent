"""End-to-end: dashboard write endpoints route through the single-writer daemon.

These exercise the REAL remote path a dashboard process uses under single-writer:
write_session -> RemoteWriter -> unix socket -> WriterDaemon._dispatch (which
enforces OP_ALLOWLIST) -> writer thread -> DB. A regression (missing allowlist
entry, wrong kwarg, or a stray writable connect) surfaces here as an exception
or a missing row, exactly as it would in the live dashboard.
"""
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_daemon as wd
from plugins.kanban.dashboard import plugin_api as api


def _wait_sock(p: Path) -> None:
    for _ in range(250):
        if Path(p).exists():
            return
        time.sleep(0.02)
    raise RuntimeError("daemon socket never appeared")


@pytest.fixture
def daemon_board(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    # Real dashboard semantics: a separate process pinned to this board's socket.
    # With the socket env set, single_writer_enabled() is True and write_session
    # routes over the socket (no in-process daemon registration).
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_WRITER_SOCK", str(sock))
    boot = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    boot.close()
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait_sock(sock)
    assert kb.single_writer_enabled() is True  # guard: we're on the remote path
    try:
        yield db
    finally:
        server.shutdown()


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _new_task(title="t", assignee="engineer") -> str:
    body = api.create_task(
        api.CreateTaskBody(title=title, assignee=assignee), board=None
    )
    return body["task"]["id"]


def test_create_task_writes_through_daemon(daemon_board):
    tid = _new_task(title="demo")
    con = _ro(daemon_board)
    try:
        row = con.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        con.close()
    assert row and row[0] == "demo"


def test_add_comment_writes_through_daemon(daemon_board):
    tid = _new_task()
    api.add_comment(tid, api.CommentBody(body="hello world"), board=None)
    con = _ro(daemon_board)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_profile_sub_upsert_and_remove_through_daemon(daemon_board):
    """The 'Wake Hermes profiles' add/remove flow the user reported."""
    tid = _new_task()
    res = api.upsert_profile_sub(tid, api.ProfileSubBody(profile="engineer"), board=None)
    assert res["ok"] is True
    con = _ro(daemon_board)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM kanban_profile_event_subs WHERE task_id=? AND profile='engineer'",
            (tid,),
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1

    api.remove_profile_sub(tid, "engineer", name="", board=None)
    con = _ro(daemon_board)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM kanban_profile_event_subs WHERE task_id=? AND profile='engineer'",
            (tid,),
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_reassign_writes_through_daemon(daemon_board):
    tid = _new_task(assignee="engineer")
    api.reassign_task_endpoint(tid, api.ReassignBody(profile="reviewer"), board=None)
    con = _ro(daemon_board)
    try:
        assignee = con.execute(
            "SELECT assignee FROM tasks WHERE id=?", (tid,)
        ).fetchone()[0]
    finally:
        con.close()
    assert assignee == "reviewer"


def test_link_and_unlink_through_daemon(daemon_board):
    parent = _new_task(title="parent")
    child = _new_task(title="child")
    api.add_link(api.LinkBody(parent_id=parent, child_id=child), board=None)
    con = _ro(daemon_board)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id=? AND child_id=?",
            (parent, child),
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1

    api.delete_link(parent_id=parent, child_id=child, board=None)
    con = _ro(daemon_board)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id=? AND child_id=?",
            (parent, child),
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_all_dashboard_write_ops_are_allowlisted():
    """Every op the dashboard routes via `w.<op>(...)` must be in OP_ALLOWLIST,
    or it would be rejected over the socket with 'op not allowed' (a 500)."""
    import re

    src = Path(api.__file__).read_text()
    routed = set(re.findall(r"\bw\.(\w+)\(", src))
    missing = routed - wd.OP_ALLOWLIST
    assert not missing, f"dashboard routes ops not in OP_ALLOWLIST: {sorted(missing)}"


def _col(db: Path, tid: str, col: str):
    con = _ro(db)
    try:
        return con.execute(f"SELECT {col} FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    finally:
        con.close()


def test_update_task_status_priority_title_through_daemon(daemon_board):
    from fastapi import HTTPException

    db = daemon_board
    tid = _new_task(title="orig", assignee="engineer")

    # status: -> blocked -> ready (unblock) -> todo (direct)
    api.update_task(tid, api.UpdateTaskBody(status="blocked", block_reason="x"), board=None)
    assert _col(db, tid, "status") == "blocked"
    api.update_task(tid, api.UpdateTaskBody(status="ready"), board=None)
    assert _col(db, tid, "status") == "ready"
    api.update_task(tid, api.UpdateTaskBody(status="todo"), board=None)
    assert _col(db, tid, "status") == "todo"

    # priority
    api.update_task(tid, api.UpdateTaskBody(priority=7), board=None)
    assert _col(db, tid, "priority") == 7

    # title / body
    api.update_task(tid, api.UpdateTaskBody(title="renamed", body="newbody"), board=None)
    assert _col(db, tid, "title") == "renamed"
    assert _col(db, tid, "body") == "newbody"

    # empty title -> 400 (handler pre-validation, before any write)
    with pytest.raises(HTTPException) as ei:
        api.update_task(tid, api.UpdateTaskBody(title="   "), board=None)
    assert ei.value.status_code == 400

    # 'running' is rejected with 400, not routed
    with pytest.raises(HTTPException) as ei2:
        api.update_task(tid, api.UpdateTaskBody(status="running"), board=None)
    assert ei2.value.status_code == 400


def test_delete_task_through_daemon(daemon_board):
    from fastapi import HTTPException

    db = daemon_board
    tid = _new_task(title="todelete")
    api.delete_task(tid, board=None)
    con = _ro(db)
    try:
        assert con.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone() is None
    finally:
        con.close()
    with pytest.raises(HTTPException) as ei:
        api.delete_task(tid, board=None)
    assert ei.value.status_code == 404


def test_bulk_update_priority_and_assignee_through_daemon(daemon_board):
    db = daemon_board
    t1 = _new_task(title="a", assignee="engineer")
    t2 = _new_task(title="b", assignee="engineer")
    res = api.bulk_update(
        api.BulkTaskBody(ids=[t1, t2, "nope"], priority=9, assignee="reviewer"),
        board=None,
    )
    byid = {r["id"]: r for r in res["results"]}
    assert byid[t1]["ok"] and byid[t2]["ok"]
    assert byid["nope"]["ok"] is False and "not found" in byid["nope"]["error"]
    assert _col(db, t1, "priority") == 9 and _col(db, t2, "priority") == 9
    assert _col(db, t1, "assignee") == "reviewer"
    assert _col(db, t2, "assignee") == "reviewer"


def test_dispatch_degrades_to_noop_under_single_writer(daemon_board):
    """dispatch_once spawns workers + reclaims, so it can't run on the writer
    thread; under single-writer the manual nudge returns an informative no-op
    (the gateway auto-dispatches) instead of a 500."""
    res = api.dispatch(dry_run=False, max_n=8, board=None)
    assert res["spawned"] == []
    assert res["reclaimed"] == 0
    assert "note" in res and "single-writer" in res["note"]
