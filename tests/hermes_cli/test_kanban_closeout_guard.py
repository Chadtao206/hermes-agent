"""Regression tests for kanban worker closeout enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "sqlite")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
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


def test_cli_guard_genuine_no_closeout_routes_to_sticky_block(monkeypatch):
    # A worker that DID real work (real prose, no failure signal) but forgot the
    # terminal closeout is the genuine protocol violation → sticky block.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_genuine")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claimer:42")

    fake_store = MagicMock()
    fake_store.auto_block_unclosed_worker_turn.return_value = True

    monkeypatch.setattr(
        "hermes_cli.kanban.store.kanban_store",
        lambda board=None: fake_store,
    )

    from cli import _auto_block_unclosed_kanban_worker_turn

    assert _auto_block_unclosed_kanban_worker_turn(
        "Implemented the feature, added tests, pushed the branch — all green.",
        {"completed": True},
    ) is True

    fake_store.auto_block_unclosed_worker_turn.assert_called_once_with(
        "t_genuine",
        final_response="Implemented the feature, added tests, pushed the branch — all green.",
        expected_run_id=42,
        expected_claim_lock="claimer:42",
    )
    fake_store.record_task_failure.assert_not_called()
    fake_store.close.assert_called_once_with()


def test_cli_guard_infra_abort_routes_to_retryable_failure(monkeypatch):
    # A failed/aborted worker turn (the worker never did real work) must be
    # recorded as a RETRYABLE failure carrying the real cause — NOT the sticky,
    # misleading protocol_violation_clean_exit block.
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_backend_store")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claimer:42")

    fake_store = MagicMock()
    fake_store.record_task_failure.return_value = False  # re-queued (retry), not exhausted

    monkeypatch.setattr(
        "hermes_cli.kanban.store.kanban_store",
        lambda board=None: fake_store,
    )

    from cli import _auto_block_unclosed_kanban_worker_turn

    assert _auto_block_unclosed_kanban_worker_turn(
        "API call failed after 3 retries: Connection error.",
        {"completed": False, "failed": True},
    ) is False

    fake_store.record_task_failure.assert_called_once()
    args, kwargs = fake_store.record_task_failure.call_args
    assert args[0] == "t_backend_store"
    assert "Connection error" in args[1] and "aborted turn" in args[1]
    assert kwargs["outcome"] == "worker_clean_exit_no_progress"
    fake_store.auto_block_unclosed_worker_turn.assert_not_called()
    fake_store.close.assert_called_once_with()


@pytest.mark.parametrize("response,result", [
    ("", {"completed": False}),                                  # empty exit, no work done
    ("some partial text", {"error": "Operation interrupted during retry"}),  # error field set
    ("Claude session error: pool_full", {"completed": True}),    # session pool exhausted
    ("Error code: 401 - token_invalidated", {"completed": True}),  # provider auth (no failed flag)
    ("API call failed after 3 retries", {"partial": True}),      # partial/truncated
])
def test_cli_guard_classifies_infra_aborts_as_retryable(monkeypatch, response, result):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "c:1")

    fake_store = MagicMock()
    fake_store.record_task_failure.return_value = False
    monkeypatch.setattr(
        "hermes_cli.kanban.store.kanban_store", lambda board=None: fake_store
    )

    from cli import _auto_block_unclosed_kanban_worker_turn

    _auto_block_unclosed_kanban_worker_turn(response, result)
    fake_store.record_task_failure.assert_called_once()
    fake_store.auto_block_unclosed_worker_turn.assert_not_called()


def test_cli_guard_infra_abort_not_labeled_protocol_violation(kanban_home, monkeypatch):
    # End-to-end on the real (sqlite) store: an infra abort must NOT emit a
    # protocol_violation event and must NOT leave the task running.
    with kb.connect() as conn:
        tid, run_id, claim_lock = _claimed_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim_lock)

    from cli import _auto_block_unclosed_kanban_worker_turn

    _auto_block_unclosed_kanban_worker_turn(
        "", {"failed": True, "error": "Error code: 401 - token_invalidated"}
    )

    with kb.connect() as conn:
        pv = conn.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE task_id=? AND kind='protocol_violation'",
            (tid,),
        ).fetchone()
        assert pv["c"] == 0  # not mislabeled as a protocol violation
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status != "running"  # the retry-aware path acted (re-queued/failed), not left hanging
