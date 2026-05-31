"""C2: notifier board writes route through the single-writer daemon.

Under ``kanban.single_writer_daemon`` the gateway process owns the board's
writer daemon, and the notifier tick's writes route through it rather than a
direct writable ``connect`` (which would raise ``DirectWriteForbidden``).
Also covers corruption back-off under recovery.
"""
import asyncio

import pytest

import gateway.run as gr
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_daemon as wd


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})


def _make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: _RecordingAdapter()}
    runner._kanban_sub_fail_counts = {}
    return runner


def _notify_cursor(db_path, task_id):
    ro = kb.connect(db_path=db_path, readonly=True)
    try:
        row = ro.execute(
            "SELECT last_event_id FROM kanban_notify_subs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return row["last_event_id"] if row else None
    finally:
        ro.close()


def _notify_sub_exists(db_path, task_id):
    ro = kb.connect(db_path=db_path, readonly=True)
    try:
        row = ro.execute(
            "SELECT 1 FROM kanban_notify_subs WHERE task_id=?", (task_id,),
        ).fetchone()
        return row is not None
    finally:
        ro.close()


def test_notifier_tick_claims_and_advances_through_daemon(tmp_path, monkeypatch):
    """A full tick under the flag must deliver a (non-terminal) event without a
    DirectWriteForbidden: the read-modify-write claim and the post-send cursor
    advance both route through the writer daemon; reads use a readonly conn."""
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)

    started = gr._spawn_writer_daemons([db], auto_recovery=False)
    try:
        tid = started[0].execute("create_task", title="watch me", assignee="worker")
        started[0].execute(
            "add_notify_sub", task_id=tid, platform="telegram", chat_id="chat-1",
        )
        # A `blocked` event is terminal-kind (delivered) but does NOT make the
        # task done, so the subscription survives and the cursor advance runs.
        started[0].execute("block_task", task_id=tid, reason="needs review")

        runner = _make_runner()
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

        adapter = runner.adapters[Platform.TELEGRAM]
        assert len(adapter.sent) == 1
        assert tid in adapter.sent[0]["text"]
        assert "blocked" in adapter.sent[0]["text"]
        # Subscription survived (not terminal) and its cursor was advanced past
        # 0 by the daemon-routed claim+advance.
        assert _notify_sub_exists(db, tid)
        assert _notify_cursor(db, tid) > 0
    finally:
        for d in started:
            wd.unregister_daemon(d.db_path)
            d.shutdown()


def test_corruption_backs_off_instead_of_disabling_under_recovery(
    tmp_path, monkeypatch, caplog
):
    """With the daemon + auto-recovery on, a confirmed-corrupt board read must
    NOT permanently disable the notifier for the process — the daemon owns
    recovery, so the notifier backs off and retries next tick."""
    shared_db = tmp_path / "kanban.db"

    def fake_list_boards(include_archived=False):
        return [{"slug": "default", "db_path": str(shared_db)}]

    def fake_connect(*, board=None, readonly=False, **kwargs):
        raise kb.KanbanDbCorruptError(
            shared_db, None,
            "integrity_check returned 'database disk image is malformed'",
        )

    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.setattr(gr, "_writer_auto_recovery_enabled", lambda: True)
    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        gr, "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner()
    for _ in range(4):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    disabled = getattr(runner, "_kanban_notifier_disabled_db_paths", {})
    assert str(shared_db.resolve()) not in disabled
