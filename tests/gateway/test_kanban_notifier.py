import asyncio
import sqlite3
from pathlib import Path

import pytest

from gateway.config import Platform
import gateway.run as gateway_run
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_profile_event_table_canary_rejects_impossible_claim_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile wake canary", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="default",
            event_kinds=["completed"],
        )
        conn.execute(
            """
            INSERT INTO kanban_profile_event_claims (
                event_id, profile, name, root_task_id, claimed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("t_not_an_event_id", 1779917918, "", tid, 1779917918),
        )
        with pytest.raises(sqlite3.DatabaseError, match="profile-event table invariant"):
            kb.validate_profile_event_tables(conn)
    finally:
        conn.close()


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_corrupt_db_disable_is_keyed_by_resolved_path(tmp_path, monkeypatch):
    """Once one alias sees DB corruption, same-DB aliases stop retrying.

    The gateway can enumerate multiple board slugs that resolve to the same
    SQLite file. Corruption fail-stop must key by resolved DB path, not slug,
    so a later tick with a different alias first does not keep hammering the
    same unhealthy file.
    """
    shared_db = tmp_path / "shared-corrupt.db"
    orders = [
        [
            {"slug": "alias-a", "db_path": str(shared_db)},
            {"slug": "alias-b", "db_path": str(shared_db)},
        ],
        [
            {"slug": "alias-b", "db_path": str(shared_db)},
            {"slug": "alias-a", "db_path": str(shared_db)},
        ],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise kb.KanbanDbCorruptError(
            shared_db,
            None,
            "integrity_check returned 'wrong # of entries in index idx_profile_event_claims_root'",
        )

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect_calls == ["alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert str(shared_db.resolve()) in disabled
    assert disabled[str(shared_db.resolve())]["reason"] == "corrupt_db"



def test_kanban_notifier_transient_malformed_is_not_db_path_disabled(tmp_path, monkeypatch, caplog):
    """A connection-local malformed read should not process-disable a healthy DB."""
    shared_db = tmp_path / "shared-healthy.db"
    orders = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (False, "confirmation probe quick_check/integrity_check ok"),
    )

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect_calls == ["alias-a", "alias-a"]
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert "treating as transient" in caplog.text


def test_kanban_notifier_disk_io_disable_is_keyed_by_resolved_path(tmp_path, monkeypatch, caplog):
    """Disk I/O fail-stop should disable the DB path once per process.

    If one alias hits `sqlite3.OperationalError("disk I/O error")`, later
    aliases resolving to the same DB must be skipped for the process lifetime.
    """
    shared_db = tmp_path / "shared-disk-io.db"
    orders = [
        [
            {"slug": "alias-a", "db_path": str(shared_db)},
            {"slug": "alias-b", "db_path": str(shared_db)},
        ],
        [
            {"slug": "alias-b", "db_path": str(shared_db)},
            {"slug": "alias-a", "db_path": str(shared_db)},
        ],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect_calls == ["alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert str(shared_db.resolve()) in disabled
    assert disabled[str(shared_db.resolve())]["reason"] == "disk_io_error"
    assert "filesystem-level fault" in caplog.text


def test_kanban_notifier_inner_disk_io_disables_board_path(tmp_path, monkeypatch):
    """Disk I/O during in-conn board reads should quarantine that DB path."""
    shared_db = tmp_path / "inner-disk-io.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    orders = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]
    connect_calls = []
    list_calls = []
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_notify_subs(*args, **kwargs):
        list_calls.append(1)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", fake_list_notify_subs)

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(list_calls) == 1
    assert connect_calls == ["alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert str(shared_db.resolve()) in disabled
    assert disabled[str(shared_db.resolve())]["reason"] == "disk_io_error"


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


# ---------------------------------------------------------------------------
# Event-driven kanban subscriptions: per-sub event_kinds + include_children.
# These pin the first-pass orchestration contract — legacy subs still see only
# terminal events, custom kinds let a sub tail lifecycle moments (created,
# claimed, etc.), and include_children fans descendant events up to a parent.
# ---------------------------------------------------------------------------


def test_legacy_subscription_only_sees_terminal_events(tmp_path, monkeypatch):
    """A sub registered without event_kinds keeps the old terminal-only filter.

    `created` and `claimed` fire on every task during its lifecycle but the
    legacy gateway watcher only surfaces terminal kinds — that must stay
    true even after the event_kinds column lands.
    """
    db_path = tmp_path / "legacy-sub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Only `created` exists on the task; legacy filter must drop it.
    assert adapter.sent == [], (
        f"Legacy subscription should ignore non-terminal events, got "
        f"{[s['text'] for s in adapter.sent]}"
    )

    # Now complete the task — that *is* a terminal event and must deliver.
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="ok")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()


def test_custom_event_kinds_delivers_lifecycle_events(tmp_path, monkeypatch):
    """A sub with explicit ``event_kinds`` receives non-terminal lifecycle pings.

    Pins that the gateway watcher honors per-sub kind filters instead of the
    hardcoded terminal set, and that the renderer doesn't drop `created` /
    `claimed`.
    """
    db_path = tmp_path / "custom-kinds.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="lifecycle task", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            event_kinds=["created", "claimed"],
        )
        # The `create_task` call above already emitted a `created` event
        # before the sub existed (cursor starts at 0, so it'll still be
        # picked up). Add a `claimed` to exercise both kinds in one tick.
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    kinds_delivered = [s["text"] for s in adapter.sent]
    assert any("created" in t for t in kinds_delivered), kinds_delivered
    assert any("claimed" in t for t in kinds_delivered), kinds_delivered
    # No terminal event ever fired, so the only deliveries are the two
    # lifecycle pings we asked for.
    assert len(adapter.sent) == 2, kinds_delivered


def test_include_children_subscription_receives_child_completion(tmp_path, monkeypatch):
    """A parent-level sub with include_children sees descendant completions.

    Verifies the subtree fetch path: events from a child task linked under
    the parent via a dependency edge are claimed under the parent's single
    cursor and rendered against the child task's title.
    """
    db_path = tmp_path / "subtree.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="epic-parent", assignee="lead")
        child_id = kb.create_task(conn, title="leaf-child", assignee="worker")
        kb.link_tasks(conn, parent_id, child_id)
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            include_children=True,
        )
        # Stamp a `completed` event onto the CHILD directly. Driving it
        # through `complete_task` would require running the full
        # ready/claim/run lifecycle, which is out of scope for this test
        # — we're pinning the subtree-fetch contract: a parent sub with
        # include_children sees the child's event.
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "child done"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"Parent sub with include_children should receive the child's "
        f"completion event; got {[s['text'] for s in adapter.sent]}"
    )
    text = adapter.sent[0]["text"]
    assert "done" in text.lower()
    assert child_id in text, (
        f"Rendered message should identify the descendant task; got {text!r}"
    )
    assert "leaf-child" in text, (
        f"Rendered message should use the child's title, not the parent's; "
        f"got {text!r}"
    )



def test_include_children_subscription_survives_parent_completion(tmp_path, monkeypatch):
    """Subtree subscriptions stay alive after root completion.

    In dependency graphs the parent often completes before children become
    ready. A root-level orchestration subscription must therefore survive the
    root's completed event so descendant lane completions still notify Jensen
    or another orchestrator.
    """
    db_path = tmp_path / "subtree-survives-root.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="root lane", assignee="lead")
        child_id = kb.create_task(
            conn, title="child lane", assignee="worker", parents=[parent_id],
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            include_children=True,
        )
        assert kb.complete_task(conn, parent_id, summary="root done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert parent_id in adapter.sent[0]["text"]

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, parent_id)
        assert len(subs) == 1, (
            "include_children subscription must survive root completion so "
            "downstream child lane events can still notify"
        )
        assert kb.complete_task(conn, child_id, summary="child done")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2
    assert child_id in adapter.sent[1]["text"]
    assert "child lane" in adapter.sent[1]["text"]



class FailOnSecondSendAdapter:
    """Adapter that fails only on its second send call."""

    def __init__(self):
        self.calls = 0
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated second-send failure")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def test_multi_event_subscription_rewinds_to_last_success(tmp_path, monkeypatch):
    """Retry after a partial batch failure must not duplicate successes.

    Custom event subscriptions make multi-event batches normal. If the first
    event sends successfully and the second send fails, the notifier should
    rewind only to the first event id, not all the way back to the pre-claim
    cursor. Otherwise the first event is delivered twice on retry.
    """
    db_path = tmp_path / "partial-batch-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="partial failure", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            event_kinds=["created", "claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    adapter = FailOnSecondSendAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "created" in adapter.sent[0]["text"]

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    texts = [item["text"] for item in adapter.sent]
    assert len(texts) == 2, texts
    assert sum("created" in text for text in texts) == 1, texts
    assert sum("claimed" in text for text in texts) == 1, texts



def test_include_children_subscription_unsubs_when_root_archived(tmp_path, monkeypatch):
    """Archive is the explicit cleanup path for subtree subscriptions."""
    db_path = tmp_path / "subtree-archive-cleanup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="root cleanup", assignee="lead")
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            include_children=True,
        )
        assert kb.archive_task(conn, parent_id)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "archived" in adapter.sent[0]["text"].lower()

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, parent_id) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 2: profile-level event subscriptions drive a fire-and-forget
# ``hermes -p <profile> chat -q <prompt>`` wake instead of a chat message.
# These tests pin: (a) the wake fires without any active adapter, (b) the
# spawned command/prompt/env match the contract, (c) spawn failure rewinds
# the cursor so the next tick retries, (d) include_children fans descendant
# events into the wake batch.
# ---------------------------------------------------------------------------


def _make_runner_no_adapters():
    """Runner with the kanban notifier wired but zero connected adapters.

    Phase 2 requires profile wakes to run on adapter-less gateways — this
    fixture pins that invariant.
    """
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._kanban_sub_fail_counts = {}
    return runner


def test_notifier_heartbeat_records_on_adapterless_tick(tmp_path, monkeypatch):
    """The gateway records presence even when only profile wakes are possible."""
    db_path = tmp_path / "adapterless-heartbeat.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    runner = _make_runner_no_adapters()
    runner._kanban_notifier_profile = "default"
    runner._kanban_notifier_started_at = 1_000
    runner._kanban_notifier_host = "test-host"
    runner._kanban_notifier_id = "test-host:4242:1000"
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 4242)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        rows = kb.list_notifier_heartbeats(
            conn,
            board_slug="default",
            db_path=str(db_path.resolve()),
            now=int(kb.time.time()),
        )
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["notifier_id"] == "test-host:4242:1000"
    assert rows[0]["notifier_profile"] == "default"
    assert rows[0]["active"] is True


def test_notifier_heartbeat_failure_does_not_block_profile_wake(tmp_path, monkeypatch):
    """Heartbeat write failures are diagnostics-only; wake claims still run."""
    db_path = tmp_path / "heartbeat-failure-still-wakes.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="wake despite heartbeat fail", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    captured_events = []

    def fake_heartbeat(*args, **kwargs):
        raise RuntimeError("heartbeat table temporarily unavailable")

    def fake_wake(self, sub, events, task, event_tasks, board):
        captured_events.extend(events)
        return True, None

    monkeypatch.setattr(kb, "record_notifier_heartbeat", fake_heartbeat)
    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert captured_events
    assert any(ev.kind == "claimed" for ev in captured_events)


def test_profile_event_wake_spawns_without_adapters(tmp_path, monkeypatch):
    """A profile event sub spawns the wake command with no chat adapter connected."""
    db_path = tmp_path / "profile-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile target", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed", "completed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    captured: dict = {}

    def fake_wake(self, sub, events, task, event_tasks, board):
        captured["sub"] = sub
        captured["events"] = list(events)
        captured["board"] = board
        captured["task_id"] = sub["task_id"]
        captured["profile"] = sub["profile"]
        # Reuse the real prompt builder so we exercise it.
        captured["prompt"] = self._build_kanban_event_wake_prompt(
            sub, events, task, event_tasks, board,
        )
        return True

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake,
    )

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert captured.get("profile") == "jensen", (
        "Wake should fire even with zero connected adapters"
    )
    assert captured["task_id"] == tid
    assert any(ev.kind == "claimed" for ev in captured["events"])
    prompt = captured["prompt"]
    assert "Kanban event wake" in prompt
    assert "DO NOT create cron" in prompt
    assert tid in prompt
    assert "jensen" in prompt
    assert "claimed" in prompt

    # Cursor advanced past the wake batch.
    conn = kb.connect()
    try:
        subs = kb.list_profile_event_subs(conn, task_id=tid)
        assert len(subs) == 1
        assert int(subs[0]["last_event_id"]) > 0
        assert int(subs[0]["last_wake_at"] or 0) > 0
    finally:
        conn.close()


def test_profile_event_wake_includes_child_event(tmp_path, monkeypatch):
    """include_children profile sub picks up a descendant lifecycle event."""
    db_path = tmp_path / "profile-subtree.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="parent", assignee="lead")
        child_id = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent_id, child_id)
        kb.add_profile_event_sub(
            conn,
            task_id=parent_id,
            profile="jensen",
            include_children=True,
        )
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "child done"},
        )
    finally:
        conn.close()

    captured: dict = {}

    def fake_wake(self, sub, events, task, event_tasks, board):
        captured["events"] = list(events)
        captured["event_tasks"] = dict(event_tasks)
        return True

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake,
    )

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    events = captured.get("events") or []
    assert any(e.task_id == child_id and e.kind == "completed" for e in events), (
        f"include_children parent sub should see child completion; got "
        f"{[(e.task_id, e.kind) for e in events]}"
    )
    ev_tasks = captured.get("event_tasks") or {}
    assert child_id in ev_tasks
    assert ev_tasks[child_id] is not None
    assert ev_tasks[child_id].title == "child"


def test_profile_event_wake_dedupes_overlapping_child_roots(tmp_path, monkeypatch):
    """The same descendant event should wake one profile/name only once."""
    db_path = tmp_path / "profile-subtree-dedupe.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root_a = kb.create_task(conn, title="root a", assignee="lead")
        root_b = kb.create_task(conn, title="root b", assignee="lead")
        child_id = kb.create_task(conn, title="shared child", assignee="worker")
        kb.link_tasks(conn, root_a, child_id)
        kb.link_tasks(conn, root_b, child_id)
        for root_id in (root_a, root_b):
            kb.add_profile_event_sub(
                conn,
                task_id=root_id,
                profile="default",
                name="jensen-orchestrator",
                event_kinds=["completed"],
                include_children=True,
            )
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "shared done"},
        )
    finally:
        conn.close()

    wake_calls: list[tuple[str, list[str]]] = []

    def fake_wake(self, sub, events, task, event_tasks, board):
        wake_calls.append((sub["task_id"], [e.task_id for e in events]))
        return True

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(wake_calls) == 1
    assert wake_calls[0][1] == [child_id]

    conn = kb.connect()
    try:
        claims = conn.execute(
            "SELECT event_id, profile, name, root_task_id "
            "FROM kanban_profile_event_claims"
        ).fetchall()
        assert len(claims) == 1
        assert claims[0]["profile"] == "default"
        assert claims[0]["name"] == "jensen-orchestrator"
        subs = kb.list_profile_event_subs(conn, profile="default")
        assert len(subs) == 2
        assert all(int(sub["last_event_id"] or 0) > 0 for sub in subs)
    finally:
        conn.close()


def test_profile_event_wake_failure_rewinds_cursor(tmp_path, monkeypatch):
    """Spawn failure must rewind the cursor so the next tick retries."""
    db_path = tmp_path / "profile-rewind.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry target", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    def fake_wake_fail(self, sub, events, task, event_tasks, board):
        return False

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake_fail,
    )

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        subs = kb.list_profile_event_subs(conn, task_id=tid)
        assert len(subs) == 1
        # Cursor rewound to 0; last_wake_at untouched, and the transient
        # event claim was released so a later notifier tick can retry.
        assert int(subs[0]["last_event_id"]) == 0
        assert subs[0]["last_wake_at"] in (None, 0)
        claims = conn.execute(
            "SELECT COUNT(*) FROM kanban_profile_event_claims"
        ).fetchone()[0]
        assert claims == 0
    finally:
        conn.close()

    wake_calls: list[int] = []

    def fake_wake_success(self, sub, events, task, event_tasks, board):
        wake_calls.append(len(events))
        return True

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake_success,
    )
    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert wake_calls == [1]


def test_profile_event_wake_command_and_env_via_popen(tmp_path, monkeypatch):
    """The real wake path constructs the expected argv + env via subprocess.Popen."""
    db_path = tmp_path / "profile-popen.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="popen target", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="default",  # 'default' avoids the profile-dir existence check
            event_kinds=["claimed"],
            wake_prompt="please orchestrate",
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    popen_calls: list[dict] = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append({"cmd": cmd, "kwargs": kwargs})

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(popen_calls) == 1, (
        f"Expected exactly one Popen for the wake spawn; got {len(popen_calls)}"
    )
    call = popen_calls[0]
    cmd = call["cmd"]
    # Profile, accept-hooks, and the chat -q invocation must all appear.
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "default"
    assert "--accept-hooks" in cmd
    assert "chat" in cmd and "-q" in cmd
    prompt_arg = cmd[cmd.index("-q") + 1]
    assert "Kanban event wake" in prompt_arg
    assert tid in prompt_arg
    assert "please orchestrate" in prompt_arg

    env = call["kwargs"]["env"]
    assert env.get("HERMES_KANBAN_EVENT_WAKE") == "1"
    assert env.get("HERMES_PROFILE") == "default"
    assert env.get("HERMES_KANBAN_BOARD")
    assert env.get("HERMES_KANBAN_DB")

    # Cursor advanced + last_wake_at stamped on Popen success.
    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["last_event_id"]) > 0
        assert int(sub["last_wake_at"] or 0) > 0
    finally:
        conn.close()


def test_profile_event_wake_disabled_via_wake_agent_zero(tmp_path, monkeypatch):
    """wake_agent=0 advances the cursor but never spawns."""
    db_path = tmp_path / "profile-no-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no-wake", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
            wake_agent=False,
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    wake_calls: list = []

    def fake_wake(self, sub, events, task, event_tasks, board):
        wake_calls.append(sub)
        return True

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert wake_calls == [], (
        "wake_agent=0 subscriptions must not spawn a wake"
    )
    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["last_event_id"]) > 0  # cursor still advances
        assert sub["last_wake_at"] in (None, 0)
    finally:
        conn.close()


def test_chat_subscription_unaffected_when_profile_sub_present(tmp_path, monkeypatch):
    """Adding a profile sub does not change chat-subscription delivery."""
    db_path = tmp_path / "mixed-subs.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="mixed", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.add_profile_event_sub(
            conn, task_id=tid, profile="jensen", event_kinds=["completed"],
        )
        kb.complete_task(conn, tid, summary="ok")
    finally:
        conn.close()

    wake_calls: list = []

    def fake_wake(self, sub, events, task, event_tasks, board):
        wake_calls.append(sub)
        return True

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Chat sub still delivers the terminal event.
    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()
    # Profile sub also fires for the same event.
    assert len(wake_calls) == 1
    assert wake_calls[0]["profile"] == "jensen"



# ---------------------------------------------------------------------------
# Phase 3: profile-wake health tracking + wake-event audit log
# ---------------------------------------------------------------------------
#
# These tests pin the gateway → kanban_db wiring for ``record_profile_wake_*``:
# success clears the error state and appends a ``status='success'`` wake-event
# row, failure increments ``wake_failure_count``, stamps the sanitized error,
# preserves the existing cursor-rewind behavior, and appends/coalesces
# ``status='failed'`` wake-event rows.


def test_profile_wake_success_records_health_and_wake_event(tmp_path, monkeypatch):
    db_path = tmp_path / "profile-health-success.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="health success", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        # Seed a "previously failed" state so the success path must clear it.
        kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=0,
            old_cursor=0,
            error=RuntimeError("stale"),
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    def fake_wake(self, sub, events, task, event_tasks, board):
        return True, None

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["last_event_id"]) > 0
        assert int(sub["last_wake_at"] or 0) > 0
        # Success path must clear error fields + reset failure counter.
        assert sub["last_wake_error"] in (None, "")
        assert sub["last_wake_error_at"] in (None, 0)
        assert int(sub["wake_failure_count"] or 0) == 0
        # One pre-seeded failed row + one success row from this tick.
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        statuses = [r["status"] for r in rows]
        assert "success" in statuses, f"expected a success row; got {statuses}"
        assert "failed" in statuses, f"expected pre-seeded failed row; got {statuses}"
    finally:
        conn.close()


def test_profile_wake_failure_records_health_and_preserves_rewind(tmp_path, monkeypatch):
    db_path = tmp_path / "profile-health-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="health failure", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    def fake_wake_fail(self, sub, events, task, event_tasks, board):
        return False, "BoomError: spawn refused"

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake_fail)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        # Cursor rewound to 0 → next tick retries (preserve Phase 2 behavior).
        assert int(sub["last_event_id"]) == 0
        assert sub["last_wake_at"] in (None, 0)
        assert int(sub["wake_failure_count"] or 0) == 1
        assert sub["last_wake_error"] and "BoomError" in sub["last_wake_error"]
        assert int(sub["last_wake_error_at"] or 0) > 0
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] and "BoomError" in rows[0]["error"]
        assert int(rows[0]["claimed_event_cursor"]) > 0
    finally:
        conn.close()


def test_profile_wake_failure_rows_are_throttled_but_health_updates(tmp_path, monkeypatch):
    db_path = tmp_path / "profile-health-failure-throttle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="health failure throttle", assignee="worker")
        kb.add_profile_event_sub(conn, task_id=tid, profile="jensen")
        first_id = kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=10,
            old_cursor=0,
            error=RuntimeError("first"),
            at=1_000,
        )
        second_id = kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=11,
            old_cursor=0,
            error=RuntimeError("second"),
            at=1_010,
        )
        assert second_id == first_id
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["wake_failure_count"] or 0) == 2
        assert "second" in sub["last_wake_error"]
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        assert len(rows) == 1

        third_id = kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=12,
            old_cursor=0,
            error=RuntimeError("third"),
            at=1_061,
        )
        assert third_id != first_id
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        assert len(rows) == 2
    finally:
        conn.close()


def test_profile_wake_missing_profile_returns_false_and_does_not_popen(tmp_path, monkeypatch):
    """Missing profiles must be treated as spawn failure before cursor ack."""
    db_path = tmp_path / "missing-profile-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="missing profile wake", assignee="worker")
        task = kb.get_task(conn, tid)
        ev = kb.list_events(conn, tid)[0]
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    sub = {"task_id": tid, "profile": "does-not-exist", "name": ""}
    with pytest.MonkeyPatch.context() as mp:
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise AssertionError("Popen should not be reached for missing profile")
        mp.setattr("subprocess.Popen", fake_popen)
        result = runner._kanban_profile_wake(sub, [ev], task, {}, "default")

    # Phase 3: ``_kanban_profile_wake`` returns ``(ok, error_text)`` so the
    # caller can stamp the sanitized reason onto the wake-events row.
    ok, err = result if isinstance(result, tuple) else (result, None)
    assert ok is False
    assert err and "does-not-exist" in err
    assert popen_calls == []
