from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_kanban_reconcile_wake_triage_under_test",
        Path(__file__).resolve().parents[2] / "scripts" / "kanban_reconcile_wake_triage.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(actions, mode):
    module = _load_module()
    triage = module.rec.classify_wake_triage(actions)
    assert triage["mode"] == mode
    return {
        "ok": not actions,
        "board": "default",
        "db_path": "/tmp/kanban.db",
        "actions": actions,
        "wake_triage": triage,
        "mutation_applied": False,
    }


def test_no_agent_auto_silent_emits_wake_agent_false():
    module = _load_module()
    output = module.render_output(
        _result([], module.rec.WAKE_BUCKET_AUTO_SILENT),
        mode="no-agent",
    )

    assert json.loads(output) == {"wakeAgent": False, "mode": "auto_silent"}


def test_no_agent_compact_notify_emits_slack_safe_text():
    module = _load_module()
    actions = [
        {
            "kind": "pre_spawn_validation_decision",
            "task_id": "t_ready",
            "severity": "warning",
            "reason": "profile missing before spawn",
            "safe_to_apply": False,
            "signature": "pre_spawn_validation_decision:t_ready:abc",
            "details": {
                "assignee": "missing-profile",
                "validation_errors": ["profile not found: missing-profile"],
                "large_nested_payload": {"should_not_dump": "x" * 500},
            },
        }
    ]

    output = module.render_output(
        _result(actions, module.rec.WAKE_BUCKET_COMPACT_NOTIFY),
        mode="no-agent",
        examples=1,
    )

    assert "Kanban reconcile compact notification" in output
    assert "Mode: compact_notify | wake_agent=false" in output
    assert "pre_spawn_validation_decision" in output
    assert "highlights:" in output
    assert "large_nested_payload" not in output
    assert "should_not_dump" not in output


def test_agent_gate_suppresses_compact_notify_without_waking_agent():
    module = _load_module()
    actions = [
        {
            "kind": "old_ready_spawnable",
            "task_id": "t_ready",
            "severity": "warning",
            "reason": "ready too long",
            "safe_to_apply": False,
            "signature": "old_ready_spawnable:t_ready:abc",
            "details": {"assignee": "engineer"},
        }
    ]

    output = module.render_output(
        _result(actions, module.rec.WAKE_BUCKET_COMPACT_NOTIFY),
        mode="agent-gate",
    )

    assert json.loads(output) == {"wakeAgent": False, "mode": "compact_notify"}


def test_jensen_decision_required_emits_compact_prompt_in_both_modes():
    module = _load_module()
    actions = [
        {
            "kind": "scheduled_with_completed_parents_decision",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "needs explicit keep-parked/unblock/close decision",
            "safe_to_apply": False,
            "signature": "scheduled_with_completed_parents_decision:t_review:abc",
            "details": {
                "assignee": "reviewer",
                "parents": "t_impl:done",
                "parent_count": 1,
            },
        }
    ]
    result = _result(actions, module.rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED)

    no_agent_output = module.render_output(result, mode="no-agent", examples=1)
    agent_gate_output = module.render_output(result, mode="agent-gate", examples=1)

    for output in (no_agent_output, agent_gate_output):
        assert "Kanban reconcile requires Jensen decision" in output
        assert "Objective: resolve Kanban reconcile decision-required actions" in output
        assert "mode=jensen_decision_required wake_agent=true" in output
        assert "scheduled_with_completed_parents_decision" in output
        assert "Recommended Jensen action:" in output


def test_json_output_contains_triage_and_rendered_text():
    module = _load_module()
    actions = [
        {
            "kind": "old_ready_spawnable",
            "task_id": "t_ready",
            "severity": "warning",
            "reason": "ready too long",
            "safe_to_apply": False,
            "signature": "old_ready_spawnable:t_ready:abc",
            "details": {},
        }
    ]

    output = module.render_output(
        _result(actions, module.rec.WAKE_BUCKET_COMPACT_NOTIFY),
        mode="no-agent",
        json_output=True,
    )
    payload = json.loads(output)

    assert payload["mode"] == "no-agent"
    assert payload["triage"]["mode"] == module.rec.WAKE_BUCKET_COMPACT_NOTIFY
    assert "Kanban reconcile compact notification" in payload["rendered_text"]
