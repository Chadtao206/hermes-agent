"""Known kanban write-gate exceptions must survive the single-writer daemon
boundary: serialize to a JSON-able payload and reconstruct as the same type
with the same attributes, so `except kb.PRHeadGateError` keeps working when the
write ran on the daemon/over the socket."""
import json

from hermes_cli import kanban_db as kb


def test_prhead_gate_error_round_trips():
    err = kb.PRHeadGateError(
        task_id="t_child", expected_sha="abc123", reviewed_sha="def456",
        parent_task_id="t_parent", parent_run_id=7,
    )
    payload = kb.serialize_kanban_error(err)
    assert payload["type"] == "PRHeadGateError"
    # Payload must be JSON-able (it rides the wire frame).
    json.dumps(payload)

    out = kb.reconstruct_kanban_error(payload)
    assert isinstance(out, kb.PRHeadGateError)
    assert out.expected_sha == "abc123"
    assert out.reviewed_sha == "def456"
    assert out.parent_task_id == "t_parent"
    assert out.parent_run_id == 7
    assert str(out) == str(err)


def test_hallucinated_cards_error_round_trips():
    err = kb.HallucinatedCardsError(["t_a", "t_b"], "t_completer")
    payload = kb.serialize_kanban_error(err)
    assert payload["type"] == "HallucinatedCardsError"
    out = kb.reconstruct_kanban_error(payload)
    assert isinstance(out, kb.HallucinatedCardsError)
    assert out.phantom == ["t_a", "t_b"]
    assert out.completing_task_id == "t_completer"


def test_external_handoff_gate_error_round_trips():
    err = kb.ExternalHandoffGateError(task_id="t_x")
    out = kb.reconstruct_kanban_error(kb.serialize_kanban_error(err))
    assert isinstance(out, kb.ExternalHandoffGateError)
    assert out.task_id == "t_x"


def test_unknown_error_is_not_serializable():
    assert kb.serialize_kanban_error(ValueError("plain")) is None
    assert kb.serialize_kanban_error(RuntimeError("x")) is None


def test_reconstruct_tolerates_unknown_or_empty_payload():
    assert kb.reconstruct_kanban_error(None) is None
    assert kb.reconstruct_kanban_error({}) is None
    assert kb.reconstruct_kanban_error({"type": "NotAThing", "fields": {}}) is None
