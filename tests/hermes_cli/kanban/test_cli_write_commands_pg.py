"""Tier-1 CLI write commands route through the backend-aware store (no direct
writable kb.connect()). Verified under the Postgres backend, incl. that they
never open a direct sqlite connection (would raise DirectWriteForbidden live)."""
import argparse
import uuid
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore
from hermes_cli.kanban import cli as kcli


@pytest.fixture
def pg(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    # Use board="default" so _make_store() (which calls PostgresKanbanStore(board=None)
    # → board="default") looks up tasks on the same board as this fixture's store.
    board = "default"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, board
    finally:
        s.close(); pool.close()


def _forbid_direct_connect(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("direct kb.connect() called — should route via the store")
    monkeypatch.setattr(kb, "connect", _boom)


def test_profile_subs_add_routes_through_store(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="needs a sub")
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, profile="default", name="op-sub", json=False)
    rc = kcli._cmd_profile_subs_add(ns)
    assert rc == 0
    subs = s.list_profile_event_subs(task_id=tid, profile="default", enabled_only=False)
    assert any((x.get("name") or "") == "op-sub" for x in subs)


def test_wake_arm_routes_through_store(pg, monkeypatch):
    s, board = pg
    tid = s.create_task(title="orchestrator root")
    monkeypatch.setattr(kb, "list_profiles_on_disk", lambda: ["default"])
    monkeypatch.delenv("HERMES_KANBAN_EVENT_WAKE", raising=False)
    _forbid_direct_connect(monkeypatch)
    ns = argparse.Namespace(task_id=tid, profile="default",
                            name="jensen-orchestrator", json=False)
    rc = kcli._cmd_kanban_wake_arm(ns)
    assert rc == 0
    subs = s.list_profile_event_subs(task_id=tid, profile="default", enabled_only=False)
    armed = [x for x in subs if (x.get("name") or "") == "jensen-orchestrator"]
    assert armed and bool(armed[0].get("wake_agent"))
