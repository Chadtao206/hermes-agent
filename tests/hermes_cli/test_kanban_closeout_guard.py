"""Regression tests for kanban worker closeout enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claimed_task(conn):
    tid = kb.create_task(conn, title="worker forgot closeout", assignee="engineer")
    claimed = kb.claim_task(conn, tid, claimer="host:worker")
    assert claimed is not None
    assert claimed.current_run_id is not None
    assert claimed.claim_lock is not None
    return tid, int(claimed.current_run_id), claimed.claim_lock


def test_auto_block_unclosed_worker_turn_blocks_running_task(kanban_home):
    with kb.connect() as conn:
        tid, run_id, claim_lock = _claimed_task(conn)

        ok = kb.auto_block_unclosed_worker_turn(
            conn,
            tid,
            final_response="Implementation done, but I forgot the kanban tool call.",
            expected_run_id=run_id,
            expected_claim_lock=claim_lock,
        )

        assert ok is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"

        run = conn.execute(
            "SELECT outcome, status, summary, metadata FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["outcome"] == "blocked"
        assert run["status"] == "blocked"
        assert "forgot the kanban tool call" in (run["summary"] or "")
        metadata = json.loads(run["metadata"])
        assert metadata["failure_class"] == kb.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT
        assert metadata["closeout_guard"] == "cli_final_response_auto_block"

        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='protocol_violation' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["source"] == "cli_closeout_guard"
        assert payload["failure_class"] == kb.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT


def test_auto_block_unclosed_worker_turn_respects_run_and_claim(kanban_home):
    with kb.connect() as conn:
        tid, run_id, claim_lock = _claimed_task(conn)

        assert kb.auto_block_unclosed_worker_turn(
            conn,
            tid,
            final_response="wrong run",
            expected_run_id=run_id + 1,
            expected_claim_lock=claim_lock,
        ) is False
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"

        assert kb.auto_block_unclosed_worker_turn(
            conn,
            tid,
            final_response="wrong claim",
            expected_run_id=run_id,
            expected_claim_lock="other:worker",
        ) is False
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"


def test_cli_guard_auto_blocks_from_kanban_worker_env(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid, run_id, claim_lock = _claimed_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim_lock)

    from cli import _auto_block_unclosed_kanban_worker_turn

    assert _auto_block_unclosed_kanban_worker_turn(
        "Final prose without terminal closeout.",
        {"completed": True},
    ) is True

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
