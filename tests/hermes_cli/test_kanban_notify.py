import asyncio
import sqlite3
import pytest

from pathlib import Path
from types import SimpleNamespace
from hermes_cli import kanban_db as kb
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Allow the kanban notifier path-validator to upload artifacts the
    # tests write under ``tmp_path``. Without this, every artifact-delivery
    # test silently drops files because ``tmp_path`` isn't inside the
    # default ``MEDIA_DELIVERY_SAFE_ROOTS`` cache dirs.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    return home


@pytest.mark.asyncio
async def test_notifier_unsubs_after_completed_event(kanban_home):
    """
    Subscription should be remove after completed event
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="test task", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
        kb.complete_task(conn, tid, result="completed by agent")
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    fake_adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    fake_adapter.send.assert_called_once()
    call_msg = fake_adapter.send.call_args[0][1]
    assert "completed" in call_msg

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert subs == [], "Subscription should be unsub after completed event"


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ["gave_up", "crashed", "timed_out"])
async def test_notifier_unsubs_after_abnormal_events(kind, kanban_home):
    """
    Event kinds gave_up / crashed / timed_out send a notification but DO
    NOT delete the subscription. The dispatcher may respawn the task and
    fire the same event kind again (e.g. a worker that crashes, gets
    reclaimed, and crashes a second time); the user must hear about the
    second event too. Subscriptions are removed only when the task hits
    a truly final status (done / archived) — see the comment on
    TERMINAL_KINDS in gateway/run.py and PR #21398.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()

    try:
        tid = kb.create_task(conn, title=f"test {kind} task", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
        kb._append_event(conn, tid, kind=kind)
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    fake_adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # The user is notified about the abnormal event...
    fake_adapter.send.assert_called_once()
    assert kind.replace('_', ' ') in fake_adapter.send.call_args[0][1]

    # ...but the subscription survives so a respawn-then-same-event cycle
    # reaches the user too. The cursor (last_event_id) advanced inside
    # the same write txn as the claim, so the same event won't re-fire.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert len(subs) == 1, (
        f"Subscription should survive {kind!r} so the next cycle of the "
        f"same event reaches the user; got {subs!r}"
    )
    assert int(subs[0]["last_event_id"]) >= 1, (
        "Cursor should have advanced past the delivered event "
        "(claim_unseen_events_for_sub advances atomically inside the "
        "same write txn as the read)."
    )


@pytest.mark.asyncio
async def test_notifier_second_blocked_delivers(kanban_home):
    """
    After the first blocked, should receive second blocked notification.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    delivered_msgs: list[str] = []

    async def _capture_send(chat_id, msg, metadata=None):
        delivered_msgs.append(msg)

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(side_effect=_capture_send)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(_):
        nonlocal tick_count
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 6:
            runner._running = False

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="test task", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")

        # Cycle 1: blocked
        kb.block_task(conn, tid, reason="first block")
    finally:
        conn.close()

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # Cycle 2: unblock → block run again
    runner._running = True
    tick_count = 0

    conn = kb.connect()
    try:
        kb.unblock_task(conn, tid)
        kb.block_task(conn, tid, reason="second block")
    finally:
        conn.close()

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    blocked_deliveries = [m for m in delivered_msgs if "blocked" in m]
    assert "second block" not in blocked_deliveries[0]
    assert "second block" in blocked_deliveries[1]
    assert len(blocked_deliveries) == 2, (
        f"Should receive 2 blocked notification, but only get {len(blocked_deliveries)} count\n"
        f"Message {delivered_msgs}"
    )


# ---------------------------------------------------------------------------
# Regression: gateway watchers must not double-init the kanban DB.
#
# Both the notifier watcher (`_kanban_notifier_watcher`) and the dispatcher
# tick (`_tick_once_for_board`) used to call `_kb.connect(board=slug)`
# immediately followed by `_kb.init_db(board=slug)`. Since `connect()`
# already runs the schema + idempotent migration on first open per process,
# the explicit `init_db()` was redundant — and worse, `init_db()`
# deliberately busts the per-process cache and re-runs the migration on a
# *second* connection, which races the first.  On legacy DBs this surfaced
# as `duplicate column name: <col>` (now tolerated by
# `_add_column_if_missing`) and intermittent `database is locked` errors
# (issue #21378).
#
# The fix removes the `init_db()` calls in both watchers; this regression
# test pins that behaviour so we don't reintroduce them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifier_does_not_call_init_db(kanban_home):
    """Notifier watcher path must not invoke `_kb.init_db` (issue #21378)."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(_):
        nonlocal tick_count
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 3:
            runner._running = False

    init_db_calls: list[object] = []
    real_init_db = kb.init_db

    def _spy_init_db(*args, **kwargs):
        init_db_calls.append((args, kwargs))
        return real_init_db(*args, **kwargs)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep), \
         patch("hermes_cli.kanban_db.init_db", side_effect=_spy_init_db):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    assert init_db_calls == [], (
        "_kanban_notifier_watcher must not call init_db on every tick — "
        "connect() handles first-run schema init. "
        "Reintroducing init_db revives issue #21378. "
        f"Got {len(init_db_calls)} call(s): {init_db_calls}"
    )


def test_dispatcher_tick_does_not_call_init_db(kanban_home, monkeypatch):
    """`_tick_once_for_board` must not invoke `_kb.init_db` (issue #21378).

    `connect()` already runs the schema + idempotent migration on first open
    per process. The explicit `init_db()` call was redundant and triggered a
    second migration on a second connection that raced the first.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from unittest.mock import patch

    runner = object.__new__(GatewayRunner)

    init_db_calls: list[object] = []
    real_init_db = kb.init_db

    def _spy_init_db(*args, **kwargs):
        init_db_calls.append((args, kwargs))
        return real_init_db(*args, **kwargs)

    # The dispatcher watcher's tick lives as a local closure inside
    # `_kanban_dispatcher_watcher`. Read the source and assert the
    # specific patterns that would reintroduce the bug are absent.
    import inspect
    src = inspect.getsource(GatewayRunner._kanban_dispatcher_watcher)
    assert "_kb.init_db(board=slug)" not in src, (
        "_kanban_dispatcher_watcher must not call _kb.init_db(board=slug) — "
        "see issue #21378. Use connect() alone; it runs migrations on first "
        "open per process."
    )

    notifier_src = inspect.getsource(GatewayRunner._kanban_notifier_watcher)
    assert "_kb.init_db(board=slug)" not in notifier_src, (
        "_kanban_notifier_watcher must not call _kb.init_db(board=slug) — "
        "see issue #21378."
    )


@pytest.mark.asyncio
async def test_dispatcher_quarantines_board_on_disk_io_error(
    kanban_home, monkeypatch, caplog,
):
    """A disk-I/O OperationalError should disable that board for this process."""
    from gateway.run import GatewayRunner
    import hermes_cli.config as cli_config

    runner = object.__new__(GatewayRunner)
    runner._running = True

    shared_db = kb.kanban_db_path().resolve()
    board_rows = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]

    real_connect = kb.connect
    write_connect_calls: list[str] = []

    def fake_list_boards(include_archived=False):
        return board_rows.pop(0) if board_rows else []

    def fake_connect(*, board=None, readonly=False, **kwargs):
        if readonly:
            return real_connect(board=board, readonly=True)
        write_connect_calls.append(board or "")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        cli_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )

    real_sleep = asyncio.sleep
    sleep_calls = {"count": 0}

    async def fake_sleep(delay):
        await real_sleep(0)
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 3:
            runner._running = False

    with patch("gateway.run.asyncio.sleep", side_effect=fake_sleep):
        await asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10.0)

    # First tick attempts one writable open and quarantines the board.
    # Second tick should skip `_tick_once_for_board` for that slug.
    assert write_connect_calls == ["alias-a"]
    assert "filesystem-level fault" in caplog.text
    assert "disabled for this board DB until gateway restart" in caplog.text


@pytest.mark.asyncio
async def test_dispatcher_quarantines_shared_db_across_aliases(
    kanban_home, monkeypatch, caplog,
):
    """A disk-I/O fault on alias-a should quarantine alias-b for the same DB path."""
    from gateway.run import GatewayRunner
    import hermes_cli.config as cli_config

    runner = object.__new__(GatewayRunner)
    runner._running = True

    shared_db = kb.kanban_db_path().resolve()
    boards = [
        {"slug": "alias-a", "db_path": str(shared_db)},
        {"slug": "alias-b", "db_path": str(shared_db)},
    ]
    board_rows = [list(boards), list(boards), list(boards)]

    real_connect = kb.connect
    write_connect_calls: list[str] = []
    readonly_connect_calls: list[str] = []

    def fake_list_boards(include_archived=False):
        return board_rows.pop(0) if board_rows else []

    def fake_connect(*, board=None, readonly=False, **kwargs):
        if readonly:
            readonly_connect_calls.append(board or "")
            return real_connect(board=board, readonly=True)
        write_connect_calls.append(board or "")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        cli_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )

    real_sleep = asyncio.sleep
    sleep_calls = {"count": 0}

    async def fake_sleep(delay):
        await real_sleep(0)
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 3:
            runner._running = False

    with patch("gateway.run.asyncio.sleep", side_effect=fake_sleep):
        await asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10.0)

    # alias-b must not attempt to reopen the same unhealthy DB.
    assert write_connect_calls == ["alias-a"]
    # `_ready_nonempty` runs after `_tick_once` in the same loop; once alias-a
    # quarantines this resolved DB path, readonly probes must skip both aliases.
    assert readonly_connect_calls == []
    assert "filesystem-level fault" in caplog.text


@pytest.mark.asyncio
async def test_dispatcher_ready_probe_uses_readonly_connect(kanban_home, monkeypatch):
    """`_ready_nonempty` should use readonly DB opens for spawnability probes."""
    from gateway.run import GatewayRunner
    import hermes_cli.config as cli_config

    runner = object.__new__(GatewayRunner)
    runner._running = True

    shared_db = kb.kanban_db_path().resolve()
    board_rows = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]

    real_connect = kb.connect
    connect_calls: list[tuple[str, bool]] = []

    def fake_list_boards(include_archived=False):
        return board_rows.pop(0) if board_rows else []

    def fake_connect(*, board=None, readonly=False, **kwargs):
        connect_calls.append((board or "", bool(readonly)))
        return real_connect(board=board, readonly=readonly)

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "dispatch_once", lambda *args, **kwargs: SimpleNamespace(summary=lambda: {}))
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda _conn: False)
    monkeypatch.setattr(
        cli_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )

    real_sleep = asyncio.sleep
    sleep_calls = {"count": 0}

    async def fake_sleep(delay):
        await real_sleep(0)
        sleep_calls["count"] += 1
        if sleep_calls["count"] >= 2:
            runner._running = False

    with patch("gateway.run.asyncio.sleep", side_effect=fake_sleep):
        await asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10.0)

    # There should be at least one readonly open from `_ready_nonempty`.
    assert ("alias-a", True) in connect_calls


@pytest.mark.asyncio
async def test_notifier_skips_subscription_owned_by_other_profile(kanban_home):
    """Each gateway keeps its watcher on, but only the subscribing profile claims."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned task", assignee="backend-engineer")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat1",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, result="done")
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "business-partner"

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep
    tick_count = 0

    async def _fast_sleep(_):
        nonlocal tick_count
        await _orig_sleep(0)
        tick_count += 1
        if tick_count >= 3:
            runner._running = False

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    fake_adapter.send.assert_not_called()
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert len(subs) == 1
    assert int(subs[0]["last_event_id"]) == 0, "wrong profile must not claim the event"


@pytest.mark.asyncio
async def test_notifier_delivers_subscription_owned_by_current_profile(kanban_home):
    """The gateway for the profile that created/subscribed the task reports it."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned task", assignee="backend-engineer")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat1",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, result="done")
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "default"

    fake_adapter = MagicMock()

    async def _send_and_stop(chat_id, msg, metadata=None):
        runner._running = False

    fake_adapter.send = AsyncMock(side_effect=_send_and_stop)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    fake_adapter.send.assert_called_once()
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert subs == []


@pytest.mark.asyncio
async def test_gateway_create_autosubscribes_on_explicit_board(kanban_home):
    """`/kanban --board <slug> create ...` must subscribe on that board.

    The gateway handler currently auto-subscribes after `/kanban create`,
    but the create detection must still work when the shared `--board`
    flag appears before the subcommand, and the subscription must land in
    that board's DB rather than the ambient/default board.
    """
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    kb.create_board("projx")

    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat1",
        thread_id="th1",
        user_id="u1",
    )
    event = SimpleNamespace(
        text='/kanban --board projx create "hello" --assignee alice',
        source=source,
    )

    out = await GatewayRunner._handle_kanban_command(runner, event)

    assert "subscribed" in out.lower()

    conn = kb.connect(board="projx")
    try:
        subs = kb.list_notify_subs(conn)
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()

    assert [t.title for t in tasks] == ["hello"]
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "th1"

    conn = kb.connect(board="default")
    try:
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_notifier_uploads_artifacts_on_completion(kanban_home, tmp_path, monkeypatch):
    """When a completed event carries ``artifacts`` in its payload, the
    notifier uploads each file to the subscribed chat as a native
    attachment. Images batch through send_multiple_images; documents
    route through send_document. See the artifacts wiring in
    gateway/run.py._deliver_kanban_artifacts.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from tools import kanban_tools as kt

    # ``_deliver_kanban_artifacts`` routes candidates through
    # ``BasePlatformAdapter.filter_local_delivery_paths``, which only accepts
    # paths under ``MEDIA_DELIVERY_SAFE_ROOTS`` or roots explicitly allowlisted
    # via ``HERMES_MEDIA_ALLOW_DIRS``. Test fixtures live under ``tmp_path``,
    # so allowlist it for the duration of the test.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    # Materialize real files so os.path.isfile passes inside the helper.
    chart_path = tmp_path / "q3-revenue.png"
    chart_path.write_bytes(b"PNG-fake-bytes")
    report_path = tmp_path / "report.pdf"
    report_path.write_bytes(b"%PDF-fake")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="render q3 chart", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
    finally:
        conn.close()

    # Use the production handler so we exercise the full path: tool args
    # → metadata.artifacts → event payload promotion.
    import os
    os.environ["HERMES_KANBAN_TASK"] = tid
    try:
        out = kt._handle_complete({
            "summary": "rendered the chart",
            "artifacts": [str(chart_path), str(report_path)],
        })
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
    import json as _json
    assert _json.loads(out)["ok"] is True

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    sends: list = []
    images_uploaded: list = []
    documents_uploaded: list = []

    async def _send(chat_id, msg, metadata=None):
        sends.append((chat_id, msg))
        runner._running = False

    async def _send_images(chat_id, images, metadata=None, **_kw):
        images_uploaded.extend(p for p, _ in images)

    async def _send_document(chat_id, file_path, metadata=None, **_kw):
        documents_uploaded.append(file_path)

    fake_adapter.send = AsyncMock(side_effect=_send)
    fake_adapter.send_multiple_images = AsyncMock(side_effect=_send_images)
    fake_adapter.send_document = AsyncMock(side_effect=_send_document)
    # extract_local_files is used internally for legacy path fallback;
    # the real BasePlatformAdapter implementation lives there, so wire it.
    from gateway.platforms.base import BasePlatformAdapter
    fake_adapter.extract_local_files = BasePlatformAdapter.extract_local_files

    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # The text completion notification fired.
    assert len(sends) == 1
    # The PNG rode the image-batch path.
    assert any("q3-revenue.png" in p for p in images_uploaded), images_uploaded
    # The PDF rode the document path.
    assert any("report.pdf" in p for p in documents_uploaded), documents_uploaded


@pytest.mark.asyncio
async def test_notifier_artifact_delivery_skips_missing_files(kanban_home, tmp_path, monkeypatch):
    """Missing artifact paths are silently skipped — they may have been
    referenced by name only. The notifier must not crash and must still
    deliver any artifacts that do exist."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from tools import kanban_tools as kt

    # Allow ``tmp_path`` through the media-delivery safety filter. See the
    # companion test for the full explanation.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    real_pdf = tmp_path / "real.pdf"
    real_pdf.write_bytes(b"%PDF-fake")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
    finally:
        conn.close()

    import os
    os.environ["HERMES_KANBAN_TASK"] = tid
    try:
        kt._handle_complete({
            "summary": "one real, one ghost",
            "artifacts": [str(real_pdf), "/tmp/definitely-does-not-exist.pdf"],
        })
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    documents_uploaded: list = []

    async def _send(chat_id, msg, metadata=None):
        runner._running = False

    async def _send_document(chat_id, file_path, metadata=None, **_kw):
        documents_uploaded.append(file_path)

    fake_adapter.send = AsyncMock(side_effect=_send)
    fake_adapter.send_document = AsyncMock(side_effect=_send_document)
    fake_adapter.send_multiple_images = AsyncMock()
    from gateway.platforms.base import BasePlatformAdapter
    fake_adapter.extract_local_files = BasePlatformAdapter.extract_local_files

    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # Only the real file was uploaded.
    assert len(documents_uploaded) == 1
    assert "real.pdf" in documents_uploaded[0]


# ---------------------------------------------------------------------------
# Phase 2: profile-level Kanban event subscriptions (DB-layer behavior).
# These exercise the helpers directly; gateway-side wake spawning is covered
# in tests/gateway/test_kanban_notifier.py.
# ---------------------------------------------------------------------------


def test_profile_event_sub_claim_custom_kind_advances_cursor(kanban_home):
    """A profile sub with an explicit kind claims that event and advances."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile-claim", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")

        old_c, new_c, events = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert old_c == 0
        assert new_c > 0
        assert [e.kind for e in events] == ["claimed"]

        # Cursor has been atomically advanced inside the claim txn.
        subs = kb.list_profile_event_subs(conn, task_id=tid)
        assert len(subs) == 1
        assert int(subs[0]["last_event_id"]) == new_c

        # A second claim with no new events is a no-op.
        old2, new2, ev2 = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert ev2 == []
        assert old2 == new_c
        assert new2 == new_c
    finally:
        conn.close()


def test_profile_event_sub_include_children_claims_descendant(kanban_home):
    """include_children fans dependency-descendant events up to the parent sub."""
    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="root", assignee="lead")
        child_id = kb.create_task(conn, title="leaf", assignee="worker")
        kb.link_tasks(conn, parent_id, child_id)
        kb.add_profile_event_sub(
            conn,
            task_id=parent_id,
            profile="jensen",
            include_children=True,
        )
        # Child completes — default kinds include 'completed'.
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "child done"},
        )

        _, new_c, events = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=parent_id, profile="jensen",
        )
        assert new_c > 0
        assert any(
            e.task_id == child_id and e.kind == "completed" for e in events
        ), [(e.task_id, e.kind) for e in events]
    finally:
        conn.close()


def test_profile_event_sub_rewind_restores_cursor(kanban_home):
    """Rewind after claim leaves the event reclaimable on next tick."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="rewind", assignee="worker")
        kb.add_profile_event_sub(
            conn, task_id=tid, profile="jensen", event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")

        old_c, new_c, events = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert events  # sanity: we did claim something

        ok = kb.rewind_profile_event_cursor(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=new_c,
            old_cursor=old_c,
        )
        assert ok

        # Re-claim sees the event again.
        _, _, events2 = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert [e.kind for e in events2] == ["claimed"]
    finally:
        conn.close()


def test_profile_event_sub_disabled_returns_empty(kanban_home):
    """enabled=0 short-circuits the claim path; events are not consumed."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="disabled", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
            enabled=False,
        )
        kb._append_event(conn, tid, kind="claimed")

        old_c, new_c, events = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert events == []
        assert old_c == new_c == 0

        # enabled_only=True (default) hides this sub from listing.
        assert kb.list_profile_event_subs(conn, task_id=tid) == []
        # enabled_only=False surfaces it.
        all_subs = kb.list_profile_event_subs(
            conn, task_id=tid, enabled_only=False,
        )
        assert len(all_subs) == 1
        assert int(all_subs[0]["enabled"]) == 0
    finally:
        conn.close()


def test_profile_event_sub_remove_returns_existence(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="remove", assignee="worker")
        kb.add_profile_event_sub(
            conn, task_id=tid, profile="jensen", name="lane-a",
        )
        assert kb.remove_profile_event_sub(
            conn, task_id=tid, profile="jensen", name="lane-a",
        )
        assert not kb.remove_profile_event_sub(
            conn, task_id=tid, profile="jensen", name="lane-a",
        )
    finally:
        conn.close()


def test_profile_event_sub_legacy_db_migrates_table(tmp_path, monkeypatch):
    """Legacy DBs without ``kanban_profile_event_subs`` get the table on init."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    # Hand-craft a pre-Phase-2 DB by initializing then dropping the new table.
    kb.init_db()
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DROP TABLE IF EXISTS kanban_profile_event_subs")
        raw.commit()
    finally:
        raw.close()

    # Bust the per-process init cache so the next connect() re-runs migrations.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()

    conn = kb.connect()
    try:
        # Smoke-test by inserting + listing — fails loudly if table is missing.
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        kb.add_profile_event_sub(
            conn, task_id=tid, profile="jensen", event_kinds=["claimed"],
        )
        assert len(kb.list_profile_event_subs(conn, task_id=tid)) == 1
    finally:
        conn.close()



def test_profile_event_sub_readd_preserves_existing_options(kanban_home):
    """Re-adding the same profile sub without options is an idempotent ensure."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile preserve", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
            include_children=True,
            wake_agent=False,
            wake_prompt="custom hint",
            enabled=False,
        )
        kb.add_profile_event_sub(conn, task_id=tid, profile="jensen")
        sub = kb.list_profile_event_subs(
            conn, task_id=tid, profile="jensen", enabled_only=False,
        )[0]
    finally:
        conn.close()

    assert sub["event_kinds"] == '["claimed"]'
    assert int(sub["include_children"]) == 1
    assert int(sub["wake_agent"]) == 0
    assert sub["wake_prompt"] == "custom hint"
    assert int(sub["enabled"]) == 0


def test_profile_event_sub_readd_can_update_explicit_fields(kanban_home):
    """Explicit optional args still update the profile sub in place."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile update", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
            include_children=True,
            wake_agent=False,
            wake_prompt="old hint",
            enabled=False,
        )
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["completed"],
            include_children=False,
            wake_agent=True,
            wake_prompt=None,
            enabled=True,
        )
        sub = kb.list_profile_event_subs(
            conn, task_id=tid, profile="jensen", enabled_only=False,
        )[0]
    finally:
        conn.close()

    assert sub["event_kinds"] == '["completed"]'
    assert int(sub["include_children"]) == 0
    assert int(sub["wake_agent"]) == 1
    assert sub["wake_prompt"] is None
    assert int(sub["enabled"]) == 1



def test_profile_event_late_success_ack_does_not_move_cursor_backwards(kanban_home):
    """A late successful wake ack must not clobber a newer claim cursor."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile cursor monotonic", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["created", "claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
        old1, cur1, events1 = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert old1 == 0
        assert len(events1) == 2
        kb._append_event(conn, tid, kind="claimed")
        old2, cur2, events2 = kb.claim_unseen_events_for_profile_sub(
            conn, task_id=tid, profile="jensen",
        )
        assert old2 == cur1
        assert len(events2) == 1
        assert cur2 > cur1

        # Simulate the first wake's success callback arriving after the second
        # claim already advanced the cursor. It may stamp last_wake_at but must
        # not move last_event_id backwards.
        kb.advance_profile_event_cursor(
            conn,
            task_id=tid,
            profile="jensen",
            new_cursor=cur1,
            last_wake_at=123,
        )
        sub = kb.list_profile_event_subs(conn, task_id=tid, profile="jensen")[0]
    finally:
        conn.close()

    assert int(sub["last_event_id"]) == cur2
    assert int(sub["last_wake_at"]) == 123


def test_notify_late_success_ack_does_not_move_cursor_backwards(kanban_home):
    """Chat notify cursor advance is also monotonic under late acks."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="chat cursor monotonic", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            event_kinds=["created", "claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
        old1, cur1, events1 = kb.claim_unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["created", "claimed"],
        )
        assert old1 == 0
        assert len(events1) == 2
        kb._append_event(conn, tid, kind="claimed")
        old2, cur2, events2 = kb.claim_unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["created", "claimed"],
        )
        assert old2 == cur1
        assert len(events2) == 1
        assert cur2 > cur1

        kb.advance_notify_cursor(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            new_cursor=cur1,
        )
        sub = kb.list_notify_subs(conn, tid)[0]
    finally:
        conn.close()

    assert int(sub["last_event_id"]) == cur2
