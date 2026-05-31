"""Tests that the kanban CLI kanban_command() preamble skips sqlite init_db
under backend=postgres, preventing "could not initialize database" errors that
would otherwise trip the single-writer guard.
"""
from __future__ import annotations

import os
import argparse
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    "HERMES_PG_TEST_DSN" not in os.environ,
    reason="needs Postgres (set HERMES_PG_TEST_DSN)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(action: str, **kwargs):
    """Build a minimal argparse.Namespace that kanban_command() expects."""
    return argparse.Namespace(kanban_action=action, board=None, **kwargs)


def _list_args(**kwargs):
    """Return a Namespace suitable for the 'list' subcommand."""
    defaults = dict(
        assignee=None,
        mine=False,
        status=None,
        tenant=None,
        session=None,
        archived=False,
        sort=None,
        workflow_template_id=None,
        current_step_key=None,
        json=False,
    )
    defaults.update(kwargs)
    return _make_args("list", **defaults)


# ---------------------------------------------------------------------------
# Test 1: guard blocks sqlite error propagation under postgres backend
# ---------------------------------------------------------------------------

def test_sqlite_init_error_not_surfaced_under_postgres(monkeypatch, capsys):
    """When backend=postgres and init_db() would raise, the guard must prevent
    it from being called so no 'could not initialize database' appears in stderr
    and the command succeeds.

    We simulate the sqlite failure scenario by making init_db() raise; the
    guard (once implemented) prevents it from ever being called.
    """
    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    import hermes_cli.kanban.cli as cli
    import hermes_cli.kanban.store as store_mod

    # Use a unique board name and override the postgres board resolution so
    # both the test-setup store and the CLI's _make_store() see the same board.
    board = f"test_{uuid4().hex[:8]}"
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)

    # Patch resolve_backend BEFORE creating store so get_pool() in the CLI
    # will reuse the same DSN.
    monkeypatch.setattr(store_mod, "resolve_backend", lambda: "postgres")
    if hasattr(cli, "resolve_backend"):
        monkeypatch.setattr(cli, "resolve_backend", lambda: "postgres")

    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    # Also patch PostgresKanbanStore so board=None resolves to our board name,
    # matching what _make_store() will produce for the CLI's list command.
    import hermes_cli.kanban.store_postgres as pg_store_mod
    _orig_pg_init = pg_store_mod.PostgresKanbanStore.__init__

    def _patched_pg_init(self, board=None, pool=None):
        _orig_pg_init(self, board=board or os.environ.get("HERMES_KANBAN_BOARD") or "default", pool=pool)

    monkeypatch.setattr(pg_store_mod.PostgresKanbanStore, "__init__", _patched_pg_init)

    store = PostgresKanbanStore(board=board, pool=pool)
    try:
        store.create_task(title="pg list smoke", assignee="engineer")

        # Make init_db() raise — simulates the single-writer guard failure
        # that triggers under postgres. The preamble guard must prevent this
        # from being called at all when backend=postgres.
        monkeypatch.setattr(
            cli.kb, "init_db",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("single-writer guard: database is locked")
            ),
        )

        rc = cli.kanban_command(_list_args())

        captured = capsys.readouterr()
        assert "could not initialize database" not in captured.err, (
            f"sqlite error leaked under postgres backend: {captured.err!r}"
        )
        assert rc == 0, f"expected rc=0, got {rc}; stderr={captured.err!r}"
        assert "pg list smoke" in captured.out, (
            f"expected task title in output; got: {captured.out!r}"
        )
    finally:
        store.close()
        pool.close()


# ---------------------------------------------------------------------------
# Test 2: unit — kb.init_db() must NOT be called under postgres backend
# ---------------------------------------------------------------------------

def test_init_db_skipped_under_postgres(monkeypatch):
    """Direct assertion: sqlite kb.init_db() must not be called under postgres."""
    import hermes_cli.kanban.cli as cli
    import hermes_cli.kanban.store as store_mod

    # Patch resolve_backend on the store module (the authoritative location)
    # and also on the cli module if it has a module-level reference.
    monkeypatch.setattr(store_mod, "resolve_backend", lambda: "postgres")
    if hasattr(cli, "resolve_backend"):
        monkeypatch.setattr(cli, "resolve_backend", lambda: "postgres")

    called = {"init": False}
    monkeypatch.setattr(cli.kb, "init_db",
                        lambda *a, **k: called.__setitem__("init", True))

    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", os.environ["HERMES_PG_TEST_DSN"])

    board = f"test_{uuid4().hex[:8]}"
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)

    dsn = os.environ["HERMES_PG_TEST_DSN"]
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(dsn)
    pg_pool.ensure_schema(pool)
    store = PostgresKanbanStore(board=board, pool=pool)
    try:
        cli.kanban_command(_list_args())
    finally:
        store.close()
        pool.close()

    assert called["init"] is False, (
        "kb.init_db() was called under backend=postgres — the guard is missing"
    )
