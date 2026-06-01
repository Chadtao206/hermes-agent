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


def _seed_running_with_pid(store, pid, title="run"):
    tid = store.create_task(title=title)
    assert store.claim_task(tid) is not None      # ready -> running (+run)
    store.record_spawn_success(tid, pid)          # sets worker_pid
    return tid


def _detect(store, monkeypatch, *, kind, code, skip_unknown=False):
    """Run crash detection with a forced (kind, code) classification + dead pid."""
    if isinstance(store, PostgresKanbanStore):
        return store._pg_detect_crashed_workers(
            pid_alive_fn=lambda p: False,
            classify_exit_fn=lambda p: (kind, code),
            skip_unknown=skip_unknown)
    monkeypatch.setattr(kb, "_pid_alive", lambda p: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda p: (kind, code))
    with kb.connect_closing() as conn:
        return kb.detect_crashed_workers(conn, skip_unknown=skip_unknown)


def _events(store, tid):
    return [(e.kind, e.payload) for e in store.list_events(tid)]


def test_m2_nonzero_exit_text_and_payload(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483645)
    crashed = _detect(store, monkeypatch, kind="nonzero_exit", code=7)
    assert tid in crashed
    crash_ev = [p for (k, p) in _events(store, tid) if k == "crashed"]
    assert crash_ev and crash_ev[-1].get("exit_kind") == "nonzero_exit"
    assert crash_ev[-1].get("exit_code") == 7
    assert store.get_task(tid).status == "ready"


def test_m2_signaled_text_and_payload(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483644)
    crashed = _detect(store, monkeypatch, kind="signaled", code=9)
    assert tid in crashed
    crash_ev = [p for (k, p) in _events(store, tid) if k == "crashed"]
    assert crash_ev[-1].get("exit_kind") == "signaled"
    assert crash_ev[-1].get("exit_code") == 9


def test_m2_unknown_no_exit_code(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483643)
    crashed = _detect(store, monkeypatch, kind="unknown", code=None)
    assert tid in crashed
    crash_ev = [p for (k, p) in _events(store, tid) if k == "crashed"]
    assert "exit_code" not in crash_ev[-1]
    assert "exit_kind" not in crash_ev[-1]


def test_m3_skip_unknown_defers(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483642)
    crashed = _detect(store, monkeypatch, kind="unknown", code=None, skip_unknown=True)
    assert tid not in crashed
    assert store.get_task(tid).status == "running"


def test_m1_systemic_three_same_fingerprint_cap_block(store, monkeypatch):
    pids = [2147483600, 2147483601, 2147483602]
    tids = [_seed_running_with_pid(store, p, title=f"t{p}") for p in pids]
    crashed = _detect(store, monkeypatch, kind="nonzero_exit", code=7)
    assert set(tids) <= set(crashed)
    for tid in tids:
        assert store.get_task(tid).status == "blocked"
        assert any(k == "gave_up" for (k, _p) in _events(store, tid))


def test_m1_two_below_threshold_not_capped(store, monkeypatch):
    pids = [2147483610, 2147483611]
    tids = [_seed_running_with_pid(store, p, title=f"t{p}") for p in pids]
    _detect(store, monkeypatch, kind="nonzero_exit", code=7)
    for tid in tids:
        assert store.get_task(tid).status == "ready"
        assert not any(k == "gave_up" for (k, _p) in _events(store, tid))


def test_protocol_violation_caps_immediately(store, monkeypatch):
    tid = _seed_running_with_pid(store, 2147483630)
    _detect(store, monkeypatch, kind="clean_exit", code=0)
    assert store.get_task(tid).status == "blocked"
    kinds = [k for (k, _p) in _events(store, tid)]
    assert "protocol_violation" in kinds and "gave_up" in kinds
