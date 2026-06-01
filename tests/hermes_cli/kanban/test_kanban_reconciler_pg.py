"""PG reconciler: collector emits the 9 core action kinds; 2 niche deferred."""
import os
import shutil
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker")),
    reason="postgres backend unavailable")

from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore
from hermes_cli import kanban_reconciler as krec


@pytest.fixture
def pg(_pg_dsn):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"rec_{uuid.uuid4().hex[:8]}"
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s, pool, board
    finally:
        s.close(); pool.close()


def _actions(pool, board, store, **kw):
    with pool.connection() as conn:
        return krec._collect_reconcile_actions_pg(
            conn, board, store,
            ready_age_seconds=kw.get("ready_age_seconds", 900),
            now=kw.get("now", int(time.time())))


def _kinds(pool, board, store, **kw):
    return {a.kind for a in _actions(pool, board, store, **kw)}


def _action_of(actions, kind):
    for a in actions:
        if a.kind == kind:
            return a
    raise AssertionError(f"no action of kind {kind!r}; got {[a.kind for a in actions]}")


# --- running-task scan (dead / expired / stale-heartbeat) -------------------

def test_dead_running_candidate(pg):
    s, pool, board = pg
    t = s.create_task(title="x", assignee="engineer")
    s.claim_task(t)  # -> running, current_run_id set
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET worker_pid=%s WHERE board=%s AND id=%s",
            (2147483647, board, t))
        conn.commit()
    assert "dead_running_candidate" in _kinds(pool, board, s)


def test_expired_claim_candidate(pg):
    s, pool, board = pg
    now = int(time.time())
    t = s.create_task(title="x", assignee="engineer")
    s.claim_task(t)
    with pool.connection() as conn:
        # keep the worker pid alive so dead_running does not also fire, just
        # expire the claim window
        conn.execute(
            "UPDATE tasks SET worker_pid=%s, claim_expires=%s "
            "WHERE board=%s AND id=%s",
            (os.getpid(), now - 100, board, t))
        conn.commit()
    kinds = _kinds(pool, board, s, now=now)
    assert "expired_claim_candidate" in kinds


def test_stale_heartbeat_observed(pg):
    s, pool, board = pg
    now = int(time.time())
    t = s.create_task(title="x", assignee="engineer")
    s.claim_task(t)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET worker_pid=%s, last_heartbeat_at=%s "
            "WHERE board=%s AND id=%s",
            (os.getpid(), now - 2000, board, t))
        conn.commit()
    assert "stale_heartbeat_observed" in _kinds(pool, board, s, now=now)


# --- stale run metadata ------------------------------------------------------

def test_stale_run_metadata(pg):
    s, pool, board = pg
    t = s.create_task(title="x", assignee="engineer")
    s.claim_task(t)  # creates a task_runs row status='running'
    # Move the task off 'running' but leave the run row marked running and
    # clear the task's current_run_id -> stale run metadata.
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
            "WHERE board=%s AND id=%s",
            (board, t))
        conn.commit()
    assert "stale_run_metadata" in _kinds(pool, board, s)


# --- orphan claim lock -------------------------------------------------------

def test_orphan_claim_lock_observed(pg):
    s, pool, board = pg
    t = s.create_task(title="x")
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET claim_lock=%s WHERE board=%s AND id=%s",
                     ("host:abc", board, t)); conn.commit()
    assert "orphan_claim_lock_observed" in _kinds(pool, board, s)


# --- blocked / scheduled with completed parents -----------------------------

def test_blocked_with_completed_parents_decision(pg):
    s, pool, board = pg
    parent = s.create_task(title="p")
    child = s.create_task(title="c", parents=[parent])
    s.complete_task(parent, summary="done")
    s.block_task(child, reason="manual")
    assert "blocked_with_completed_parents_decision" in _kinds(pool, board, s)


def test_scheduled_with_completed_parents_decision(pg):
    s, pool, board = pg
    now = int(time.time())
    parent = s.create_task(title="p")
    child = s.create_task(title="c", parents=[parent])
    s.complete_task(parent, summary="done")
    s.schedule_task(child, reason="park")
    # backdate the child so it clears the ready_age gate
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (now - 10_000, board, child)); conn.commit()
    assert "scheduled_with_completed_parents_decision" in _kinds(
        pool, board, s, ready_age_seconds=900, now=now)


# --- pre-spawn validation ----------------------------------------------------

def test_pre_spawn_validation_decision(pg):
    s, pool, board = pg
    # ready + claim_lock IS NULL + assignee profile that does not resolve
    # -> _pre_spawn_validation_errors_for_reconcile yields "profile not found".
    t = s.create_task(title="x", assignee="nope_nonexistent_profile_xyz")
    actions = _actions(pool, board, s)
    a = _action_of(actions, "pre_spawn_validation_decision")
    assert a.details["validation_errors"]
    assert any("profile not found" in e for e in a.details["validation_errors"])


# --- old ready catch-all -----------------------------------------------------

def test_old_ready_nonspawnable(pg):
    s, pool, board = pg
    t = s.create_task(title="x", assignee=None)
    old = int(time.time()) - 10_000
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (old, board, t)); conn.commit()
    assert "old_ready_nonspawnable" in _kinds(pool, board, s, ready_age_seconds=900)


def test_old_ready_spawnable(pg):
    s, pool, board = pg
    # 'default' profile always resolves spawnable=True in _profile_spawnable.
    t = s.create_task(title="x", assignee="default")
    old = int(time.time()) - 10_000
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (old, board, t)); conn.commit()
    assert "old_ready_spawnable" in _kinds(pool, board, s, ready_age_seconds=900)


# --- deferred kinds never emitted -------------------------------------------

def test_deferred_kinds_never_emitted(pg):
    s, pool, board = pg
    kinds = _kinds(pool, board, s)
    assert "review_parent_pr_head_evidence_missing" not in kinds
    assert "repeated_failure_signature_decision" not in kinds


def test_deferred_kinds_absent_even_with_review_lane_and_failures(pg):
    """Seed conditions that WOULD trigger the deferred kinds in sqlite and
    confirm the PG collector still never emits them."""
    s, pool, board = pg
    # review-lane card with all-terminal parents (would-be review_parent_*)
    parent = s.create_task(title="p")
    reviewer = s.create_task(title="r", assignee="reviewer", parents=[parent])
    s.complete_task(parent, summary="done")
    s.block_task(reviewer, reason="manual")
    # repeated-failure residue
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET consecutive_failures=9, "
            "last_failure_error='boom systemic' WHERE board=%s AND id=%s",
            (board, reviewer))
        conn.commit()
    kinds = _kinds(pool, board, s)
    assert "review_parent_pr_head_evidence_missing" not in kinds
    assert "repeated_failure_signature_decision" not in kinds


def test_read_only_no_mutation(pg):
    """Collector must not write. Snapshot task/run/event counts around a call."""
    s, pool, board = pg
    parent = s.create_task(title="p")
    child = s.create_task(title="c", parents=[parent])
    s.complete_task(parent, summary="done")
    s.block_task(child, reason="manual")

    def _counts():
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks WHERE board=%s", (board,))
            tasks = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM task_runs WHERE board=%s", (board,))
            runs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM task_events WHERE board=%s", (board,))
            events = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM task_comments WHERE board=%s", (board,))
            comments = cur.fetchone()[0]
        return (tasks, runs, events, comments)

    before = _counts()
    _kinds(pool, board, s)
    assert _counts() == before


def test_blocked_parents_string_is_p_id_ordered_and_stable(pg):
    """string_agg ORDER BY p.id makes the parents string deterministic across runs."""
    s, pool, board = pg
    parents = [s.create_task(title=f"p{i}") for i in range(3)]
    child = s.create_task(title="c", parents=parents)
    for p in parents:
        s.complete_task(p, summary="done")
    s.block_task(child, reason="manual")

    def _parents_str():
        with pool.connection() as conn:
            actions = krec._collect_reconcile_actions_pg(
                conn, board, s, ready_age_seconds=900, now=int(time.time()))
        act = next(a for a in actions
                   if a.kind == "blocked_with_completed_parents_decision"
                   and a.task_id == child)
        return act.details["parents"]

    expected = ", ".join(f"{pid}:done" for pid in sorted(parents))
    first = _parents_str()
    assert first == expected           # ordered by p.id
    assert _parents_str() == first     # stable across runs (same signature)


# --- run_reconciler PG dispatch ----------------------------------------------

def test_run_reconciler_pg_returns_real_actions_and_partial(pg, monkeypatch):
    s, pool, board = pg
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    t = s.create_task(title="x", assignee=None)
    old = int(time.time()) - 10_000
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (old, board, t)); conn.commit()
    res = krec.run_reconciler(ready_age_seconds=900)
    assert res["mutation_applied"] is False
    assert res["board"] == board
    assert any(a["kind"].startswith("old_ready_") for a in res["actions"])
    assert "partial" in res
    assert set(res["partial"]["deferred_kinds"]) == {
        "review_parent_pr_head_evidence_missing",
        "repeated_failure_signature_decision"}
    assert "://" in res["db_path"]
    assert "postgres:postgres@" not in res["db_path"]   # no password/userinfo


def test_run_reconciler_pg_unreachable_returns_shape(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: "default")
    class _BadPool:
        def connection(self, *a, **k): raise RuntimeError("nope-secret-host")
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: _BadPool())
    res = krec.run_reconciler()
    assert res["ok"] is False
    assert res["actions"] == []
    assert "nope-secret-host" not in str(res)   # no raw exception text / no DSN
    assert "partial" in res


def test_run_reconciler_pg_error_does_not_fall_through_to_sqlite(pg, monkeypatch):
    """A post-probe PG error must NOT silently fall through to the frozen
    sqlite snapshot; it returns a redacted PG error dict."""
    s, pool, board = pg
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    def _boom(*a, **k):
        raise RuntimeError("boom-secret-host port=5432")
    monkeypatch.setattr(krec, "_collect_reconcile_actions_pg", _boom)
    res = krec.run_reconciler(ready_age_seconds=900)
    assert res["ok"] is False
    assert res["actions"] == []
    assert res["db_path"].startswith("postgres://")   # PG path, NOT frozen sqlite file
    assert "boom-secret-host" not in str(res)          # no raw exception / DSN leak
    assert res.get("error")                            # explicit error marker
    # crucially, db_path is NOT a local sqlite path:
    assert "kanban.db" not in res["db_path"]
