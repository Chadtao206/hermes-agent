"""CLI live-session publication for the dashboard Control Center."""

import queue
import types
from datetime import datetime


def _make_cli():
    from cli import HermesCLI

    obj = HermesCLI.__new__(HermesCLI)
    obj.session_id = "cli-session-001"
    obj.session_start = datetime(2026, 1, 2, 3, 4, 5)
    obj._session_db = None
    obj.agent = types.SimpleNamespace(model="test-model")
    obj.model = "fallback-model"
    obj._agent_running = False
    obj._pending_input = queue.Queue()
    obj._last_control_center_publish = 0.0
    obj._should_exit = False
    return obj


def _live_rows():
    import control_center_store as cc

    db = cc.ControlCenterDB()
    try:
        return db.list_live_sessions(running_only=False, limit=20)
    finally:
        db.close()


def test_cli_publishes_live_session_row():
    cli_obj = _make_cli()

    cli_obj._publish_control_center_session(
        running=True,
        awaiting_input=True,
        payload={"extra": "value"},
        force=True,
    )

    rows = {row["session_id"]: row for row in _live_rows()}
    row = rows["cli-session-001"]
    assert row["owner_kind"] == "cli"
    assert row["owner_id"]
    assert row["source"] == "cli"
    assert row["model"] == "test-model"
    assert row["running"] == 1
    assert row["awaiting_input"] == 1
    assert row["payload"]["pid"]
    assert row["payload"]["extra"] == "value"


def test_cli_marks_control_center_session_closed():
    cli_obj = _make_cli()
    cli_obj._publish_control_center_session(running=True, awaiting_input=True, force=True)

    cli_obj._mark_control_center_session_closed()

    rows = {row["session_id"]: row for row in _live_rows()}
    row = rows["cli-session-001"]
    assert row["running"] == 0
    assert row["awaiting_input"] == 0
    assert row["payload"]["closed"] is True


def test_cli_processes_submit_command_as_next_input():
    import control_center_store as cc

    cli_obj = _make_cli()
    cli_obj._publish_control_center_session(running=True, awaiting_input=True, force=True)
    command_id = cc.cc_enqueue_command(
        "submit",
        target_session_id="cli-session-001",
        payload={"text": "hello from dashboard"},
    )

    assert cli_obj._process_control_center_command() is True
    assert cli_obj._pending_input.get_nowait() == "hello from dashboard"
    command = next(item for item in cc.read_commands(limit=10) if item["id"] == command_id)
    assert command["status"] == "completed"
    assert command["result"]["status"] == "queued"


def test_cli_processes_steer_command_inline_when_agent_running():
    import control_center_store as cc

    seen = []
    cli_obj = _make_cli()
    cli_obj._agent_running = True
    cli_obj.agent = types.SimpleNamespace(
        model="test-model",
        steer=lambda text: seen.append(text) or True,
    )
    cli_obj._publish_control_center_session(running=True, awaiting_input=False, force=True)
    command_id = cc.cc_enqueue_command(
        "steer",
        target_session_id="cli-session-001",
        payload={"text": "adjust course"},
    )

    assert cli_obj._process_control_center_command() is True
    assert seen == ["adjust course"]
    command = next(item for item in cc.read_commands(limit=10) if item["id"] == command_id)
    assert command["status"] == "completed"
    assert command["result"] == {"status": "steer_queued", "accepted": True}
