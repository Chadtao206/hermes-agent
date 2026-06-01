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


def test_events_stream_tails_postgres(pg_client, monkeypatch):
    # _check_ws_token rejects a missing token even in the test harness
    # (it only fails-open for a *non-empty* token when web_server isn't
    # importable). The pg_client router is loaded by path under a synthetic
    # module name, so monkeypatch _check_ws_token on that exact instance.
    import sys
    plugin_mod = sys.modules["hermes_dashboard_plugin_kanban_test"]
    monkeypatch.setattr(plugin_mod, "_check_ws_token", lambda _token: True)

    s = pg_client.pg_store
    t = s.create_task(title="evt", assignee="engineer")  # emits task_events rows
    with pg_client.websocket_connect("/api/plugins/kanban/events?since=0") as ws:
        msg = ws.receive_json()
        assert "events" in msg and msg["cursor"] > 0
        assert any(e["task_id"] == t for e in msg["events"])
        # payloads are objects/None, never raw JSON strings
        assert all(not isinstance(e["payload"], str) for e in msg["events"])


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


def test_task_detail_workers_diagnostics_pg(pg_client):
    s = pg_client.pg_store
    p = s.create_task(title="parent", assignee="engineer", body="pbody")
    c = s.create_task(title="child", assignee="reviewer", body="cbody")
    s.link_tasks(p, c)
    s.add_comment(c, author="ops", body="please review")

    r = pg_client.get(f"/api/plugins/kanban/tasks/{c}")
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["id"] == c and body["task"]["body"] == "cbody"
    assert [cm["body"] for cm in body["comments"]] == ["please review"]
    assert body["links"]["parents"] == [p]

    r2 = pg_client.get(f"/api/plugins/kanban/tasks/{c}?run_state_type=status&run_state_name=running")
    assert r2.status_code == 400

    aw = pg_client.get("/api/plugins/kanban/workers/active").json()
    assert aw["count"] == 0 and aw["workers"] == []

    dg = pg_client.get("/api/plugins/kanban/diagnostics").json()
    assert "diagnostics" in dg and "count" in dg

    wh = pg_client.get("/api/plugins/kanban/wake-health/details").json()
    assert "wake_health" in wh and "rows" in wh

    lg = pg_client.get(f"/api/plugins/kanban/tasks/{c}/log")
    assert lg.status_code == 200  # exists check via store; log file absent -> content ""


def test_get_task_pool_failure_503_no_dsn_leak(pg_client, monkeypatch):
    """If the pool/store acquisition raises on a transient pooler outage, the
    /tasks/{id} PG branch must map it to a controlled 503 — never an unguarded
    500 — and the DSN-bearing exception message must not leak in the response.

    Regression for the bug where _store()/_pg_reads() ran OUTSIDE the guarded
    try, so a pool failure escaped as a 500 with a DSN-bearing traceback.
    """
    import sys
    from fastapi.testclient import TestClient

    s = pg_client.pg_store
    c = s.create_task(title="leaky", assignee="engineer")

    plugin_mod = sys.modules["hermes_dashboard_plugin_kanban_test"]
    fake_dsn = "postgresql://postgres:SUPERSECRET@db.example.invalid:6543/kanban"

    def _boom(*_a, **_k):
        raise RuntimeError(f"connection to pooler failed using {fake_dsn}")

    # _store is what acquires pg_pool.get_pool() under the PG branch; making it
    # raise simulates a transient pooler outage at store-open time.
    monkeypatch.setattr(plugin_mod, "_store", _boom)

    # raise_server_exceptions=False so an UNGUARDED 500 would surface as a 500
    # status (pre-fix) rather than re-raising inside the test client; post-fix
    # the guarded try maps it to 503.
    client = TestClient(pg_client.app, raise_server_exceptions=False)
    r = client.get(f"/api/plugins/kanban/tasks/{c}")
    assert r.status_code == 503, r.text
    assert "SUPERSECRET" not in r.text
    assert fake_dsn not in r.text


def test_get_board_pool_failure_503_no_dsn_leak(pg_client, monkeypatch):
    """If _store()/_pg_reads() raises during /board PG branch, it must produce a
    controlled 503 — not an unguarded 500 — and must not leak the DSN.

    Regression for the bug where _store() ran OUTSIDE the guarded try in get_board,
    so a pool failure escaped as a 500 with a DSN-bearing traceback.
    """
    import sys
    from fastapi.testclient import TestClient

    plugin_mod = sys.modules["hermes_dashboard_plugin_kanban_test"]
    fake_dsn = "postgresql://postgres:SUPERSECRET@db.example.invalid:6543/kanban"

    def _boom(*_a, **_k):
        raise RuntimeError(f"connection to pooler failed using {fake_dsn}")

    monkeypatch.setattr(plugin_mod, "_store", _boom)

    client = TestClient(pg_client.app, raise_server_exceptions=False)
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 503, r.text
    assert "SUPERSECRET" not in r.text
    assert fake_dsn not in r.text


def test_writes_land_in_postgres(pg_client):
    s = pg_client.pg_store
    r = pg_client.post("/api/plugins/kanban/tasks", json={"title": "made in dashboard", "assignee": "engineer"})
    assert r.status_code == 200
    tid = r.json()["task"]["id"]
    assert s.get_task(tid) is not None
    r = pg_client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"title": "renamed", "priority": 5})
    assert r.status_code == 200
    assert s.get_task(tid).title == "renamed"
    assert s.get_task(tid).priority == 5
    r = pg_client.post("/api/plugins/kanban/tasks/bulk", json={"ids": [tid], "archive": True})
    assert r.status_code == 200
    assert r.json()["results"][0]["ok"] is True
    assert s.get_task(tid).status == "archived"


def test_create_with_parents_links_in_pg(pg_client):
    s = pg_client.pg_store
    parent = s.create_task(title="P", assignee="engineer")
    r = pg_client.post("/api/plugins/kanban/tasks",
                       json={"title": "child", "assignee": "reviewer", "parents": [parent]})
    assert r.status_code == 200
    child = r.json()["task"]["id"]
    # the link landed in PG
    assert parent in s.parent_ids(child)


def test_reconcile_clean_board_pg(pg_client):
    """Clean board → run_reconciler returns empty actions + partial note; no old no-op."""
    r = pg_client.get("/api/plugins/kanban/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert body.get("actions") == []
    assert "partial" in body                        # backend-aware run_reconciler result
    assert "text_preview" in body                   # format_reconcile_text ran
    assert "not yet available" not in (body.get("note") or "")  # old no-op note gone


def test_reconcile_returns_real_actions_under_postgres(pg_client):
    import time
    from hermes_cli.kanban import pg_pool
    s = pg_client.pg_store
    t = s.create_task(title="stuck ready", assignee=None)
    with pg_pool.get_pool().connection() as conn:
        conn.execute("UPDATE tasks SET created_at=%s WHERE board=%s AND id=%s",
                     (int(time.time()) - 10_000, s.board, t)); conn.commit()
    r = pg_client.get("/api/plugins/kanban/reconcile?ready_age_seconds=900")
    assert r.status_code == 200
    body = r.json()
    assert body["mutation_applied"] is False
    assert any(a["kind"].startswith("old_ready_") for a in body["actions"])  # real, not no-op
    assert "partial" in body                       # deferred-kinds note present
    assert "text_preview" in body                  # format_reconcile_text ran
    assert "not yet available" not in (body.get("note") or "")  # old no-op note gone


def test_boards_counts_pg(pg_client):
    s = pg_client.pg_store
    s.create_task(title="x", assignee="engineer")
    body = pg_client.get("/api/plugins/kanban/boards").json()
    default = next((b for b in body["boards"] if b["slug"] == "default"), None)
    assert default is not None and default["total"] >= 1
