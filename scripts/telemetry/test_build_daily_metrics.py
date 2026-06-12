#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from build_daily_metrics import compute_for_day
from common import ensure_initialized


def case_routing_coverage_gap_does_not_zero_task_telemetry_completeness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        telemetry_root = Path(tmp) / "telemetry"
        ensure_initialized(telemetry_root)
        conn = sqlite3.connect(telemetry_root / "events.db")
        try:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, opened_at, closed_at, status, surface, title,
                    user_goal_summary, owner_profile, task_type, outcome,
                    notes_json, provenance, substantiality, first_action_at,
                    latest_run_id, telemetry_complete, telemetry_gaps_json,
                    review_required
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "kanban:t_routed_without_correctness",
                    "2026-06-12T10:00:00+00:00",
                    "2026-06-12T10:30:00+00:00",
                    "completed",
                    "kanban",
                    "Completed routed task",
                    "Completed routed task",
                    "engineer",
                    "kanban",
                    "success",
                    "{}",
                    "real",
                    "substantial",
                    "2026-06-12T10:01:00+00:00",
                    "kanban_run:1",
                    1,
                    "[]",
                    0,
                ),
            )
            conn.execute(
                """
                INSERT INTO execution_runs(
                    task_id, run_id, profile, status, outcome, started_at, ended_at,
                    summary, error, metadata_json, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "kanban:t_routed_without_correctness",
                    "kanban_run:1",
                    "engineer",
                    "done",
                    "completed",
                    "2026-06-12T10:01:00+00:00",
                    "2026-06-12T10:30:00+00:00",
                    "done",
                    None,
                    "{}",
                    "test",
                ),
            )
            conn.execute(
                """
                INSERT INTO routing_decisions(
                    task_id, occurred_at, sequence_index, initial_owner,
                    decided_owner, was_initial_owner_correct, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "kanban:t_routed_without_correctness",
                    "2026-06-12T10:00:30+00:00",
                    0,
                    "default",
                    "engineer",
                    None,
                    "test",
                ),
            )

            payload = compute_for_day(conn, "2026-06-12")
        finally:
            conn.close()

    bench = payload["bench"]
    workflow = payload["workflows"]["kanban"]
    assert bench["tasks_completed"] == 1, bench
    assert bench["telemetry_completeness_rate"] == 1.0, bench
    assert workflow["telemetry_completeness_rate"] == 1.0, workflow
    assert bench["first_owner_routing_coverage_num"] == 0, bench
    assert bench["first_owner_routing_coverage_den"] == 1, bench
    assert bench["first_owner_routing_accuracy"] is None, bench


def main() -> int:
    case_routing_coverage_gap_does_not_zero_task_telemetry_completeness()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
