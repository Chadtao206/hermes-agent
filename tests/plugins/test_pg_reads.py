"""Unit tests for the dashboard Postgres read helpers."""
import importlib.util
import time
from pathlib import Path


def _load_pg_reads():
    repo_root = Path(__file__).resolve().parents[2]
    f = repo_root / "plugins" / "kanban" / "dashboard" / "pg_reads.py"
    spec = importlib.util.spec_from_file_location("kanban_dash_pg_reads_test", f)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_board_aggregates(pg_board):
    pg = _load_pg_reads()
    s = pg_board
    p = s.create_task(title="parent", assignee="engineer", body="b", tenant="acme")
    c1 = s.create_task(title="c1", assignee="reviewer")
    c2 = s.create_task(title="c2", assignee="engineer")
    s.link_tasks(p, c1)
    s.link_tasks(p, c2)
    # link_tasks(dependency) demotes ready->todo; bypass store guards with
    # direct SQL to put c1 into done (complete_task requires running/ready/
    # blocked/scheduled, not todo; set_status_direct to ready also blocked
    # because the parent is not done).
    from hermes_cli.kanban import pg_pool
    with pg_pool.get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='done', completed_at=%s "
            "WHERE board=%s AND id=%s",
            (int(time.time()), "default", c1),
        )
        cur.execute(
            "INSERT INTO task_events (board, task_id, kind, payload, created_at) "
            "VALUES (%s, %s, 'status', '{\"status\": \"done\"}'::jsonb, %s)",
            ("default", c1, int(time.time())),
        )
    s.add_comment(p, author="ops", body="hi")
    s.add_comment(p, author="ops", body="again")

    assert pg.comment_counts("default").get(p) == 2
    lc = pg.link_counts("default")
    assert lc.get(p, {}).get("children") == 2
    assert lc.get(c1, {}).get("parents") == 1
    prog = pg.child_progress("default")
    assert prog.get(p) == {"done": 1, "total": 2}
    assert "acme" in pg.distinct_tenants("default")
    assert set(pg.distinct_assignees("default")) >= {"engineer", "reviewer"}
    assert pg.latest_event_id("default") > 0
    assert pg.board_counts("default").get("done") == 1


def test_events_since_active_workers_blocking(pg_board):
    pg = _load_pg_reads()
    s = pg_board
    p = s.create_task(title="parent", assignee="engineer")
    c = s.create_task(title="child", assignee="reviewer")
    s.link_tasks(p, c)  # c depends on p (p not done) -> p blocks c

    cursor, events = pg.events_since("default", 0, 200)
    assert cursor > 0
    assert all(isinstance(e["payload"], (dict, type(None))) for e in events)
    assert {e["task_id"] for e in events} >= {p, c}
    # incremental: nothing new past the cursor
    cursor2, events2 = pg.events_since("default", cursor, 200)
    assert events2 == [] and cursor2 == cursor

    blockers = pg.parents_blocking_ready("default", c)
    assert [b["id"] for b in blockers] == [p]
    assert blockers[0]["status"] != "done"

    assert pg.active_workers("default") == []  # nothing running/claimed


def test_diagnostics_rows_and_wake_health(pg_board):
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config
    pg = _load_pg_reads()
    s = pg_board
    t = s.create_task(title="t", assignee="engineer")
    s.add_profile_event_sub(task_id=t, profile="engineer", name="", wake_agent=True)

    task_rows, events_by, runs_by = pg.diagnostics_rows("default")
    assert any(r["id"] == t for r in task_rows)
    # rows are dict-shaped with the keys the engine reads
    row = next(r for r in task_rows if r["id"] == t)
    for k in ("id", "status", "assignee", "consecutive_failures", "last_failure_error", "created_at"):
        assert k in row
    # engine consumes them without error
    cfg = kd.config_from_runtime_config(load_config())
    diags = kd.compute_task_diagnostics(row, events_by.get(t, []), runs_by.get(t, []), config=cfg)
    assert isinstance(diags, list)

    wh = pg.wake_health("default", [t])
    assert wh["subscription_count"] == 1
    rows, overflow = pg.wake_health_rows("default", [t], {t: s.get_task(t)}, 50)
    assert isinstance(rows, list) and overflow == 0
