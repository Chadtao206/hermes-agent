from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_reconciler as rec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _kinds(result: dict) -> set[str]:
    return {action["kind"] for action in result["actions"]}


def _actions(result: dict, kind: str) -> list[dict]:
    return [action for action in result["actions"] if action["kind"] == kind]


def test_reconciler_reports_no_actions_for_healthy_board(kanban_home):
    result = rec.run_reconciler(now=1_700_000_000)

    assert result["mutation_applied"] is False
    assert result["actions"] == []
    assert result["ok"] is True
    assert result["wake_triage"]["mode"] == rec.WAKE_BUCKET_AUTO_SILENT
    assert result["wake_triage"]["wake_agent"] is False


def test_wake_triage_routes_deterministic_defects_to_compact_notify():
    actions = [
        {
            "kind": "pre_spawn_validation_decision",
            "task_id": "t_ready",
            "severity": "warning",
            "reason": "missing forced skill",
            "safe_to_apply": False,
            "signature": "pre_spawn_validation_decision:t_ready:abc",
            "details": {"validation_errors": ["missing forced skill(s): demo"]},
        },
        {
            "kind": "repeated_failure_signature_decision",
            "task_id": None,
            "severity": "error",
            "reason": "possible platform defect",
            "safe_to_apply": False,
            "signature": "repeated_failure_signature_decision:board:def",
            "details": {"failure_signature": "profile crash"},
        },
    ]

    triage = rec.classify_wake_triage(actions)

    assert triage["mode"] == rec.WAKE_BUCKET_COMPACT_NOTIFY
    assert triage["wake_agent"] is False
    assert triage["summary"][rec.WAKE_BUCKET_COMPACT_NOTIFY] == 2
    assert triage["summary"][rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED] == 0


def test_wake_triage_routes_ambiguous_handoffs_to_jensen_decision():
    actions = [
        {
            "kind": "scheduled_with_completed_parents_decision",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "needs keep-parked/unblock/close decision",
            "safe_to_apply": False,
            "signature": "scheduled_with_completed_parents_decision:t_review:abc",
            "details": {},
        },
        {
            "kind": "old_ready_spawnable",
            "task_id": "t_ready",
            "severity": "warning",
            "reason": "ready too long",
            "safe_to_apply": False,
            "signature": "old_ready_spawnable:t_ready:def",
            "details": {},
        },
    ]

    triage = rec.classify_wake_triage(actions)

    assert triage["mode"] == rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED
    assert triage["wake_agent"] is True
    assert triage["summary"][rec.WAKE_BUCKET_COMPACT_NOTIFY] == 1
    assert triage["summary"][rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED] == 1


def test_wake_triage_groups_duplicate_decision_actions_by_task():
    actions = [
        {
            "kind": "review_parent_pr_head_evidence_missing",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "missing PR head evidence",
            "safe_to_apply": False,
            "signature": "review_parent_pr_head_evidence_missing:t_review:abc",
            "details": {
                "parents": "t_impl:done",
                "parent_closeouts": [{"parent_task_id": "t_impl"}],
            },
        },
        {
            "kind": "scheduled_with_completed_parents_decision",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "needs keep-parked/unblock/close decision",
            "safe_to_apply": False,
            "signature": "scheduled_with_completed_parents_decision:t_review:def",
            "details": {},
        },
        {
            "kind": "scheduled_with_completed_parents_decision",
            "task_id": "t_other",
            "severity": "warning",
            "reason": "needs keep-parked/unblock/close decision",
            "safe_to_apply": False,
            "signature": "scheduled_with_completed_parents_decision:t_other:ghi",
            "details": {},
        },
    ]

    triage = rec.classify_wake_triage(actions)

    assert triage["summary"][rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED] == 3
    assert triage["decision_packet_count"] == 2
    first_packet = triage["decision_packets"][0]
    assert first_packet["task_id"] == "t_other"
    second_packet = triage["decision_packets"][1]
    assert second_packet["task_id"] == "t_review"
    assert second_packet["action_count"] == 2
    assert second_packet["kinds"] == [
        "review_parent_pr_head_evidence_missing",
        "scheduled_with_completed_parents_decision",
    ]
    assert second_packet["safe_to_apply"] is False
    assert second_packet["primary_category"] == "review_evidence_gap"
    assert second_packet["decision_categories"] == [
        "parked_completed_dependencies",
        "review_evidence_gap",
    ]
    assert second_packet["suggested_options"][:3] == [
        "remediate_parent_closeout",
        "keep_parked",
        "manual_review_with_stale_pr_risk",
    ]
    assert "PR-head evidence" in second_packet["recommended_next_step"]
    assert second_packet["affected_parent_task_ids"] == ["t_impl"]
    plans = second_packet["operator_plans"]
    assert set(plans) >= {
        "remediate_parent_closeout",
        "keep_parked",
        "manual_review_with_stale_pr_risk",
        "unblock",
        "close",
    }
    remediation = plans["remediate_parent_closeout"]
    assert remediation["dry_run"] is True
    assert remediation["requires_confirmation"] is True
    assert remediation["requires_input"] == [
        "verified current PR head SHA for each remediated parent"
    ]
    remediation_commands = [step["command"] for step in remediation["commands"]]
    assert any("hermes kanban show t_impl" in command for command in remediation_commands)
    assert any("hermes kanban edit t_impl" in command for command in remediation_commands)
    assert any("<verified_pr_head_sha>" in command for command in remediation_commands)
    unblock_commands = [step["command"] for step in plans["unblock"]["commands"]]
    assert unblock_commands[-1] == "hermes kanban unblock t_review"


def test_wake_triage_hints_blocked_completed_dependencies():
    actions = [
        {
            "kind": "blocked_with_completed_parents_decision",
            "task_id": "t_blocked",
            "severity": "warning",
            "reason": "blocked parents done",
            "safe_to_apply": False,
            "signature": "blocked_with_completed_parents_decision:t_blocked:abc",
            "details": {},
        },
    ]

    triage = rec.classify_wake_triage(actions)
    packet = triage["decision_packets"][0]

    assert packet["primary_category"] == "blocked_completed_dependencies"
    assert packet["decision_categories"] == ["blocked_completed_dependencies"]
    assert packet["suggested_options"] == ["unblock", "keep_blocked", "close"]
    assert "keep-blocked" in packet["recommended_next_step"]
    assert packet["operator_plans"]["keep_blocked"]["commands"][0]["command"].startswith(
        "hermes kanban comment t_blocked"
    )
    assert packet["operator_plans"]["close"]["commands"][0]["command"].startswith(
        "hermes kanban complete t_blocked"
    )


def test_format_reconcile_text_uses_decision_packets_for_jensen_output():
    actions = [
        {
            "kind": "review_parent_pr_head_evidence_missing",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "missing PR head evidence",
            "safe_to_apply": False,
            "signature": "review_parent_pr_head_evidence_missing:t_review:abc",
            "details": {
                "parents": "t_impl:done",
                "parent_closeouts": [{"parent_task_id": "t_impl"}],
            },
        },
        {
            "kind": "scheduled_with_completed_parents_decision",
            "task_id": "t_review",
            "severity": "warning",
            "reason": "needs keep-parked/unblock/close decision",
            "safe_to_apply": False,
            "signature": "scheduled_with_completed_parents_decision:t_review:def",
            "details": {},
        },
    ]
    result = {
        "board": "default",
        "db_path": "/tmp/kanban.db",
        "actions": actions,
        "wake_triage": rec.classify_wake_triage(actions),
    }

    text = rec.format_reconcile_text(result, max_examples=1)

    assert "decision_packets=1" in text
    assert "Decision packets (first 1; grouped by task" in text
    assert "packet [t_review] (decision-only; 2 action(s))" in text
    assert "review_parent_pr_head_evidence_missing" in text
    assert "scheduled_with_completed_parents_decision" in text
    assert "Examples (first" not in text
    assert "category: review_evidence_gap" in text
    assert "options: remediate_parent_closeout, keep_parked" in text
    assert "dry-run plans: remediate_parent_closeout:2 cmd(s), keep_parked:1 cmd(s)" in text
    assert "next: remediate parent closeout PR-head evidence" in text


def test_reconciler_splits_dead_expired_and_stale_heartbeat(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="running", assignee="engineer")
        conn.execute(
            """
            UPDATE tasks
               SET status = 'running', worker_pid = ?, claim_lock = ?,
                   claim_expires = ?, last_heartbeat_at = ?, started_at = ?
             WHERE id = ?
            """,
            (99999999, "otherhost:claim", now - 60, now - 3600, now - 7200, task_id),
        )

    result = rec.run_reconciler(now=now)
    kinds = _kinds(result)

    assert "dead_running_candidate" in kinds
    assert "expired_claim_candidate" in kinds
    assert "stale_heartbeat_observed" in kinds
    assert _actions(result, "stale_heartbeat_observed")[0]["safe_to_apply"] is False
    # Cross-host dead candidates remain advisory in Phase 1A.
    assert _actions(result, "dead_running_candidate")[0]["safe_to_apply"] is False


def test_stale_heartbeat_only_is_advisory_not_safe_to_apply(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="running", assignee="engineer")
        conn.execute(
            """
            UPDATE tasks
               SET status = 'running', worker_pid = ?, claim_lock = ?,
                   claim_expires = ?, last_heartbeat_at = ?, started_at = ?
             WHERE id = ?
            """,
            (os.getpid(), "localhost:claim", now + 3600, now - 3600, now - 7200, task_id),
        )

    result = rec.run_reconciler(now=now)
    stale = _actions(result, "stale_heartbeat_observed")

    assert len(stale) == 1
    assert stale[0]["task_id"] == task_id
    assert stale[0]["safe_to_apply"] is False
    assert "dead_running_candidate" not in _kinds(result)


def test_blocked_with_completed_parents_is_decision_only(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        child = kb.create_task(conn, title="child", assignee="reviewer")
        kb.complete_task(conn, parent, summary="done")
        kb.block_task(conn, child, reason="waiting on remediation")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            (parent, child),
        )

    result = rec.run_reconciler(now=1_700_000_000)
    actions = _actions(result, "blocked_with_completed_parents_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == child
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["signature"].startswith(
        f"blocked_with_completed_parents_decision:{child}:"
    )


def test_scheduled_with_completed_parents_is_decision_only(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        kb.complete_task(conn, parent, summary="done")
        child = kb.create_task(
            conn,
            title="scheduled child",
            assignee="reviewer",
            parents=[parent],
            initial_status="scheduled",
        )
        conn.execute(
            "UPDATE tasks SET created_at = ?, started_at = ? WHERE id = ?",
            (now - 3600, now - 1800, child),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "scheduled_with_completed_parents_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == child
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["details"]["parent_count"] == 1
    assert actions[0]["details"]["age_seconds"] == 3600
    assert actions[0]["signature"].startswith(
        f"scheduled_with_completed_parents_decision:{child}:"
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "scheduled"


def test_scheduled_with_completed_parents_respects_age_and_active_run_guards(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        kb.complete_task(conn, parent, summary="done")
        young = kb.create_task(
            conn,
            title="young scheduled child",
            assignee="reviewer",
            parents=[parent],
            initial_status="scheduled",
        )
        active = kb.create_task(
            conn,
            title="active scheduled child",
            assignee="reviewer",
            parents=[parent],
            initial_status="scheduled",
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 10, young))
        conn.execute(
            "UPDATE tasks SET created_at = ?, current_run_id = ? WHERE id = ?",
            (now - 3600, 12345, active),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)

    assert _actions(result, "scheduled_with_completed_parents_decision") == []


def test_review_parent_pr_head_evidence_missing_is_decision_only(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="implementation", assignee="engineer")
        kb.complete_task(conn, parent, summary="Implemented but forgot PR metadata.")
        child = kb.create_task(
            conn,
            title="final review",
            assignee="reviewer",
            parents=[parent],
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, child))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "review_parent_pr_head_evidence_missing")

    assert len(actions) == 1
    assert actions[0]["task_id"] == child
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["details"]["assignee"] == "reviewer"
    assert actions[0]["details"]["status"] == "ready"
    assert actions[0]["details"]["parent_count"] == 1
    closeout = actions[0]["details"]["parent_closeouts"][0]
    assert closeout["parent_task_id"] == parent
    assert closeout["pr_head_sha_present"] is False
    assert closeout["latest_completed_run_id"] is not None
    assert actions[0]["signature"].startswith(
        f"review_parent_pr_head_evidence_missing:{child}:"
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "ready"


def test_review_parent_pr_head_evidence_present_suppresses_warning(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="implementation", assignee="engineer")
        kb.complete_task(
            conn,
            parent,
            summary="Implemented and opened PR.",
            metadata={"pull_request_head_sha": "abcdef1234567890"},
        )
        child = kb.create_task(
            conn,
            title="final review",
            assignee="boris",
            parents=[parent],
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, child))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)

    assert _actions(result, "review_parent_pr_head_evidence_missing") == []


def test_review_parent_pr_head_evidence_missing_ignores_non_review_lanes(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="implementation", assignee="engineer")
        kb.complete_task(conn, parent, summary="done")
        child = kb.create_task(
            conn,
            title="engineering followup",
            assignee="engineer",
            parents=[parent],
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, child))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)

    assert _actions(result, "review_parent_pr_head_evidence_missing") == []


def test_pre_spawn_validation_surfaces_missing_profile(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: False)
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ready", assignee="missing-profile")
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, task_id))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "pre_spawn_validation_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == task_id
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["details"]["assignee"] == "missing-profile"
    assert "profile not found: missing-profile" in actions[0]["details"]["validation_errors"]


def test_pre_spawn_validation_surfaces_missing_forced_skill(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: True)
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="ready with missing skill",
            assignee="engineer",
            skills=["definitely-missing-skill"],
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, task_id))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "pre_spawn_validation_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == task_id
    assert actions[0]["details"]["skills"] == ["definitely-missing-skill"]
    assert "missing forced skill(s): definitely-missing-skill" in actions[0]["details"]["validation_errors"]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"


def test_pre_spawn_validation_surfaces_review_sdlc_skill_gap(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: True)
    monkeypatch.setattr(rec, "_has_sdlc_review_skill", lambda: True)
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute(
            "UPDATE tasks SET status = 'review', created_at = ? WHERE id = ?",
            (now - 3600, task_id),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "pre_spawn_validation_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == task_id
    assert "missing forced skill(s): sdlc-review" in actions[0]["details"]["validation_errors"]


def test_pre_spawn_validation_surfaces_workspace_shape_error(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: True)
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="dir without path",
            assignee="engineer",
            workspace_kind="dir",
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, task_id))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "pre_spawn_validation_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == task_id
    assert actions[0]["details"]["workspace_kind"] == "dir"
    assert "workspace_kind=dir requires workspace_path" in actions[0]["details"]["validation_errors"]


def test_pre_spawn_validation_suppresses_when_forced_skill_exists(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: True)
    skill_dir = kanban_home / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="ready with available skill",
            assignee="engineer",
            skills=["demo-skill"],
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, task_id))

    result = rec.run_reconciler(now=now, ready_age_seconds=60)

    assert _actions(result, "pre_spawn_validation_decision") == []


def test_repeated_failure_signature_surfaces_platform_defect_risk(kanban_home):
    with kb.connect() as conn:
        task_ids = [
            kb.create_task(conn, title=f"flaky {idx}", assignee="engineer")
            for idx in range(kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD)
        ]
        for idx, task_id in enumerate(task_ids):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = 1, last_failure_error = ? WHERE id = ?",
                (f"pid {10000 + idx} not alive", task_id),
            )

    result = rec.run_reconciler(now=1_700_000_000, ready_age_seconds=999999)
    actions = _actions(result, "repeated_failure_signature_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] is None
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["severity"] == "warning"
    details = actions[0]["details"]
    assert details["failure_signature"] == "pid n not alive"
    assert details["task_count"] == kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD
    assert details["status_counts"] == {"ready": kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD}
    assert len(details["tasks"]) == kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD


def test_repeated_failure_signature_respects_threshold(kanban_home):
    with kb.connect() as conn:
        for idx in range(kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD - 1):
            task_id = kb.create_task(conn, title=f"flaky {idx}", assignee="engineer")
            conn.execute(
                "UPDATE tasks SET consecutive_failures = 1, last_failure_error = ? WHERE id = ?",
                (f"pid {20000 + idx} not alive", task_id),
            )

    result = rec.run_reconciler(now=1_700_000_000, ready_age_seconds=999999)

    assert _actions(result, "repeated_failure_signature_decision") == []


def test_repeated_failure_signature_surfaces_systemic_metadata_below_threshold(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="systemic", assignee="engineer")
        conn.execute(
            "UPDATE tasks SET status = 'blocked', consecutive_failures = 1, "
            "last_failure_error = 'same platform failure' WHERE id = ?",
            (task_id,),
        )
        conn.execute(
            """
            INSERT INTO task_events(task_id, kind, payload, created_at)
            VALUES (?, 'gave_up', ?, ?)
            """,
            (
                task_id,
                json.dumps({
                    "failure_class": kb.FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE,
                    "failure_signature": "platform-profile-boom",
                    "trigger_outcome": "spawn_failed",
                }),
                1_700_000_000,
            ),
        )

    result = rec.run_reconciler(now=1_700_000_000, ready_age_seconds=999999)
    actions = _actions(result, "repeated_failure_signature_decision")

    assert len(actions) == 1
    assert actions[0]["severity"] == "error"
    details = actions[0]["details"]
    assert details["failure_signature"] == "platform-profile-boom"
    assert details["task_count"] == 1
    assert details["systemic_metadata_present"] is True
    assert kb.FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE in details["failure_classes"]


def test_stale_run_metadata_is_reported_without_task_mutation(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="done task", assignee="engineer")
        kb.complete_task(conn, task_id, summary="done")
        run_id = conn.execute(
            """
            INSERT INTO task_runs(task_id, profile, status, worker_pid, started_at)
            VALUES (?, 'engineer', 'running', 99999999, ?)
            """,
            (task_id, 1),
        ).lastrowid

    result = rec.run_reconciler(now=1_700_000_000)
    actions = _actions(result, "stale_run_metadata")

    assert len(actions) == 1
    assert actions[0]["details"]["run_id"] == run_id
    assert actions[0]["safe_to_apply"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"


def test_old_ready_and_review_spawnable_classification(kanban_home, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: profile == "engineer")
    with kb.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="engineer")
        blocked_ready = kb.create_task(conn, title="ready nonspawn", assignee="not-a-profile")
        review = kb.create_task(conn, title="review", assignee="engineer")
        review_nonspawn = kb.create_task(conn, title="review nonspawn", assignee="not-a-profile")
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id IN (?, ?)",
            (now - 3600, ready, blocked_ready),
        )
        conn.execute(
            "UPDATE tasks SET status = 'review', created_at = ? WHERE id IN (?, ?)",
            (now - 3600, review, review_nonspawn),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    kinds = _kinds(result)

    assert "old_ready_spawnable" in kinds
    assert "old_ready_nonspawnable" in kinds
    assert "old_review_spawnable" in kinds
    assert "old_review_nonspawnable" in kinds


def test_review_skill_provenance_missing_is_diagnostic_only(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_has_sdlc_review_skill", lambda: False)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))

    result = rec.run_reconciler(now=1_700_000_000)
    actions = _actions(result, "review_skill_provenance_missing")

    assert len(actions) == 1
    assert actions[0]["task_id"] is None
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["details"]["skill"] == "sdlc-review"


def test_reconcile_cli_json_is_read_only(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        child = kb.create_task(conn, title="child", assignee="reviewer")
        kb.complete_task(conn, parent, summary="done")
        kb.block_task(conn, child, reason="waiting")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            (parent, child),
        )

    db_path = kb.kanban_db_path()
    # Force setup writes into the main DB, then remove absent sidecars so this
    # regression proves reconcile itself does not create WAL/SHM files.
    with kb.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for sidecar in (db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    wal_before = db_path.with_name(db_path.name + "-wal").exists()
    shm_before = db_path.with_name(db_path.name + "-shm").exists()

    out = kc.run_slash("reconcile --json")
    payload = json.loads(out)

    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert before == after
    assert db_path.with_name(db_path.name + "-wal").exists() == wal_before
    assert db_path.with_name(db_path.name + "-shm").exists() == shm_before
    assert payload["mutation_applied"] is False
    assert "blocked_with_completed_parents_decision" in _kinds(payload)
    assert payload["wake_triage"]["mode"] == rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED
    assert payload["wake_triage"]["wake_agent"] is True


def test_reconcile_cli_text_groups_actions(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ready", assignee="missing-profile")
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, task_id))

    out = kc.run_slash("reconcile --ready-age-seconds 60")

    assert "Kanban reconcile:" in out
    assert "Summary:" in out
    assert "Wake triage:" in out
    assert "old_ready_nonspawnable" in out


def test_reconcile_text_is_compact_and_points_to_json_for_full_payload():
    actions = []
    for idx in range(3):
        actions.append({
            "kind": "repeated_failure_signature_decision",
            "task_id": None,
            "severity": "warning",
            "reason": "multiple non-terminal tasks share a failure signature or systemic failure metadata; treat as possible platform/profile defect before retrying",
            "safe_to_apply": False,
            "signature": f"sig-{idx}",
            "details": {
                "failure_signature": "spawn failure with a very long normalized platform signature " + ("x" * 200),
                "task_count": 12,
                "status_counts": {"ready": 8, "blocked": 4},
                "assignee_counts": {"engineer": 7, "reviewer": 5},
                "tasks": [
                    {
                        "task_id": f"t_{idx}_{task_idx}",
                        "status": "ready",
                        "assignee": "engineer",
                        "last_failure_error": "full nested error should not be dumped " + ("y" * 500),
                    }
                    for task_idx in range(6)
                ],
                "large_nested_payload": {"secretly_too_big": "z" * 1000},
            },
        })

    text = rec.format_reconcile_text(
        {
            "board": "default",
            "db_path": "/tmp/kanban.db",
            "actions": actions,
        },
        max_examples=1,
    )

    assert "Summary:" in text
    assert "Wake triage:" in text
    assert "mode=compact_notify wake_agent=false" in text
    assert "Examples (first 1; full payload: rerun with --json):" in text
    assert "... 2 more action(s) omitted" in text
    assert "highlights:" in text
    assert "failure_signature=" in text
    assert "status_counts=ready=8, blocked=4" in text
    assert "details:" not in text
    assert "large_nested_payload" not in text
    assert "secretly_too_big" not in text
    assert "full nested error should not be dumped" not in text


def test_reconcile_cli_examples_limits_human_output_but_json_remains_full(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        for idx in range(3):
            task_id = kb.create_task(conn, title=f"ready {idx}", assignee="missing-profile")
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                (now - 3600 - idx, task_id),
            )

    text = kc.run_slash("reconcile --ready-age-seconds 60 --examples 1")
    payload = json.loads(kc.run_slash("reconcile --ready-age-seconds 60 --json"))

    assert "Examples (first 1; full payload: rerun with --json):" in text
    assert "more action(s) omitted" in text
    assert len(payload["actions"]) >= 3
    assert payload["wake_triage"]["mode"] == rec.WAKE_BUCKET_COMPACT_NOTIFY
    assert payload["wake_triage"]["wake_agent"] is False
    assert payload["mutation_applied"] is False
