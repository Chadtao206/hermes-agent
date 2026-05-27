#!/usr/bin/env python3
"""Self-contained verification for review-block telemetry normalization.

Run manually:
    python3 /Users/ctao/.hermes/hermes-agent/scripts/telemetry/test_normalize_review_block_events.py

Exits non-zero on failed assertions; prints "OK" on success.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import normalize_review_block_events as normalizer
from common import events_connection


def _insert_event(conn: sqlite3.Connection, task_id: str, at: str, event_type: str, payload: dict | str | None) -> None:
    if isinstance(payload, dict):
        payload_json = json.dumps(payload, sort_keys=True)
    else:
        payload_json = payload
    conn.execute(
        "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json, provenance) VALUES (?, ?, ?, 'engineer', ?, 'real')",
        (task_id, at, event_type, payload_json),
    )


def _count(conn: sqlite3.Connection, task_id: str, event_type: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND event_type = ?",
        (task_id, event_type),
    ).fetchone()[0])


def case_review_required_block_gets_explicit_pair(telemetry_root: Path) -> None:
    task_id = "kanban:t_review_required"
    with events_connection(telemetry_root) as conn:
        _insert_event(
            conn,
            task_id,
            "2026-05-25T10:00:00+00:00",
            "blocked",
            {"reason": "review-required: implementation ready for Boris"},
        )
        _insert_event(
            conn,
            task_id,
            "2026-05-25T10:30:00+00:00",
            "unblocked",
            {"status": "ready"},
        )

    first = normalizer.normalize(telemetry_root)
    second = normalizer.normalize(telemetry_root)
    assert first["review_blocked_inserted"] == 1, first
    assert first["review_unblocked_inserted"] == 1, first
    assert second == {
        "review_blocked_inserted": 0,
        "review_unblocked_inserted": 0,
        "generic_unblocked_inserted": 0,
    }, second

    with sqlite3.connect(telemetry_root / "events.db") as conn:
        assert _count(conn, task_id, "review_blocked") == 1
        assert _count(conn, task_id, "review_unblocked") == 1


def case_direct_review_block_gets_unblocked_even_with_malformed_json(telemetry_root: Path) -> None:
    task_id = "jira:HSS-0000"
    with events_connection(telemetry_root) as conn:
        _insert_event(
            conn,
            task_id,
            "2026-05-25T11:00:00+00:00",
            "review_blocked",
            {"finding": "Boris blocked on test evidence"},
        )
        # Regression: production telemetry can contain legacy raw payloads that
        # are not valid JSON; normalizer queries must ignore rather than fail.
        _insert_event(
            conn,
            task_id,
            "2026-05-25T11:10:00+00:00",
            "unblocked",
            "not-json",
        )
        _insert_event(
            conn,
            task_id,
            "2026-05-25T11:20:00+00:00",
            "remediation_pushed",
            {"summary": "tests added"},
        )

    result = normalizer.normalize(telemetry_root)
    assert result["review_unblocked_inserted"] == 1, result
    # Existing generic unblocked means no synthetic generic duplicate is needed.
    assert result["generic_unblocked_inserted"] == 0, result

    with sqlite3.connect(telemetry_root / "events.db") as conn:
        assert _count(conn, task_id, "review_unblocked") == 1


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        telemetry_root = Path(tmp) / "telemetry"
        case_review_required_block_gets_explicit_pair(telemetry_root)
        case_direct_review_block_gets_unblocked_even_with_malformed_json(telemetry_root)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
