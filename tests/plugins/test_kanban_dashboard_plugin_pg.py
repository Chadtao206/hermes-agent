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
