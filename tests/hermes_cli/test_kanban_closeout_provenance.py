"""Workflow-metrics eligibility for kanban closeout.

The closeout payload feeds ``log_task_closeout.py`` which writes
``tasks.provenance`` / ``tasks.substantiality``. Workflow-metrics eligibility
(``export_weekly_report.py``) filters on those columns, so completed kanban
work must emit ``real`` / ``substantial`` by default — otherwise every
closeout requires manual flags and KPIs report 0 eligible kanban tasks.

These tests exercise the dry-run payload only; the SQL persistence path
is covered by the external telemetry-script verification in
``scripts/telemetry/test_log_task_closeout_provenance.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def stub_telemetry_script(tmp_path, monkeypatch):
    """Point ``_LOG_TASK_CLOSEOUT_SCRIPT`` at a path that exists so
    ``_cmd_closeout`` clears its precondition check and reaches the
    dry-run branch we want to exercise.
    """
    stub = tmp_path / "log_task_closeout.py"
    stub.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(kc, "_LOG_TASK_CLOSEOUT_SCRIPT", stub)
    return stub


def _make_completed_task(title: str = "ship feature x") -> str:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="andrej")
        # Drive the task into ``done`` so the closeout precondition passes.
        kb.complete_task(conn, tid, result="done", summary="completed via test")
    return tid


def _closeout_namespace(task_id: str, **overrides) -> argparse.Namespace:
    base = {
        "task_id": task_id,
        "telemetry_root": None,
        "owner": None,
        "title": None,
        "summary": None,
        "outcome": "success",
        "status": "completed",
        "verification_strength": "moderate",
        "initial_owner": None,
        "current_owner": None,
        "correct_owner": None,
        "user_corrected": None,
        "correction_state": None,
        "no_correction": False,
        "learning_artifact_state": None,
        "no_learning_artifact": False,
        "provenance": None,
        "substantiality": None,
        "notes_json": None,
        "dry_run": True,
        "json": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_dry_closeout(capsys, args: argparse.Namespace) -> dict:
    rc = kc._cmd_closeout(args)
    assert rc == 0
    out = capsys.readouterr().out
    return json.loads(out)


def test_default_kanban_closeout_emits_real_substantial(
    kanban_home, stub_telemetry_script, capsys,
):
    """A bare ``hermes kanban closeout <id>`` must default the payload to
    provenance=real / substantiality=substantial.

    Without this, workflow-metrics eligibility (which filters on
    ``lower(provenance)='real'`` AND ``lower(substantiality)='substantial'``)
    counts every default kanban closeout as ineligible — the audit-driver
    for this lane.
    """
    tid = _make_completed_task()
    payload = _run_dry_closeout(capsys, _closeout_namespace(tid))

    assert payload["provenance"] == "real"
    assert payload["substantiality"] == "substantial"
    # Sanity: correction/learning still default to unknown (lane-1 contract);
    # this lane only changes provenance/substantiality defaults.
    assert payload["correction_state"] == "unknown"
    assert payload["learning_artifact_state"] == "unknown"


def test_closeout_provenance_override_respected(
    kanban_home, stub_telemetry_script, capsys,
):
    """``--provenance synthetic`` must NOT be silently upgraded to real —
    seeded/demo runs deliberately opt out of workflow-metrics eligibility.
    """
    tid = _make_completed_task("seed bootstrap card")
    payload = _run_dry_closeout(
        capsys,
        _closeout_namespace(tid, provenance="synthetic", substantiality="trivial"),
    )

    assert payload["provenance"] == "synthetic"
    assert payload["substantiality"] == "trivial"


def test_closeout_substantiality_unknown_override(
    kanban_home, stub_telemetry_script, capsys,
):
    """``--substantiality unknown`` is allowed for ambiguous tasks — that
    keeps the row out of KPIs without forcing the operator to fabricate
    a label."""
    tid = _make_completed_task()
    payload = _run_dry_closeout(
        capsys,
        _closeout_namespace(tid, substantiality="unknown"),
    )

    # provenance still defaults to real; only the explicit override flips.
    assert payload["provenance"] == "real"
    assert payload["substantiality"] == "unknown"
