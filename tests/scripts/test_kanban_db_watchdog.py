from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "kanban_db_watchdog.py"


@pytest.fixture()
def watchdog_module():
    spec = importlib.util.spec_from_file_location("kanban_db_watchdog", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_silent_when_no_matching_issues(watchdog_module, monkeypatch, capsys):
    monkeypatch.setattr(
        watchdog_module.kanban_board_doctor,
        "run_board_doctor",
        lambda **kwargs: {
            "ok": True,
            "board": "default",
            "db_path": "/tmp/kanban.db",
            "issues": [
                {
                    "severity": "warning",
                    "kind": "old_ready_task",
                    "message": "old ready task",
                }
            ],
        },
    )

    rc = watchdog_module.main(["--min-severity", "critical"])

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_watchdog_emits_warning_when_threshold_allows_it(watchdog_module, monkeypatch, capsys):
    monkeypatch.setattr(
        watchdog_module.kanban_board_doctor,
        "run_board_doctor",
        lambda **kwargs: {
            "ok": False,
            "board": "default",
            "db_path": "/tmp/kanban.db",
            "issues": [
                {
                    "severity": "warning",
                    "kind": "blocked_with_completed_parents",
                    "message": "task needs explicit unblock",
                    "action": "review and unblock if ready",
                }
            ],
            "reconcile_summary": {"ok": False, "action_count": 1},
        },
    )

    rc = watchdog_module.main(["--min-severity", "warning"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "Kanban store needs attention: 1 warning+ issue on default" in out
    assert "blocked with completed parents (WARNING)" in out
    assert "review and unblock if ready" in out
    assert "Reconcile context:" in out
    assert "Open actions: 1" in out
    assert "reconcile_summary:" not in out


def test_watchdog_text_output_summarizes_reconcile_without_raw_json(watchdog_module):
    payload = {
        "ok": False,
        "board": "default",
        "db_path": "postgres://example.test:6543/postgres",
        "issues": [
            {
                "severity": "critical",
                "kind": "stale_running_task",
                "message": "running task has dead/missing worker, expired claim, or stale heartbeat",
                "action": "reclaim or inspect worker logs before retrying",
                "task_id": "t_dead",
                "assignee": "default",
                "worker_pid": 19959,
                "pid_alive": False,
                "claim_expired": False,
            }
        ],
        "reconcile_summary": {
            "ok": False,
            "action_count": 4,
            "kinds": {
                "blocked_with_completed_parents_decision": 2,
                "dead_running_candidate": 1,
                "stale_run_metadata": 1,
            },
            "wake_mode": "jensen_decision_required",
            "wake_agent": True,
            "suppressed_decision_packet_count": 2,
            "suppressed_decision_packets": [
                {"task_id": "t_ack_1", "option": "keep_blocked"},
                {"task_id": "t_ack_2", "option": "keep_blocked"},
            ],
            "suppressed_doctor_issue_count": 2,
        },
    }

    out = watchdog_module.build_message(payload, min_severity="critical")

    assert "Kanban store needs attention: 1 critical+ issue on default" in out
    assert "t_dead: stale running task (CRITICAL)" in out
    assert "Evidence: assignee=default, worker_pid=19959, pid_alive=no, claim_expired=no" in out
    assert "Useful commands:" in out
    assert "hermes kanban reclaim t_dead" in out
    assert "Open actions: 4" in out
    assert "blocked with completed parents decision=2" in out
    assert "Wake mode: jensen_decision_required; wake_agent=yes" in out
    assert "Already acknowledged decision packets: 2 (t_ack_1:keep_blocked, t_ack_2:keep_blocked)" in out
    assert "Related doctor issues hidden by acknowledgements: 2" in out
    assert "reconcile_summary:" not in out
    assert "{\"action_count\"" not in out


def test_watchdog_emits_json_and_critical_exit_code(watchdog_module, monkeypatch, capsys):
    monkeypatch.setattr(
        watchdog_module.kanban_board_doctor,
        "run_board_doctor",
        lambda **kwargs: {
            "ok": False,
            "board": "default",
            "db_path": "/tmp/kanban.db",
            "issues": [
                {
                    "severity": "critical",
                    "kind": "db_quick_check_failed",
                    "message": "PRAGMA quick_check returned malformed",
                    "action": "stop writers and recover DB",
                },
                {
                    "severity": "warning",
                    "kind": "old_ready_task",
                    "message": "old ready task",
                },
            ],
        },
    )

    rc = watchdog_module.main(["--json"])
    out = capsys.readouterr().out

    assert rc == 2
    assert '"watchdog_threshold": "critical"' in out
    assert '"kind": "db_quick_check_failed"' in out
    assert '"kind": "old_ready_task"' not in out
