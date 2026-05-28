from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_board_doctor as doctor
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


def _setup_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()



def test_doctor_preserves_failed_phase_attribution_for_ro_open_errors(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    monkeypatch.setattr(
        doctor.kanban_health,
        "run_readonly_health_bundle",
        lambda path: {
            "ok": False,
            "db_path": str(path),
            "phases": [
                {"phase": kh.PHASE_SQLITE3_CLI_QUICK_CHECK, "status": "ok"},
                {
                    "phase": kh.PHASE_PYTHON_RO_CONNECT,
                    "status": "failed",
                    "exception_class": "OperationalError",
                    "exception_message": "unable to open database file",
                },
                {"phase": kh.PHASE_PYTHON_RO_SELECT_1, "status": "skipped", "reason": "python_ro_connect_failed"},
                {
                    "phase": kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK,
                    "status": "skipped",
                    "reason": "python_ro_connect_failed",
                },
            ],
        },
    )

    result = doctor.run_board_doctor()

    assert result["ok"] is False
    issue = result["issues"][0]
    assert issue["kind"] == "db_unreadable"
    assert issue["failed_phase"] == kh.PHASE_PYTHON_RO_CONNECT
    assert "OperationalError" in issue["message"]
    assert isinstance(issue["health_phases"], list)



def test_doctor_maps_python_quick_check_rows_to_db_quick_check_failed(tmp_path, monkeypatch):
    _setup_home(tmp_path, monkeypatch)

    monkeypatch.setattr(
        doctor.kanban_health,
        "run_readonly_health_bundle",
        lambda path: {
            "ok": False,
            "db_path": str(path),
            "phases": [
                {"phase": kh.PHASE_SQLITE3_CLI_QUICK_CHECK, "status": "ok"},
                {"phase": kh.PHASE_PYTHON_RO_CONNECT, "status": "ok"},
                {"phase": kh.PHASE_PYTHON_RO_SELECT_1, "status": "ok"},
                {
                    "phase": kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK,
                    "status": "failed",
                    "message": "python read-only quick_check did not return ok",
                    "quick_check_rows": ["malformed table x"],
                },
            ],
        },
    )

    result = doctor.run_board_doctor()

    assert result["ok"] is False
    issue = result["issues"][0]
    assert issue["kind"] == "db_quick_check_failed"
    assert issue["failed_phase"] == kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK
    assert issue["quick_check_rows"] == ["malformed table x"]
