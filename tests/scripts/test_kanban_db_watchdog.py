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
    assert "Kanban DB watchdog alert (default)" in out
    assert "warning/blocked_with_completed_parents" in out
    assert "review and unblock if ready" in out


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
