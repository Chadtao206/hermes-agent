"""GET /board and GET /tasks/{id} must be identical across sqlite and postgres
for identical seed data (modulo ids/timestamps)."""
import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _router():
    repo_root = Path(__file__).resolve().parents[2]
    f = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_dash_parity_test", f)
    m = importlib.util.module_from_spec(spec)
    import sys; sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m.router


def _seed(store):
    p = store.create_task(title="parent", assignee="engineer", body="pbody", tenant="acme")
    c = store.create_task(title="child", assignee="reviewer", body="cbody")
    store.link_tasks(p, c)
    store.add_comment(p, author="ops", body="comment one")
    # Drive a real RUN through the parent so /tasks/{parent} `runs` is non-empty
    # and the parent gets a `latest_summary`. claim_task (ready->running) creates
    # the task_runs row; complete_task closes it with a summary + metadata. The
    # claimer is pinned so the `claimed` event payload is byte-identical across
    # backends. metadata is a dict so we also exercise the JSONB(PG)/json(sqlite)
    # round-trip of run.metadata for free.
    store.claim_task(p, claimer="parity")
    store.complete_task(p, summary="did it", metadata={"k": "v"})
    # Seed a profile wake-subscription on the child so /board `wake_health`
    # reports subscription_count>=1 (severity "stale", since no wake has fired)
    # on BOTH backends — exercising the wake_health translation with real data
    # instead of the all-zero/"none" empty case.
    store.add_profile_event_sub(task_id=c, profile="reviewer", name="", wake_agent=True)
    return p, c


def _norm(obj, idmap, ts_keys=("created_at", "started_at", "completed_at", "now",
                               "as_of", "checked_at",
                               # `ended_at` is the task_runs row's run-end epoch
                               # (set to `now` by complete_task); each backend's
                               # seed drives its own real claim->complete run in
                               # wall-clock time, so it legitimately differs.
                               "ended_at",
                               # `expires` is the claim TTL deadline (now+ttl), a
                               # wall-clock epoch that genuinely differs run to
                               # run — it lives under the `claimed` event payload.
                               "expires",
                               # `age` is a dict of leaf metrics computed from
                               # `now`, so each leaf legitimately differs run to
                               # run. We collapse only the LEAVES (not the `age`
                               # dict itself) so the dict's KEY SET is still
                               # compared — a structural divergence (PG omitting
                               # a key, or returning a non-dict) would still trip
                               # the assertion.
                               "created_age_seconds", "started_age_seconds",
                               "time_to_complete_seconds")):
    """Recursively replace task ids with stable tokens and null out timestamps.

    String task ids (``t_<hex>``) are remapped via ``idmap`` so the two
    backends' freshly-minted ids collapse to ``<P>``/``<C>``.

    Integer SURROGATE keys are auto-increment cursors: sqlite restarts low
    in a fresh per-test DB while Postgres draws from a shared, ever-advancing
    board sequence (e.g. sqlite event ids ``2,3`` vs PG ``1167,1168``). The
    frontend treats these only as opaque per-backend ordering/cursor tokens
    — never compared across backends — so the magnitude is BENIGN. We
    collapse the integer row ``id`` (event/comment/run rows), the board's
    ``latest_event_id`` cursor (an integer surrogate, NOT a timestamp — it is
    ``MAX(task_events.id)``), AND the ``run_id`` surrogate (the
    backend-sequence task_runs row id — surfaces under ``event.run_id``,
    ``payload.run_id`` (the `claimed` event), ``payload.closeout_packet.run_id``,
    and ``runs[].metadata.closeout_packet.run_id``; it only ever holds the
    serial run id, never a value the frontend compares cross-backend) to
    ``<SID>``, while leaving STRING ids to ``idmap`` (so the string task id
    under ``task.id``/``links`` is unaffected). This neutralises the serial-key
    drift without deleting the key or masking a real payload divergence.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ts_keys:
                out[k] = "<TS>"
            elif k in ("id", "latest_event_id", "run_id") and isinstance(v, int):
                out[k] = "<SID>"
            else:
                out[k] = _norm(v, idmap, ts_keys)
        return out
    if isinstance(obj, list):
        return [_norm(x, idmap, ts_keys) for x in obj]
    if isinstance(obj, str):
        return idmap.get(obj, obj)
    return obj


def test_board_parity(tmp_path, monkeypatch, _pg_dsn):
    # --- sqlite ---
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PG_DSN", raising=False)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    s_sql = SqliteKanbanStore(board=None)
    p1, c1 = _seed(s_sql)
    app1 = FastAPI(); app1.include_router(_router(), prefix="/api/plugins/kanban")
    b_sql = TestClient(app1).get("/api/plugins/kanban/board").json()
    t_sql = TestClient(app1).get(f"/api/plugins/kanban/tasks/{c1}").json()
    # Parent carries the comment + the seeded run, so its /tasks payload
    # exercises comment body/author serialization, the run dict (incl.
    # metadata round-trip + latest_summary), and the surrogate comment/run/
    # event ids — none of which the child (no comments, no runs) reaches.
    tp_sql = TestClient(app1).get(f"/api/plugins/kanban/tasks/{p1}").json()
    s_sql.close()

    # --- postgres ---
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        for tbl in ("task_events", "task_comments", "task_runs", "task_links",
                    "kanban_profile_wake_events", "kanban_profile_event_subs", "tasks"):
            cur.execute(f"DELETE FROM {tbl} WHERE board=%s", ("default",))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", _pg_dsn)
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    s_pg = PostgresKanbanStore(board="default", pool=pool)
    p2, c2 = _seed(s_pg)
    app2 = FastAPI(); app2.include_router(_router(), prefix="/api/plugins/kanban")
    b_pg = TestClient(app2).get("/api/plugins/kanban/board").json()
    t_pg = TestClient(app2).get(f"/api/plugins/kanban/tasks/{c2}").json()
    tp_pg = TestClient(app2).get(f"/api/plugins/kanban/tasks/{p2}").json()
    s_pg.close(); pool.close()

    idmap = {p2: "<P>", c2: "<C>", p1: "<P>", c1: "<C>"}
    assert _norm(b_sql, idmap) == _norm(b_pg, idmap)
    assert _norm(t_sql, idmap) == _norm(t_pg, idmap)
    assert _norm(tp_sql, idmap) == _norm(tp_pg, idmap)
