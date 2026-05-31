"""Tests that `hermes kanban show <task>` reads from the Postgres store
when backend=postgres.
"""
from __future__ import annotations

import argparse
import json
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    "HERMES_PG_TEST_DSN" not in os.environ,
    reason="needs Postgres (set HERMES_PG_TEST_DSN)",
)


def _show_args(task_id: str, *, json_out: bool = False,
               state_type=None, state_name=None):
    """Build a minimal argparse.Namespace for the 'show' subcommand."""
    return argparse.Namespace(
        kanban_action="show",
        board=None,
        task_id=task_id,
        json=json_out,
        state_type=state_type,
        state_name=state_name,
    )


def _patch_pg_store_board_from_env(monkeypatch):
    """Patch PostgresKanbanStore.__init__ to honour HERMES_KANBAN_BOARD env var
    when board=None, matching the same test-isolation pattern used in
    test_cli_pg_init.py and other PG CLI tests."""
    import hermes_cli.kanban.store_postgres as pg_store_mod
    _orig_init = pg_store_mod.PostgresKanbanStore.__init__

    def _patched_init(self, board=None, pool=None):
        resolved = board or os.environ.get("HERMES_KANBAN_BOARD") or "default"
        _orig_init(self, board=resolved, pool=pool)

    monkeypatch.setattr(pg_store_mod.PostgresKanbanStore, "__init__", _patched_init)


def test_cmd_show_json_reads_postgres(monkeypatch, capsys):
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    import hermes_cli.kanban.cli as cli
    import hermes_cli.kanban.store as store_mod

    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=board, pool=pool)
    try:
        tid = store.create_task(title="pg cli show", assignee="engineer", body="b")

        monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
        monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
        # Patch resolve_backend so both the cli module reference and store
        # module reference return "postgres" — ensures the guard fires.
        monkeypatch.setattr(store_mod, "resolve_backend", lambda: "postgres")
        if hasattr(cli, "resolve_backend"):
            monkeypatch.setattr(cli, "resolve_backend", lambda: "postgres")
        # Patch PostgresKanbanStore.__init__ so board=None resolves to the
        # test board from HERMES_KANBAN_BOARD, matching test_cli_pg_init.py.
        _patch_pg_store_board_from_env(monkeypatch)

        rc = cli.kanban_command(_show_args(tid, json_out=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["task"]["id"] == tid
        assert out["task"]["title"] == "pg cli show"
    finally:
        store.close()
        pool.close()


def test_cmd_show_missing_task_postgres(monkeypatch, capsys):
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    import hermes_cli.kanban.cli as cli
    import hermes_cli.kanban.store as store_mod

    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=board, pool=pool)
    try:
        monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
        monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
        monkeypatch.setattr(store_mod, "resolve_backend", lambda: "postgres")
        if hasattr(cli, "resolve_backend"):
            monkeypatch.setattr(cli, "resolve_backend", lambda: "postgres")
        _patch_pg_store_board_from_env(monkeypatch)

        rc = cli.kanban_command(_show_args("t_nope"))
        assert rc == 1
        assert "no such task" in capsys.readouterr().err
    finally:
        store.close()
        pool.close()
