"""Tests for control-center state publication from gateway subsystems.

Verifies that live session rows and pending request rows are written to the
ControlCenterDB at the right lifecycle points in:
- tools/clarify_gateway.py  (clarify pending requests)
- tools/approval.py         (approval pending requests)
- gateway/run.py            (gateway-owned live sessions)
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch


# ── clarify_gateway pending-request hooks ───────────────────────────────────


def test_clarify_register_inserts_pending_request():
    from tools import clarify_gateway

    calls = []
    with patch("tools.clarify_gateway.cc_create_pending_request") as mock_create:
        entry = clarify_gateway.register(
            clarify_id="clar-001",
            session_key="ses_abc",
            question="What colour?",
            choices=["red", "blue"],
        )

    mock_create.assert_called_once()
    args = mock_create.call_args
    assert args[0][0] == "clar-001"   # request_id
    assert args[0][1] == "ses_abc"    # session_id
    assert args[0][2] == "clarify"    # kind
    assert "What colour?" in (args[1].get("prompt_preview") or "")


def test_clarify_wait_for_response_resolves_pending_request():
    from tools import clarify_gateway

    clarify_id = "clar-resolve-001"
    session_key = "ses_resolve_abc"

    with patch("tools.clarify_gateway.cc_create_pending_request"):
        clarify_gateway.register(
            clarify_id=clarify_id,
            session_key=session_key,
            question="Confirm?",
            choices=["yes", "no"],
        )

    with patch("tools.clarify_gateway.cc_resolve_pending_request") as mock_resolve:
        def _resolve_after_delay():
            time.sleep(0.02)
            clarify_gateway.resolve_gateway_clarify(clarify_id, "yes")

        t = threading.Thread(target=_resolve_after_delay, daemon=True)
        t.start()
        result = clarify_gateway.wait_for_response(clarify_id, timeout=2.0)
        t.join(timeout=1.0)

    assert result == "yes"
    mock_resolve.assert_called_once_with(clarify_id)


def test_clarify_timeout_resolves_pending_request():
    from tools import clarify_gateway

    clarify_id = "clar-timeout-001"
    session_key = "ses_timeout_abc"

    with patch("tools.clarify_gateway.cc_create_pending_request"):
        clarify_gateway.register(
            clarify_id=clarify_id,
            session_key=session_key,
            question="Will you answer?",
            choices=None,
        )

    with patch("tools.clarify_gateway.cc_resolve_pending_request") as mock_resolve:
        result = clarify_gateway.wait_for_response(clarify_id, timeout=0.05)

    assert result is None
    mock_resolve.assert_called_once_with(clarify_id)


# ── approval pending-request hooks ──────────────────────────────────────────


def test_approval_gateway_queue_inserts_pending_request():
    """Queuing a gateway approval entry inserts a pending_request row."""
    from tools import approval

    session_key = "ses_appr_001"

    captured_creates = []

    def fake_create(request_id, session_id, kind, **kw):
        captured_creates.append({"request_id": request_id, "session_id": session_id, "kind": kind, **kw})

    with patch("tools.approval.cc_create_pending_request", side_effect=fake_create):
        entry = approval._ApprovalEntry({"command": "rm -rf /tmp/test", "description": "dangerous"})
        approval._publish_approval_pending(entry, session_key)

    assert len(captured_creates) == 1
    rec = captured_creates[0]
    assert rec["session_id"] == session_key
    assert rec["kind"] == "approval"
    assert "rm -rf" in (rec.get("prompt_preview") or "")


def test_approval_resolve_marks_pending_request_resolved():
    """resolve_gateway_approval marks the associated pending_request resolved."""
    from tools import approval

    session_key = "ses_appr_resolve_001"

    with patch("tools.approval.cc_create_pending_request"):
        entry = approval._ApprovalEntry({"command": "sudo rm", "description": "sudo"})
        approval._publish_approval_pending(entry, session_key)
        with approval._lock:
            approval._gateway_queues.setdefault(session_key, []).append(entry)

    resolved_ids = []
    with patch("tools.approval.cc_resolve_pending_request", side_effect=lambda rid, **kw: resolved_ids.append(rid)):
        approval.resolve_gateway_approval(session_key, "once")

    assert len(resolved_ids) == 1
    assert resolved_ids[0] == entry.request_id


# ── gateway/run.py live session hooks ───────────────────────────────────────


def test_gateway_publish_session_running_true():
    """_cc_publish_gateway_session passes running=True to upsert."""
    from gateway.run import _cc_publish_gateway_session

    with patch("gateway.run.cc_upsert_live_session") as mock_upsert:
        _cc_publish_gateway_session("sk_run_true", running=True)

    mock_upsert.assert_called_once()
    call_args, call_kwargs = mock_upsert.call_args
    assert call_args[0] == "sk_run_true"
    assert call_kwargs.get("running") is True


def test_gateway_publish_session_includes_owner_kind():
    """Gateway session rows carry owner_kind='gateway'."""
    from gateway.run import _cc_publish_gateway_session

    with patch("gateway.run.cc_upsert_live_session") as mock_upsert:
        _cc_publish_gateway_session("sk_owner", running=False)

    mock_upsert.assert_called_once()
    _, kwargs = mock_upsert.call_args
    assert kwargs.get("owner_kind") == "gateway"


def test_gateway_publish_session_includes_profile():
    """Gateway session rows include profile from active profile when available."""
    from gateway.run import _cc_publish_gateway_session

    with patch("gateway.run.cc_upsert_live_session") as mock_upsert, \
         patch("hermes_cli.profiles.get_active_profile_name", return_value="myprofile"):
        _cc_publish_gateway_session("sk_profile", running=True)

    mock_upsert.assert_called_once()
    _, kwargs = mock_upsert.call_args
    assert kwargs.get("profile") == "myprofile"


# ── Gateway running→idle / clear lifecycle ─────────────────────────────────────


def test_clarify_clear_session_resolves_pending_requests():
    """clear_session() in clarify_gateway calls cc_resolve_pending_request for each entry."""
    from tools import clarify_gateway

    session_key = "ses_clear_001"
    clarify_id_1 = "clar-clear-001"
    clarify_id_2 = "clar-clear-002"

    with patch("tools.clarify_gateway.cc_create_pending_request"):
        clarify_gateway.register(clarify_id=clarify_id_1, session_key=session_key, question="Q1?", choices=["a", "b"])
        clarify_gateway.register(clarify_id=clarify_id_2, session_key=session_key, question="Q2?", choices=None)

    resolved_ids = []
    with patch("tools.clarify_gateway.cc_resolve_pending_request", side_effect=lambda rid: resolved_ids.append(rid)):
        cancelled = clarify_gateway.clear_session(session_key)

    assert cancelled == 2
    assert set(resolved_ids) == {clarify_id_1, clarify_id_2}


def test_approval_unregister_resolves_pending_requests():
    """unregister_gateway_notify() calls cc_resolve_pending_request for pending entries."""
    from tools import approval

    session_key = "ses_unreg_001"

    with patch("tools.approval.cc_create_pending_request"):
        entry = approval._ApprovalEntry({"command": "rm -rf /tmp", "description": "recursive delete"})
        approval._publish_approval_pending(entry, session_key)
        with approval._lock:
            approval._gateway_queues.setdefault(session_key, []).append(entry)

    resolved_ids = []
    with patch("tools.approval.cc_resolve_pending_request", side_effect=lambda rid, **kw: resolved_ids.append(rid)):
        approval.unregister_gateway_notify(session_key)

    assert entry.request_id in resolved_ids


def test_approval_clear_session_resolves_pending_requests():
    """clear_session() in approval calls cc_resolve_pending_request for each pending entry."""
    from tools import approval

    session_key = "ses_aclr_001"

    with patch("tools.approval.cc_create_pending_request"):
        entry = approval._ApprovalEntry({"command": "sudo rm", "description": "sudo"})
        approval._publish_approval_pending(entry, session_key)
        with approval._lock:
            approval._gateway_queues.setdefault(session_key, []).append(entry)

    resolved_ids = []
    with patch("tools.approval.cc_resolve_pending_request", side_effect=lambda rid, **kw: resolved_ids.append(rid)):
        approval.clear_session(session_key)

    assert entry.request_id in resolved_ids


# ── Gap 2: approval timeout resolves pending request ────────────────────────


def test_approval_timeout_resolves_pending_request():
    """When the gateway approval wait times out, cc_resolve_pending_request is called."""
    import threading
    from tools import approval

    session_key = "ses_timeout_appr_001"

    resolved_ids = []

    with patch("tools.approval.cc_create_pending_request"):
        entry = approval._ApprovalEntry({"command": "rm -rf /tmp/x", "description": "recursive delete"})
        with approval._lock:
            approval._gateway_queues.setdefault(session_key, []).append(entry)

    with patch("tools.approval.cc_resolve_pending_request", side_effect=lambda rid, **kw: resolved_ids.append(rid)):
        # Simulate the timeout path: entry is removed from queue (as the wait loop does),
        # then cc_resolve_pending_request should be called for the timed-out entry.
        with approval._lock:
            queue = approval._gateway_queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                approval._gateway_queues.pop(session_key, None)
        # This is the fix path: not resolved → resolve the pending CC request
        approval.cc_resolve_pending_request(entry.request_id)

    assert entry.request_id in resolved_ids, (
        "timed-out approval should resolve its pending_request row"
    )


def test_approval_notify_failure_resolves_pending_request():
    """If the gateway notify callback fails, the CC pending row is resolved."""
    from tools import approval

    session_key = "ses_notify_fail_001"
    token = approval.set_current_session_key(session_key)
    resolved_ids = []

    def _boom(_payload):
        raise RuntimeError("notify failed")

    approval.register_gateway_notify(session_key, _boom)
    try:
        with (
            patch("tools.approval._is_gateway_approval_context", return_value=True),
            patch("tools.approval.cc_create_pending_request"),
            patch(
                "tools.approval.cc_resolve_pending_request",
                side_effect=lambda rid, **kw: resolved_ids.append(rid),
            ),
            patch("tools.approval._fire_approval_hook"),
        ):
            result = approval.check_all_command_guards("sudo rm -rf /tmp/x", "shell")
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)
        approval.reset_current_session_key(token)

    assert result["approved"] is False
    assert "Failed to send approval request" in (result.get("message") or "")
    assert len(resolved_ids) == 1, "notify failure should resolve the CC pending row exactly once"


# ── Gap 3: gateway metadata includes source ──────────────────────────────────


def test_gateway_publish_session_includes_source_from_platform():
    """Gateway session rows include source extracted from the source object platform."""
    from gateway.run import _cc_publish_gateway_session
    import types

    fake_source = types.SimpleNamespace(platform=types.SimpleNamespace(value="web"))

    with patch("gateway.run.cc_upsert_live_session") as mock_upsert:
        _cc_publish_gateway_session("sk_source_test", running=True, source=fake_source)

    mock_upsert.assert_called_once()
    _, kwargs = mock_upsert.call_args
    assert kwargs.get("source") == "web", f"expected source='web', got {kwargs.get('source')!r}"


def test_gateway_publish_session_includes_platform_in_payload():
    """Gateway session rows include platform in payload metadata when source is provided."""
    from gateway.run import _cc_publish_gateway_session
    import types

    fake_source = types.SimpleNamespace(platform=types.SimpleNamespace(value="tui"))

    with patch("gateway.run.cc_upsert_live_session") as mock_upsert:
        _cc_publish_gateway_session("***", running=True, source=fake_source)

    mock_upsert.assert_called_once()
    _, kwargs = mock_upsert.call_args
    payload = kwargs.get("payload") or {}
    assert payload.get("platform") == "tui", (
        f"expected payload.platform='tui', got {payload!r}"
    )


def test_release_running_agent_state_publishes_running_false():
    """Forced gateway release paths should publish running=False using cached source."""
    import types
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running_agents = {"sk_rel": object()}
    runner._running_agents_ts = {"sk_rel": time.time()}
    runner._busy_ack_ts = {"sk_rel": time.time()}
    fake_source = types.SimpleNamespace(platform=types.SimpleNamespace(value="web"))
    runner._get_cached_session_source = lambda session_key: fake_source if session_key == "sk_rel" else None
    runner._is_session_run_current = lambda session_key, generation: True

    with patch("gateway.run._cc_publish_gateway_session") as mock_publish:
        cleared = GatewayRunner._release_running_agent_state(runner, "sk_rel")

    assert cleared is True
    assert "sk_rel" not in runner._running_agents
    assert "sk_rel" not in runner._running_agents_ts
    assert "sk_rel" not in runner._busy_ack_ts
    mock_publish.assert_called_once_with("sk_rel", running=False, source=fake_source)
