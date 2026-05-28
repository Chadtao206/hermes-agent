from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "kanban_db_readonly_health.py"
)


@pytest.fixture()
def health_script_module():
    spec = importlib.util.spec_from_file_location("kanban_db_readonly_health", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def test_script_text_output_preserves_phase_attribution(health_script_module, monkeypatch, capsys):
    monkeypatch.setattr(
        health_script_module.kb,
        "kanban_db_path",
        lambda board=None: Path("/tmp/kanban.db"),
    )
    monkeypatch.setattr(
        health_script_module.kanban_health,
        "run_readonly_health_bundle",
        lambda *args, **kwargs: {
            "ok": False,
            "db_path": "/tmp/kanban.db",
            "failure_count": 1,
            "phases": [
                {"phase": "sqlite3_cli_quick_check", "status": "ok", "quick_check_rows": ["ok"]},
                {
                    "phase": "python_ro_connect",
                    "status": "failed",
                    "exception_class": "OperationalError",
                    "exception_message": "unable to open database file",
                },
                {
                    "phase": "python_ro_select_1",
                    "status": "skipped",
                    "reason": "python_ro_connect_failed",
                },
                {
                    "phase": "python_ro_pragma_quick_check",
                    "status": "skipped",
                    "reason": "python_ro_connect_failed",
                },
            ],
        },
    )

    rc = health_script_module.main([])
    out = capsys.readouterr().out

    assert rc == 2
    assert "python_ro_connect: failed" in out
    assert "exception: OperationalError: unable to open database file" in out
    assert "python_ro_select_1: skipped" in out
    assert "reason: python_ro_connect_failed" in out



def test_script_json_output_contains_all_phases(health_script_module, monkeypatch, capsys):
    monkeypatch.setattr(
        health_script_module.kb,
        "kanban_db_path",
        lambda board=None: Path("/tmp/kanban.db"),
    )
    monkeypatch.setattr(
        health_script_module.kanban_health,
        "run_readonly_health_bundle",
        lambda *args, **kwargs: {
            "ok": True,
            "db_path": "/tmp/kanban.db",
            "failure_count": 0,
            "phases": [
                {"phase": "sqlite3_cli_quick_check", "status": "ok", "quick_check_rows": ["ok"]},
                {"phase": "python_ro_connect", "status": "ok"},
                {"phase": "python_ro_select_1", "status": "ok", "row": [1]},
                {
                    "phase": "python_ro_pragma_quick_check",
                    "status": "ok",
                    "quick_check_rows": ["ok"],
                },
            ],
        },
    )

    rc = health_script_module.main(["--json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert '"phase": "sqlite3_cli_quick_check"' in out
    assert '"phase": "python_ro_pragma_quick_check"' in out


def test_health_helper_source_does_not_use_readonly_flags():
    source = (Path(__file__).resolve().parents[2] / "hermes_cli" / "kanban_health.py").read_text(
        encoding="utf-8"
    )

    assert '"-readonly"' not in source
    assert '"--readonly"' not in source
