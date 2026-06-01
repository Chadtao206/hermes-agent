"""Cross-backend parity for the PG crash lane + claim_lock default."""
import socket
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


def _host_prefix():
    return f"{kb._claimer_id().split(':', 1)[0]}:"


def test_claim_task_default_claimer_is_host_pid(store):
    tid = store.create_task(title="claim me")
    claimed = store.claim_task(tid)            # claimer=None
    assert claimed is not None
    t = store.get_task(tid)
    assert t.claim_lock is not None
    assert t.claim_lock.startswith(_host_prefix())
    claimed_events = [e for e in store.list_events(tid) if e.kind == "claimed"]
    assert claimed_events
    assert str(claimed_events[-1].payload.get("lock", "")).startswith(_host_prefix())
