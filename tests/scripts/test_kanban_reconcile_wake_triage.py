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


def test_text_output_is_bounded_for_slack_safe_delivery():
    module = _load_module()
    actions = []
    for idx in range(20):
        actions.append({
            "kind": "pre_spawn_validation_decision",
            "task_id": f"t_ready_{idx}",
            "severity": "warning",
            "reason": "profile missing before spawn " + ("x" * 200),
            "safe_to_apply": False,
            "signature": f"pre_spawn_validation_decision:t_ready_{idx}:abc",
            "details": {
                "assignee": "missing-profile",
                "validation_errors": ["profile not found: missing-profile " + ("y" * 200)],
            },
        })

    output = module.render_output(
        _result(actions, module.rec.WAKE_BUCKET_COMPACT_NOTIFY),
        mode="no-agent",
        examples=20,
        max_chars=700,
    )

    assert len(output) <= 700
    assert "[truncated: output exceeded 700 chars" in output
    assert "hermes kanban reconcile --json" in output


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


def test_stable_action_digest_ignores_age_and_timestamps():
    module = _load_module()
    base_action = {
        "kind": "scheduled_with_completed_parents_decision",
        "task_id": "t_review",
        "severity": "warning",
        "reason": "needs decision",
        "safe_to_apply": False,
        "signature": "age-sensitive-signature-1",
        "details": {
            "assignee": "reviewer",
            "parents": "t_impl:done",
            "parent_count": 1,
            "age_seconds": 10,
            "created_at": 100,
            "started_at": 200,
        },
    }
    later_action = json.loads(json.dumps(base_action))
    later_action["signature"] = "age-sensitive-signature-2"
    later_action["details"]["age_seconds"] = 9999
    later_action["details"]["created_at"] = 123
    later_action["details"]["started_at"] = 456

    first = _result([base_action], module.rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED)
    second = _result([later_action], module.rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED)

    assert module.stable_action_digest(first) == module.stable_action_digest(second)


def test_dedupe_suppresses_unchanged_signal_inside_repeat_window(tmp_path):
    module = _load_module()
    state_path = tmp_path / "wake-triage-state.json"
    actions = [
        {
            "kind": "scheduled_with_completed_parents_decision",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "needs explicit keep-parked/unblock/close decision",
            "safe_to_apply": False,
            "signature": "scheduled_with_completed_parents_decision:t_review:abc",
            "details": {"assignee": "reviewer", "parents": "t_impl:done", "age_seconds": 100},
        }
    ]
    result = _result(actions, module.rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED)

    emit_first, first_meta = module.should_emit_with_dedupe(
        result,
        mode="no-agent",
        state_path=state_path,
        min_repeat_seconds=3600,
        now=1000,
    )
    emit_second, second_meta = module.should_emit_with_dedupe(
        result,
        mode="no-agent",
        state_path=state_path,
        min_repeat_seconds=3600,
        now=1100,
    )

    assert emit_first is True
    assert first_meta["dedupe"] == "emitted"
    assert emit_second is False
    assert second_meta["dedupe"] == "suppressed"
    assert json.loads(module.render_suppressed_output(result, second_meta))["wakeAgent"] is False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["entries"]


def test_dedupe_reemits_after_repeat_window_or_changed_signal(tmp_path):
    module = _load_module()
    state_path = tmp_path / "wake-triage-state.json"
    action = {
        "kind": "pre_spawn_validation_decision",
        "task_id": "t_ready",
        "severity": "warning",
        "reason": "profile missing before spawn",
        "safe_to_apply": False,
        "signature": "pre_spawn_validation_decision:t_ready:abc",
        "details": {"assignee": "missing-profile"},
    }
    first = _result([action], module.rec.WAKE_BUCKET_COMPACT_NOTIFY)
    changed_action = json.loads(json.dumps(action))
    changed_action["details"]["validation_errors"] = ["profile not found: missing-profile"]
    changed = _result([changed_action], module.rec.WAKE_BUCKET_COMPACT_NOTIFY)

    emit_first, _ = module.should_emit_with_dedupe(
        first,
        mode="no-agent",
        state_path=state_path,
        min_repeat_seconds=3600,
        now=1000,
    )
    emit_after_window, _ = module.should_emit_with_dedupe(
        first,
        mode="no-agent",
        state_path=state_path,
        min_repeat_seconds=3600,
        now=5000,
    )
    emit_changed, _ = module.should_emit_with_dedupe(
        changed,
        mode="no-agent",
        state_path=state_path,
        min_repeat_seconds=3600,
        now=5100,
    )

    assert emit_first is True
    assert emit_after_window is True
    assert emit_changed is True


def test_corrupt_dedupe_state_does_not_block_emit(tmp_path):
    module = _load_module()
    state_path = tmp_path / "wake-triage-state.json"
    state_path.write_text("not json", encoding="utf-8")
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

    emit, metadata = module.should_emit_with_dedupe(
        _result(actions, module.rec.WAKE_BUCKET_COMPACT_NOTIFY),
        mode="no-agent",
        state_path=state_path,
        min_repeat_seconds=3600,
        now=1000,
    )

    assert emit is True
    assert metadata["dedupe"] == "emitted"
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 1


def test_cron_setup_instructions_are_non_mutating_and_actionable(monkeypatch, tmp_path):
    module = _load_module()
    script_dir = tmp_path / "scripts"
    monkeypatch.setattr(module, "default_cron_script_dir", lambda: script_dir)

    text = module.cron_setup_instructions(
        schedule="every 20m",
        deliver="slack",
    )

    assert "example only; does not create a job" in text
    assert "mkdir -p" in text
    assert "cp " in text
    assert str(script_dir / "kanban_reconcile_wake_triage.py") in text
    assert "hermes cron create 'every 20m'" in text
    assert "--script kanban_reconcile_wake_triage.py" in text
    assert "--no-agent" in text
    assert "--deliver slack" in text
    assert "Equivalent cronjob tool payload:" in text
    assert '"no_agent": true' in text
    assert '"deliver": "slack"' in text
    assert "Do not enable this automatically" in text


def test_print_cron_setup_exits_before_reconcile(monkeypatch, capsys, tmp_path):
    module = _load_module()
    monkeypatch.setattr(module, "default_cron_script_dir", lambda: tmp_path / "scripts")

    def fail_reconcile(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("reconcile should not run for --print-cron-setup")

    monkeypatch.setattr(module.rec, "run_reconciler", fail_reconcile)

    exit_code = module.main([
        "--print-cron-setup",
        "--setup-schedule",
        "every 30m",
        "--setup-deliver",
        "origin",
    ])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "hermes cron create 'every 30m'" in out
    assert "--deliver origin" in out
