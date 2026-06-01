"""kanban metrics reads the live PG board under backend=postgres."""
import uuid
import pytest

from hermes_cli import kanban_metrics as kmet
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


@pytest.fixture
def pg(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"met_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, board
    finally:
        s.close(); pool.close()


def _seed(s):
    # 2 completed + 1 blocked -> task_runs with outcomes + terminal events
    a = s.create_task(title="a"); s.claim_task(a); s.complete_task(a, summary="ok")
    b = s.create_task(title="b"); s.claim_task(b); s.complete_task(b, summary="ok")
    c = s.create_task(title="c"); s.claim_task(c); s.block_task(c, reason="needs review")
    return a, b, c


def test_metrics_reads_live_pg_current_state(pg):
    s, board = pg
    _seed(s)
    r = kmet.collect_metrics(board=board)
    assert r["db_path"].startswith("postgres://")          # live PG, not frozen sqlite
    assert "postgres:postgres@" not in r["db_path"]          # redacted, no creds
    cs = r["current_state"]
    assert cs["task_status_counts"].get("done") == 2
    assert cs["task_status_counts"].get("blocked") == 1
    assert cs["blocked_tasks"] == 1
    # health comes from the already-PG-aware doctor/reconcile
    assert "health" in r and "reconcile_ok" in r["health"]


def test_metrics_window_outcomes_live_pg(pg):
    s, board = pg
    _seed(s)
    r = kmet.collect_metrics(board=board)
    allw = next(w for w in r["windows"] if w["cutoff"] is None)  # the all-time window
    assert allw["outcome_counts"].get("completed") == 2
    assert allw["completion_count"] == 2
    assert allw["blocked_count"] == 1


def test_metrics_write_snapshot_pg(pg, tmp_path):
    s, board = pg
    _seed(s)
    snap = tmp_path / "snap.db"
    r = kmet.collect_metrics(board=board, write_snapshot=True, snapshot_db=snap)
    assert r["persisted_snapshot"]["id"]
    # persisted_snapshot["db_path"] is the snapshot file; the source PG DSN is r["db_path"]
    assert r["db_path"].startswith("postgres://")
    assert snap.exists()


def test_metrics_backend_unavailable_no_leak(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: "default")
    class _BadPool:
        def connection(self, *a, **k): raise RuntimeError("conn to secret-host:5432 failed")
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: _BadPool())
    r = kmet.collect_metrics(board="default")
    assert "secret-host" not in str(r)                       # no raw exception / DSN
    assert r["db_path"].startswith("postgres://")            # redacted
    assert r["current_state"] is not None                    # degraded shape, no raise
