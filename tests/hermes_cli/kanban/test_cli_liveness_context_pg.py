"""Tests that `hermes kanban liveness` and `hermes kanban context` read from
Postgres under backend=postgres.
"""
from __future__ import annotations

import argparse
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    "HERMES_PG_TEST_DSN" not in os.environ,
    reason="needs Postgres (set HERMES_PG_TEST_DSN)",
)


def _liveness_args(*, json_out: bool = False, board: str | None = None):
    """Build a minimal argparse.Namespace for the 'liveness' subcommand."""
    return argparse.Namespace(
        kanban_action="liveness",
        board=board,
        json=json_out,
    )


def _context_args(task_id: str, *, board: str | None = None):
    """Build a minimal argparse.Namespace for the 'context' subcommand."""
    return argparse.Namespace(
        kanban_action="context",
        board=board,
        task_id=task_id,
    )


def _patch_pg_store_board_from_env(monkeypatch):
    """Patch PostgresKanbanStore.__init__ to honour HERMES_KANBAN_BOARD env var
    when board=None, matching the same test-isolation pattern used in
    test_cli_show_pg.py."""
    import hermes_cli.kanban.store_postgres as pg_store_mod
    _orig_init = pg_store_mod.PostgresKanbanStore.__init__

    def _patched_init(self, board=None, pool=None):
        resolved = board or os.environ.get("HERMES_KANBAN_BOARD") or "default"
        _orig_init(self, board=resolved, pool=pool)

    monkeypatch.setattr(pg_store_mod.PostgresKanbanStore, "__init__", _patched_init)


def _pg_setup(monkeypatch, dsn):
    """Create an isolated test board on Postgres and patch env + modules."""
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    import hermes_cli.kanban.store as store_mod
    import hermes_cli.kanban.cli as cli

    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=board, pool=pool)

    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setattr(store_mod, "resolve_backend", lambda: "postgres")
    if hasattr(cli, "resolve_backend"):
        monkeypatch.setattr(cli, "resolve_backend", lambda: "postgres")
    _patch_pg_store_board_from_env(monkeypatch)

    return store, pool, board


def test_cmd_liveness_pg(monkeypatch, capsys):
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    import json as _json
    import hermes_cli.kanban.cli as cli
    from hermes_cli.kanban import pg_pool

    store, pool, board = _pg_setup(monkeypatch, dsn)
    # Track whether get_pool() was called (proves the PG branch was taken).
    pg_pool_calls = []
    real_get_pool = pg_pool.get_pool

    def _spy_get_pool():
        pg_pool_calls.append(True)
        return real_get_pool()

    monkeypatch.setattr(pg_pool, "get_pool", _spy_get_pool)
    try:
        store.create_task(title="x", assignee="engineer")
        rc = cli.kanban_command(_liveness_args(json_out=True))
        assert rc == 0
        out = capsys.readouterr().out
        data = _json.loads(out)
        assert "oldest_ready_age_seconds" in data
        assert isinstance(data["oldest_ready_age_seconds"], int)
        # Confirm that the PG branch was taken, not the sqlite fallback.
        assert pg_pool_calls, "expected pg_pool.get_pool() to be called under backend=postgres"
    finally:
        store.close()
        pool.close()


def test_cmd_context_pg(monkeypatch, capsys):
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    import hermes_cli.kanban.cli as cli
    import hermes_cli.kanban.store_postgres as pg_store_mod

    store, pool, board = _pg_setup(monkeypatch, dsn)

    # Install a spy on PostgresKanbanStore.build_worker_context to prove the PG
    # branch was actually taken (not the sqlite fallback).
    calls = {"n": 0}
    _orig_bwc = pg_store_mod.PostgresKanbanStore.build_worker_context

    def _spy(self, task_id):
        calls["n"] += 1
        return _orig_bwc(self, task_id)

    monkeypatch.setattr(pg_store_mod.PostgresKanbanStore, "build_worker_context", _spy)

    try:
        tid = store.create_task(title="ctx", assignee="engineer", body="b")
        rc = cli.kanban_command(_context_args(tid))
        assert rc == 0
        assert f"# Kanban task {tid}" in capsys.readouterr().out
        # Confirm the PG store's build_worker_context was called, not the sqlite path.
        assert calls["n"] >= 1, "expected PostgresKanbanStore.build_worker_context to be called under backend=postgres"
    finally:
        store.close()
        pool.close()
