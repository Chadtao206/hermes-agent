from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_metrics as km


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed_runs(now: int) -> tuple[str, str, str]:
    with kb.connect() as conn:
        completed = kb.create_task(conn, title="completed", assignee="engineer")
        crashed = kb.create_task(conn, title="crashed", assignee="engineer")
        reclaimed = kb.create_task(conn, title="reclaimed", assignee="reviewer")
        conn.execute(
            """
            INSERT INTO task_runs
                (task_id, profile, status, outcome, started_at, ended_at, summary, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                completed,
                "engineer",
                "done",
                "completed",
                now - 120,
                now - 60,
                "done",
                json.dumps({"pull_request_head_sha": "abcdef1234567890"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO task_runs
                (task_id, profile, status, outcome, started_at, ended_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (crashed, "engineer", "crashed", "crashed", now - 100, now - 80, "boom"),
        )
        conn.execute(
            """
            INSERT INTO task_runs
                (task_id, profile, status, outcome, started_at, ended_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reclaimed,
                "reviewer",
                "reclaimed",
                "reclaimed",
                now - 90,
                now - 30,
                "manual reclaim",
            ),
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (now - 60, completed),
        )
        conn.execute(
            "UPDATE tasks SET status = 'archived', consecutive_failures = 2 WHERE id = ?",
            (crashed,),
        )
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?",
            (reclaimed,),
        )
        for task_id, kind in (
            (crashed, "crashed"),
            (crashed, "gave_up"),
            (reclaimed, "protocol_violation"),
        ):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, kind, None, now - 50),
            )
    return completed, crashed, reclaimed


def test_collect_metrics_reports_reliability_windows(kanban_home):
    now = 1_700_000_000
    completed, crashed, reclaimed = _seed_runs(now)

    result = km.collect_metrics(now=now, since_epoch=now - 180, ready_age_seconds=60)
    by_label = {window["label"]: window for window in result["windows"]}

    assert result["ok"] is True
    assert result["health"]["doctor_ok"] is True
    assert result["health"]["reconcile_action_count"] == 0
    assert result["current_state"]["max_consecutive_failures"] == 2
    assert by_label["24h"]["total_runs"] == 3
    assert by_label["24h"]["tasks_attempted"] == 3
    assert by_label["24h"]["avg_attempts_per_task"] == 1.0
    assert by_label["24h"]["max_attempts_per_task"] == 1
    assert by_label["24h"]["completion_count"] == 1
    assert by_label["24h"]["failure_or_reclaim_count"] == 2
    assert by_label["24h"]["failure_or_reclaim_rate"] == 0.6667
    assert by_label["24h"]["failure_event_counts"] == {
        "crashed": 1,
        "gave_up": 1,
        "protocol_violation": 1,
    }
    assert {item["key"] for item in by_label["24h"]["task_attempt_hotspots"]} == {
        completed,
        crashed,
        reclaimed,
    }
    assert by_label["since"]["total_runs"] == 3


def test_metrics_snapshot_persists_to_sidecar_db(kanban_home):
    now = 1_700_000_000
    _seed_runs(now)
    snapshot_db = kanban_home / "metrics-sidecar.db"

    result = km.collect_metrics(
        now=now,
        write_snapshot=True,
        snapshot_db=snapshot_db,
        ready_age_seconds=60,
    )

    assert result["persisted_snapshot"]["id"] == 1
    assert result["persisted_snapshot"]["db_path"] == str(snapshot_db)
    with sqlite3.connect(snapshot_db) as conn:
        row = conn.execute(
            "SELECT board, captured_at, payload_json FROM kanban_metrics_snapshots"
        ).fetchone()
    assert row[0] == "default"
    assert row[1] == now
    payload = json.loads(row[2])
    assert payload["windows"][0]["total_runs"] == 3
    assert "persisted_snapshot" not in payload


def test_kanban_metrics_cli_json_and_snapshot(kanban_home, capsys):
    now = 1_700_000_000
    _seed_runs(now)
    snapshot_db = kanban_home / "cli-metrics.db"

    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args([
        "kanban",
        "metrics",
        "--json",
        "--write-snapshot",
        "--snapshot-db",
        str(snapshot_db),
    ])
    rc = kanban_cli.kanban_command(ns)
    out = capsys.readouterr().out
    data = json.loads(out)

    assert rc == 0
    assert data["ok"] is True
    assert data["persisted_snapshot"]["db_path"] == str(snapshot_db)
    assert snapshot_db.exists()
