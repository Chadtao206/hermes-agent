#!/usr/bin/env python3
"""Self-contained verification for kanban-closeout provenance/substantiality
persistence in ``log_task_closeout.py``.

No pytest harness exists under ``$HERMES_HOME/scripts/telemetry/``; this
script runs the actual closeout script as a subprocess against a tmp
telemetry root and asserts that the DB row + JSONL stream carry the
expected workflow-metrics eligibility labels.

Run manually:
    python3 /Users/ctao/.hermes/scripts/telemetry/test_log_task_closeout_provenance.py

Exits non-zero on the first failed assertion; prints "OK" on success.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
LOG_SCRIPT = THIS_DIR / "log_task_closeout.py"


def _query_task_row(events_db: Path, task_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(events_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT provenance, substantiality, telemetry_complete, telemetry_gaps_json "
            "FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(f"tasks row missing for {task_id}")
    return row


def _read_jsonl_record(jsonl_path: Path, task_id: str) -> dict:
    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("task_id") == task_id:
                return rec
    raise AssertionError(f"JSONL closeout record missing for {task_id}")


def _run_closeout(telemetry_root: Path, payload: dict) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(LOG_SCRIPT),
            "--telemetry-root",
            str(telemetry_root),
            "--json",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"log_task_closeout exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def case_kanban_real_substantial(telemetry_root: Path) -> None:
    """Default kanban closeout payload (mirrors hermes_cli/kanban.py) must
    persist real/substantial and leave the row telemetry_complete=1 with
    no gaps."""
    task_id = "kanban:t_test_real_substantial"
    payload = {
        "task_id": task_id,
        "title": "verify real/substantial path",
        "user_goal_summary": "kanban closeout default",
        "owner_profile": "andrej",
        "surface": "kanban",
        "status": "completed",
        "outcome": "success",
        "verification_strength": "moderate",
        "opened_at": "2026-05-20T00:00:00+00:00",
        "closed_at": "2026-05-20T01:00:00+00:00",
        "task_type": "kanban",
        "kanban_task_id": "t_test_real_substantial",
        "correct_owner": True,
        "user_corrected": False,
        "correction_state": "none",
        "learning_artifact_state": "none",
        "provenance": "real",
        "substantiality": "substantial",
        "notes": {
            "initial_owner": "andrej",
            "current_owner": "andrej",
            "kanban_status": "done",
            "kanban_run_count": 1,
        },
    }
    _run_closeout(telemetry_root, payload)

    row = _query_task_row(telemetry_root / "events.db", task_id)
    assert row["provenance"] == "real", row["provenance"]
    assert row["substantiality"] == "substantial", row["substantiality"]
    assert row["telemetry_complete"] == 1, row["telemetry_complete"]
    gaps = json.loads(row["telemetry_gaps_json"] or "[]")
    assert gaps == [], f"expected no gaps, got {gaps}"

    jsonl = telemetry_root / "events.jsonl"
    record = _read_jsonl_record(jsonl, task_id)
    assert record["provenance"] == "real"
    assert record["substantiality"] == "substantial"


def case_missing_provenance_defaults_unknown_and_gaps(telemetry_root: Path) -> None:
    """A non-kanban caller that omits provenance must default to "unknown"
    in both the DB row and the gaps list (so workflow-metrics drivers can
    see the row needs labelling)."""
    task_id = "generic:t_test_unknown"
    payload = {
        "task_id": task_id,
        "title": "verify unknown defaulting",
        "user_goal_summary": "generic caller skips provenance",
        "owner_profile": "andrej",
        "surface": "cli",
        "status": "completed",
        "outcome": "success",
        "verification_strength": "moderate",
        "opened_at": "2026-05-20T00:00:00+00:00",
        "closed_at": "2026-05-20T01:00:00+00:00",
        "correction_state": "none",
        "learning_artifact_state": "none",
        "notes": {
            "initial_owner": "andrej",
            "current_owner": "andrej",
        },
    }
    _run_closeout(telemetry_root, payload)

    row = _query_task_row(telemetry_root / "events.db", task_id)
    assert row["provenance"] == "unknown", row["provenance"]
    assert row["substantiality"] == "unknown", row["substantiality"]
    assert row["telemetry_complete"] == 0
    gaps = json.loads(row["telemetry_gaps_json"] or "[]")
    assert "provenance" in gaps and "substantiality" in gaps, gaps


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        telemetry_root = Path(tmp) / "telemetry"
        case_kanban_real_substantial(telemetry_root)
        case_missing_provenance_defaults_unknown_and_gaps(telemetry_root)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
