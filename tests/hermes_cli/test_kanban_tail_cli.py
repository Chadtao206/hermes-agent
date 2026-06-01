"""Regression: `hermes kanban tail` routes through the store under Postgres
(does not open the frozen sqlite kanban.db)."""
from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_cli import kanban_db as kb


def test_tail_reads_store_under_postgres(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")

    # If the sqlite path is taken under postgres, fail loudly.
    def _boom(*a, **k):
        raise AssertionError("tail opened a direct sqlite connection under postgres")

    monkeypatch.setattr(kb, "connect_closing", _boom)

    ev = SimpleNamespace(id=1, kind="created", payload="{}", created_at=1700000000)
    fake_store = MagicMock()
    fake_store.list_events.return_value = [ev]
    # _make_store() looks up kanban_store on the hermes_cli.kanban package at call time.
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: fake_store, raising=False)

    # Break the `while True` poll loop after the first iteration.
    import hermes_cli.kanban.cli as cli

    def _stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", _stop)

    rc = cli._cmd_tail(argparse.Namespace(task_id="t_x", interval=0.1))
    assert rc == 0
    out = capsys.readouterr().out
    assert "created" in out  # the event was printed (read via the store)
    fake_store.list_events.assert_called_with("t_x")
    fake_store.close.assert_called()
