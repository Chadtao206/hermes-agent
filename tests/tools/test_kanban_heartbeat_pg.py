"""Tests for worker heartbeat writes going through the store under backend=postgres.

These verify that both heartbeat call sites (_handle_heartbeat and
heartbeat_current_worker_from_env) update tasks.last_heartbeat_at in PG
rather than writing to the frozen sqlite DB (finding I-1).
"""
from __future__ import annotations

import json as _json
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    "HERMES_PG_TEST_DSN" not in os.environ,
    reason="needs Postgres",
)


def _running_task(store):
    """Create a task in 'ready', claim it (→ running + current_run_id), return (tid, run_id).

    claim_task requires status='ready' AND claim_lock IS NULL.  create_task
    always inserts as 'ready' (no parents, not triage/blocked/scheduled), so
    we call claim_task immediately after.
    """
    tid = store.create_task(title="hb task", assignee="engineer")
    result = store.claim_task(tid)  # sets status='running', opens a task_run, sets current_run_id
    if result is None:
        raise RuntimeError(f"claim_task returned None for {tid} – task may not be in ready state")
    t = store.get_task(tid)
    return tid, t.current_run_id


def _pg_env(monkeypatch, dsn):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore

    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=board, pool=pool)
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    return store, pool, board


def test_handle_heartbeat_writes_to_postgres(monkeypatch):
    """_handle_heartbeat under backend=postgres refreshes last_heartbeat_at in PG."""
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from tools import kanban_tools

    store, pool, board = _pg_env(monkeypatch, dsn)
    try:
        tid, run_id = _running_task(store)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        # Don't set HERMES_KANBAN_RUN_ID so heartbeat_worker matches on
        # status='running' alone — avoids any run_id type-mismatch issues.
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

        res = _json.loads(kanban_tools._handle_heartbeat({"task_id": tid, "board": board}))
        assert res.get("ok") is True, f"Unexpected result: {res}"

        # last_heartbeat_at should now be set in PG (proves the store path ran,
        # not sqlite which wouldn't have this task at all).
        t = store.get_task(tid)
        assert t is not None
        assert t.last_heartbeat_at is not None and t.last_heartbeat_at > 0, (
            f"last_heartbeat_at not set: {t.last_heartbeat_at}"
        )
    finally:
        store.close()
        pool.close()


def test_auto_heartbeat_bridge_writes_to_postgres(monkeypatch):
    """heartbeat_current_worker_from_env under backend=postgres updates last_heartbeat_at in PG."""
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from tools import kanban_tools

    store, pool, board = _pg_env(monkeypatch, dsn)
    try:
        tid, run_id = _running_task(store)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        # Don't set run_id — heartbeat_worker will match on status='running' alone.
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

        # Reset the module rate-limit so the bridge actually attempts a write.
        monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)

        result = kanban_tools.heartbeat_current_worker_from_env()
        assert result is True, "heartbeat_current_worker_from_env should return True when write attempted"

        t = store.get_task(tid)
        assert t is not None
        assert t.last_heartbeat_at is not None and t.last_heartbeat_at > 0, (
            f"last_heartbeat_at not set: {t.last_heartbeat_at}"
        )
    finally:
        store.close()
        pool.close()
