"""Tests for kanban_show / kanban_list reading from Postgres under backend=postgres.

These tests use a real Postgres test DB (HERMES_PG_TEST_DSN env var must be set).
They are skipped in CI / offline environments where PG is unavailable.
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    "HERMES_PG_TEST_DSN" not in os.environ,
    reason="needs PG: set HERMES_PG_TEST_DSN",
)


def _pg_store_env(monkeypatch, dsn):
    from uuid import uuid4
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore

    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    return PostgresKanbanStore(board=board, pool=pool), pool, board


def test_kanban_show_reads_postgres(monkeypatch):
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from tools import kanban_tools

    store, pool, board = _pg_store_env(monkeypatch, dsn)
    try:
        tid = store.create_task(title="pg show", assignee="engineer", body="b")
        out = json.loads(kanban_tools._handle_show({"task_id": tid, "board": board}))
        assert out["task"]["id"] == tid
        assert out["task"]["title"] == "pg show"
        assert "worker_context" in out and f"# Kanban task {tid}" in out["worker_context"]
    finally:
        store.close()
        pool.close()


def test_kanban_show_returns_comments_events_runs(monkeypatch):
    """_handle_show under postgres returns comments / events / runs sections."""
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from tools import kanban_tools

    store, pool, board = _pg_store_env(monkeypatch, dsn)
    try:
        tid = store.create_task(title="show detail", assignee="engineer", body="detail body")
        store.add_comment(task_id=tid, author="tester", body="a comment")
        out = json.loads(kanban_tools._handle_show({"task_id": tid, "board": board}))
        assert isinstance(out["comments"], list)
        assert isinstance(out["events"], list)
        assert isinstance(out["runs"], list)
        assert isinstance(out["parents"], list)
        assert isinstance(out["children"], list)
        assert isinstance(out["rollup_parents"], list)
        assert isinstance(out["rollup_children"], list)
        # The comment we added should appear
        bodies = [c["body"] for c in out["comments"]]
        assert "a comment" in bodies
    finally:
        store.close()
        pool.close()


def test_kanban_show_not_found_under_postgres(monkeypatch):
    """_handle_show under postgres returns tool_error for unknown task id."""
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from tools import kanban_tools

    store, pool, board = _pg_store_env(monkeypatch, dsn)
    try:
        out = json.loads(
            kanban_tools._handle_show({"task_id": "nonexistent-id-xyz", "board": board})
        )
        assert out.get("error") or "not found" in str(out).lower()
    finally:
        store.close()
        pool.close()


def test_kanban_list_reads_postgres(monkeypatch):
    """_handle_list under postgres returns tasks from Postgres, not sqlite."""
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from tools import kanban_tools

    store, pool, board = _pg_store_env(monkeypatch, dsn)
    # Unset HERMES_KANBAN_TASK so orchestrator guard passes
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    try:
        tid = store.create_task(
            title="pg list task", assignee="engineer", body="list body",
        )
        # PG create_task (no parents) starts tasks as "ready"; list without
        # status filter so both "ready" and "running" tasks are included.
        out = json.loads(kanban_tools._handle_list({"board": board}))
        assert "tasks" in out
        ids = [t["id"] for t in out["tasks"]]
        assert tid in ids, f"Expected {tid} in {ids}"
        # Verify the summary dict has the expected keys
        task_row = next(t for t in out["tasks"] if t["id"] == tid)
        for key in (
            "id", "title", "assignee", "status", "priority", "tenant",
            "workspace_kind", "workspace_path", "created_by", "created_at",
            "started_at", "completed_at", "current_run_id", "model_override",
            "parents", "children", "rollup_children",
            "parent_count", "child_count", "rollup_child_count",
        ):
            assert key in task_row, f"Missing key {key!r} in task summary"
    finally:
        store.close()
        pool.close()
