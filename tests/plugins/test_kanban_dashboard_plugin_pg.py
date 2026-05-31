"""Dashboard plugin API on the Postgres backend."""


def test_pg_client_stats_resolves_postgres(pg_client):
    # /stats routes through the backend-aware _store(); a clean PG 'default'
    # board returns zeroed counts (proves the harness resolves Postgres).
    r = pg_client.get("/api/plugins/kanban/stats")
    assert r.status_code == 200
    body = r.json()
    assert "by_status" in body
    assert sum(body["by_status"].values()) == 0


def test_board_reflects_live_postgres(pg_client):
    s = pg_client.pg_store
    p = s.create_task(title="parent", assignee="engineer", body="b", tenant="acme")
    c = s.create_task(title="child", assignee="reviewer")
    s.link_tasks(p, c)
    s.add_comment(p, author="ops", body="hi")
    r = pg_client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    body = r.json()
    cols = {col["name"]: col["tasks"] for col in body["columns"]}
    all_ids = {t["id"] for tasks in cols.values() for t in tasks}
    assert {p, c} <= all_ids
    pcard = next(t for tasks in cols.values() for t in tasks if t["id"] == p)
    assert pcard["comment_count"] == 1
    assert pcard["link_counts"]["children"] == 1
    assert pcard["progress"] == {"done": 0, "total": 1}
    assert "acme" in body["tenants"]
    assert set(body["assignees"]) >= {"engineer", "reviewer"}
    assert body["latest_event_id"] > 0


def test_board_running_column_from_postgres(pg_client):
    s = pg_client.pg_store
    t = s.create_task(title="run me", assignee="engineer")
    claimed = s.claim_task(t)
    assert claimed is not None
    body = pg_client.get("/api/plugins/kanban/board").json()
    running = next(col["tasks"] for col in body["columns"] if col["name"] == "running")
    assert t in {x["id"] for x in running}


def test_board_resolves_current_board_consistently(monkeypatch, _pg_dsn, tmp_path):
    """When ?board= is omitted and the current-board pointer != 'default',
    the store AND the aggregates must read the SAME board (regression for the
    store-vs-aggregate split-brain)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    from tests.plugins.conftest import _load_plugin_router

    board = "wt_split"
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        for tbl in ("task_events", "task_comments", "task_runs", "task_links",
                    "kanban_profile_wake_events", "kanban_profile_event_subs", "tasks"):
            cur.execute(f"DELETE FROM {tbl} WHERE board=%s", (board,))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", _pg_dsn)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)  # current-board pointer != default
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    home = tmp_path / ".hermes"; home.mkdir(); monkeypatch.setenv("HERMES_HOME", str(home))
    # Make the non-default board "exist" on disk so the HERMES_KANBAN_BOARD
    # pointer is honoured by get_current_board() (it gates the env var behind
    # board_exists()). Without this, the current-board pointer silently falls
    # back to 'default' and the split-brain regression can't be exercised.
    from hermes_cli import kanban_db
    bdir = kanban_db.board_dir(board); bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "board.json").write_text("{}", encoding="utf-8")
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        p = s.create_task(title="P", assignee="engineer", tenant="acme")
        c = s.create_task(title="C", assignee="reviewer")
        s.link_tasks(p, c)
        s.add_comment(p, author="ops", body="hi")
        app = FastAPI(); app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
        body = TestClient(app).get("/api/plugins/kanban/board").json()
        cols = {col["name"]: col["tasks"] for col in body["columns"]}
        all_ids = {t["id"] for tasks in cols.values() for t in tasks}
        assert {p, c} <= all_ids, "store did not read the current-board pointer's board"
        pcard = next(t for tasks in cols.values() for t in tasks if t["id"] == p)
        assert pcard["comment_count"] == 1, "aggregates read a different board than the store"
        assert pcard["link_counts"]["children"] == 1
        assert "acme" in body["tenants"]
    finally:
        with pool.connection() as conn, conn.cursor() as cur:
            for tbl in ("task_events", "task_comments", "task_runs", "task_links",
                        "kanban_profile_wake_events", "kanban_profile_event_subs", "tasks"):
                cur.execute(f"DELETE FROM {tbl} WHERE board=%s", (board,))
        s.close(); pool.close()
