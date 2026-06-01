"""apply_reconcile_decision on the Postgres backend: ports keep_parked/keep_blocked
acks to live PG; hard-guards every mutation option (no frozen-sqlite write)."""
import time, uuid
import pytest

from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore
from hermes_cli import kanban_reconciler as krec


@pytest.fixture
def pg(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"apply_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def _seed_blocked_with_done_parent(s):
    parent = s.create_task(title="parent")
    child = s.create_task(title="child", parents=[parent])
    s.complete_task(parent, summary="done")
    s.block_task(child, reason="needs decision")
    return child


def _ack_option_and_sig(board, child):
    res = krec.run_reconciler(board=board, ready_age_seconds=900)
    pkts = res["wake_triage"].get("decision_packets") or []
    pkt = next((p for p in pkts if p.get("task_id") == child), None)
    assert pkt is not None, f"no decision packet for {child}; packets={pkts}"
    plans = pkt.get("operator_plans") or {}
    opt = next((o for o in ("keep_parked", "keep_blocked") if o in plans), None)
    assert opt is not None, f"no ack option in operator_plans={list(plans)}"
    return opt, pkt["packet_signature"]


def test_keep_ack_writes_pg_comment_and_suppresses(pg):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    opt, sig = _ack_option_and_sig(board, child)
    res = krec.apply_reconcile_decision(
        task_id=child, option=opt, packet_signature=sig,
        confirm_dry_run=True, board=board, author="jensen")
    assert res["ok"] is True
    assert res["mutation_applied"] is True
    assert res["db_path"].startswith("postgres://")
    assert any("Jensen reconcile decision applied" in c.body and f"option={opt};" in c.body
               for c in s.list_comments(child))
    res2 = krec.run_reconciler(board=board, ready_age_seconds=900)
    assert res2["wake_triage"].get("suppressed_decision_packet_count", 0) >= 1
    assert not any(p.get("task_id") == child
                   for p in (res2["wake_triage"].get("decision_packets") or []))


def test_keep_ack_is_idempotent(pg):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    opt, sig = _ack_option_and_sig(board, child)
    first = krec.apply_reconcile_decision(task_id=child, option=opt, packet_signature=sig,
                                          confirm_dry_run=True, board=board, author="jensen")
    assert first["mutation_applied"] is True
    second = krec.apply_reconcile_decision(task_id=child, option=opt, packet_signature=sig,
                                           confirm_dry_run=True, board=board, author="jensen")
    assert second["ok"] is True
    assert second.get("idempotent") is True
    assert second["mutation_applied"] is False
    acks = [c for c in s.list_comments(child)
            if "Jensen reconcile decision applied" in c.body and f"option={opt};" in c.body]
    assert len(acks) == 1


@pytest.mark.parametrize("bad_option", ["unblock", "close", "reclaim_dead_running",
                                        "clear_orphan_claim_lock", "remediate_parent_closeout"])
def test_mutation_options_guarded_no_write(pg, bad_option):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    before = s.get_task(child).status
    res = krec.apply_reconcile_decision(
        task_id=child, option=bad_option, packet_signature="whatever",
        confirm_dry_run=True, board=board, author="jensen",
        pr_head_sha="a1b2c3d4")
    assert res["ok"] is False
    assert "not available on the postgres backend" in res["error"]
    assert s.get_task(child).status == before
    assert not any("Jensen reconcile decision applied" in c.body for c in s.list_comments(child))


def test_signature_mismatch_parity(pg):
    s, pool, board = pg
    child = _seed_blocked_with_done_parent(s)
    opt, _ = _ack_option_and_sig(board, child)
    res = krec.apply_reconcile_decision(task_id=child, option=opt,
                                        packet_signature="wrong-sig",
                                        confirm_dry_run=True, board=board, author="jensen")
    assert res["ok"] is False
    assert "packet_signature does not match" in res["error"]


def test_no_packet_parity(pg):
    s, pool, board = pg
    live = s.create_task(title="not a decision")
    res = krec.apply_reconcile_decision(task_id=live, option="keep_parked",
                                        packet_signature="x", confirm_dry_run=True,
                                        board=board, author="jensen")
    assert res["ok"] is False
    assert "no current decision packet" in res["error"]


def test_backend_unavailable_no_leak(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: "default")
    class _BadPool:
        def connection(self, *a, **k): raise RuntimeError("conn to secret-host:5432 failed")
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: _BadPool())
    res = krec.apply_reconcile_decision(task_id="t_x", option="keep_parked",
                                        packet_signature="x", confirm_dry_run=True,
                                        author="jensen")
    assert res["ok"] is False
    assert "secret-host" not in str(res)
    assert "postgres backend unavailable" in res["error"]
