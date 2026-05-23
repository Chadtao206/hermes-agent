from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_reconciler as rec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _kinds(result: dict) -> set[str]:
    return {action["kind"] for action in result["actions"]}


def _actions(result: dict, kind: str) -> list[dict]:
    return [action for action in result["actions"] if action["kind"] == kind]


def test_reconciler_reports_no_actions_for_healthy_board(kanban_home):
    result = rec.run_reconciler(now=1_700_000_000)

    assert result["mutation_applied"] is False
    assert result["actions"] == []
    assert result["ok"] is True


def test_reconciler_splits_dead_expired_and_stale_heartbeat(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="running", assignee="engineer")
        conn.execute(
            """
            UPDATE tasks
               SET status = 'running', worker_pid = ?, claim_lock = ?,
                   claim_expires = ?, last_heartbeat_at = ?, started_at = ?
             WHERE id = ?
            """,
            (99999999, "otherhost:claim", now - 60, now - 3600, now - 7200, task_id),
        )

    result = rec.run_reconciler(now=now)
    kinds = _kinds(result)

    assert "dead_running_candidate" in kinds
    assert "expired_claim_candidate" in kinds
    assert "stale_heartbeat_observed" in kinds
    assert _actions(result, "stale_heartbeat_observed")[0]["safe_to_apply"] is False
    # Cross-host dead candidates remain advisory in Phase 1A.
    assert _actions(result, "dead_running_candidate")[0]["safe_to_apply"] is False


def test_stale_heartbeat_only_is_advisory_not_safe_to_apply(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="running", assignee="engineer")
        conn.execute(
            """
            UPDATE tasks
               SET status = 'running', worker_pid = ?, claim_lock = ?,
                   claim_expires = ?, last_heartbeat_at = ?, started_at = ?
             WHERE id = ?
            """,
            (os.getpid(), "localhost:claim", now + 3600, now - 3600, now - 7200, task_id),
        )

    result = rec.run_reconciler(now=now)
    stale = _actions(result, "stale_heartbeat_observed")

    assert len(stale) == 1
    assert stale[0]["task_id"] == task_id
    assert stale[0]["safe_to_apply"] is False
    assert "dead_running_candidate" not in _kinds(result)


def test_blocked_with_completed_parents_is_decision_only(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        child = kb.create_task(conn, title="child", assignee="reviewer")
        kb.complete_task(conn, parent, summary="done")
        kb.block_task(conn, child, reason="waiting on remediation")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            (parent, child),
        )

    result = rec.run_reconciler(now=1_700_000_000)
    actions = _actions(result, "blocked_with_completed_parents_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == child
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["signature"].startswith(
        f"blocked_with_completed_parents_decision:{child}:"
    )


def test_scheduled_with_completed_parents_is_decision_only(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        kb.complete_task(conn, parent, summary="done")
        child = kb.create_task(
            conn,
            title="scheduled child",
            assignee="reviewer",
            parents=[parent],
            initial_status="scheduled",
        )
        conn.execute(
            "UPDATE tasks SET created_at = ?, started_at = ? WHERE id = ?",
            (now - 3600, now - 1800, child),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    actions = _actions(result, "scheduled_with_completed_parents_decision")

    assert len(actions) == 1
    assert actions[0]["task_id"] == child
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["details"]["parent_count"] == 1
    assert actions[0]["details"]["age_seconds"] == 3600
    assert actions[0]["signature"].startswith(
        f"scheduled_with_completed_parents_decision:{child}:"
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "scheduled"


def test_scheduled_with_completed_parents_respects_age_and_active_run_guards(kanban_home):
    now = 1_700_000_000
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        kb.complete_task(conn, parent, summary="done")
        young = kb.create_task(
            conn,
            title="young scheduled child",
            assignee="reviewer",
            parents=[parent],
            initial_status="scheduled",
        )
        active = kb.create_task(
            conn,
            title="active scheduled child",
            assignee="reviewer",
            parents=[parent],
            initial_status="scheduled",
        )
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 10, young))
        conn.execute(
            "UPDATE tasks SET created_at = ?, current_run_id = ? WHERE id = ?",
            (now - 3600, 12345, active),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)

    assert _actions(result, "scheduled_with_completed_parents_decision") == []


def test_stale_run_metadata_is_reported_without_task_mutation(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="done task", assignee="engineer")
        kb.complete_task(conn, task_id, summary="done")
        run_id = conn.execute(
            """
            INSERT INTO task_runs(task_id, profile, status, worker_pid, started_at)
            VALUES (?, 'engineer', 'running', 99999999, ?)
            """,
            (task_id, 1),
        ).lastrowid

    result = rec.run_reconciler(now=1_700_000_000)
    actions = _actions(result, "stale_run_metadata")

    assert len(actions) == 1
    assert actions[0]["details"]["run_id"] == run_id
    assert actions[0]["safe_to_apply"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"


def test_old_ready_and_review_spawnable_classification(kanban_home, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(rec, "_profile_spawnable", lambda profile: profile == "engineer")
    with kb.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="engineer")
        blocked_ready = kb.create_task(conn, title="ready nonspawn", assignee="not-a-profile")
        review = kb.create_task(conn, title="review", assignee="engineer")
        review_nonspawn = kb.create_task(conn, title="review nonspawn", assignee="not-a-profile")
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id IN (?, ?)",
            (now - 3600, ready, blocked_ready),
        )
        conn.execute(
            "UPDATE tasks SET status = 'review', created_at = ? WHERE id IN (?, ?)",
            (now - 3600, review, review_nonspawn),
        )

    result = rec.run_reconciler(now=now, ready_age_seconds=60)
    kinds = _kinds(result)

    assert "old_ready_spawnable" in kinds
    assert "old_ready_nonspawnable" in kinds
    assert "old_review_spawnable" in kinds
    assert "old_review_nonspawnable" in kinds


def test_review_skill_provenance_missing_is_diagnostic_only(kanban_home, monkeypatch):
    monkeypatch.setattr(rec, "_has_sdlc_review_skill", lambda: False)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review", assignee="reviewer")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))

    result = rec.run_reconciler(now=1_700_000_000)
    actions = _actions(result, "review_skill_provenance_missing")

    assert len(actions) == 1
    assert actions[0]["task_id"] is None
    assert actions[0]["safe_to_apply"] is False
    assert actions[0]["details"]["skill"] == "sdlc-review"


def test_reconcile_cli_json_is_read_only(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        child = kb.create_task(conn, title="child", assignee="reviewer")
        kb.complete_task(conn, parent, summary="done")
        kb.block_task(conn, child, reason="waiting")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            (parent, child),
        )

    db_path = kb.kanban_db_path()
    # Force setup writes into the main DB, then remove absent sidecars so this
    # regression proves reconcile itself does not create WAL/SHM files.
    with kb.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for sidecar in (db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    wal_before = db_path.with_name(db_path.name + "-wal").exists()
    shm_before = db_path.with_name(db_path.name + "-shm").exists()

    out = kc.run_slash("reconcile --json")
    payload = json.loads(out)

    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert before == after
    assert db_path.with_name(db_path.name + "-wal").exists() == wal_before
    assert db_path.with_name(db_path.name + "-shm").exists() == shm_before
    assert payload["mutation_applied"] is False
    assert "blocked_with_completed_parents_decision" in _kinds(payload)


def test_reconcile_cli_text_groups_actions(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ready", assignee="missing-profile")
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 3600, task_id))

    out = kc.run_slash("reconcile --ready-age-seconds 60")

    assert "Kanban reconcile:" in out
    assert "Summary:" in out
    assert "old_ready_nonspawnable" in out
