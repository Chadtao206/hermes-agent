from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from hermes_cli import kanban_health as kh


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t(value) VALUES ('ok')")


def _phase_map(result: dict) -> dict[str, dict]:
    return {entry["phase"]: entry for entry in result["phases"]}


def test_run_readonly_health_bundle_reports_ordered_phases(tmp_path):
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)

    result = kh.run_readonly_health_bundle(db_path)

    assert result["db_path"] == str(db_path)
    assert result["phase_order"] == list(kh.PHASE_ORDER)
    assert len(result["phases"]) == len(kh.PHASE_ORDER)

    phases = _phase_map(result)
    assert phases[kh.PHASE_PYTHON_RO_CONNECT]["status"] == "ok"
    assert phases[kh.PHASE_PYTHON_RO_SELECT_1]["status"] == "ok"
    assert phases[kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK]["status"] == "ok"
    assert phases[kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK]["quick_check_rows"] == ["ok"]



def test_transient_ro_open_failure_is_attributed_to_connect_phase(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)

    real_connect = kh.sqlite3.connect

    def fake_connect(*args, **kwargs):
        uri = args[0] if args else ""
        if isinstance(uri, str) and "?mode=ro" in uri:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(kh.sqlite3, "connect", fake_connect)

    result = kh.run_readonly_health_bundle(db_path)
    phases = _phase_map(result)

    assert result["ok"] is False
    assert phases[kh.PHASE_PYTHON_RO_CONNECT]["status"] == "failed"
    assert phases[kh.PHASE_PYTHON_RO_CONNECT]["exception_class"] == "OperationalError"
    assert "unable to open database file" in phases[kh.PHASE_PYTHON_RO_CONNECT]["exception_message"]
    assert phases[kh.PHASE_PYTHON_RO_SELECT_1]["status"] == "skipped"
    assert phases[kh.PHASE_PYTHON_RO_SELECT_1]["reason"] == "python_ro_connect_failed"
    assert phases[kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK]["status"] == "skipped"



def test_transient_ro_quick_check_failure_is_attributed_to_phase_four(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)

    class FakeCursor:
        def __iter__(self):
            raise sqlite3.OperationalError("unable to open database file")

    class FakeConn:
        def execute(self, query):
            normalized = " ".join(str(query).split()).upper()
            if normalized == "PRAGMA QUERY_ONLY=ON":
                return []
            if normalized == "SELECT 1":
                class Row:
                    def fetchone(self_inner):
                        return (1,)

                return Row()
            if normalized == "PRAGMA QUICK_CHECK":
                return FakeCursor()
            raise AssertionError(f"unexpected query: {query}")

        def close(self):
            return None

    monkeypatch.setattr(kh.sqlite3, "connect", lambda *args, **kwargs: FakeConn())

    result = kh.run_readonly_health_bundle(db_path)
    phases = _phase_map(result)

    assert result["ok"] is False
    assert phases[kh.PHASE_PYTHON_RO_CONNECT]["status"] == "ok"
    assert phases[kh.PHASE_PYTHON_RO_SELECT_1]["status"] == "ok"
    assert phases[kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK]["status"] == "failed"
    assert phases[kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK]["exception_class"] == "OperationalError"
    assert "unable to open database file" in phases[kh.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK]["exception_message"]


def test_sqlite3_cli_timeout_keeps_phase_attribution(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)

    monkeypatch.setattr(kh.shutil, "which", lambda _: "/usr/bin/sqlite3")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=20)

    monkeypatch.setattr(kh.subprocess, "run", fake_run)

    result = kh.run_readonly_health_bundle(db_path)
    phases = _phase_map(result)

    assert phases[kh.PHASE_SQLITE3_CLI_QUICK_CHECK]["status"] == "failed"
    assert phases[kh.PHASE_SQLITE3_CLI_QUICK_CHECK]["exception_class"] == "TimeoutExpired"
    assert phases[kh.PHASE_PYTHON_RO_CONNECT]["status"] == "ok"


def test_sqlite3_cli_quick_check_uses_immutable_ro_uri(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    _make_db(db_path)

    monkeypatch.setattr(kh.shutil, "which", lambda _: "/usr/bin/sqlite3")
    captured: dict[str, list[str]] = {}

    def fake_run(*args, **kwargs):
        argv = list(args[0])
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(kh.subprocess, "run", fake_run)

    result = kh.run_readonly_health_bundle(db_path)
    phases = _phase_map(result)
    argv = captured["argv"]

    assert "-readonly" not in argv
    assert "--readonly" not in argv
    assert argv[0] == "/usr/bin/sqlite3"
    assert any(
        arg.startswith("file:") and "mode=ro" in arg and "immutable=1" in arg
        for arg in argv
    )
    assert phases[kh.PHASE_SQLITE3_CLI_QUICK_CHECK]["status"] == "ok"
