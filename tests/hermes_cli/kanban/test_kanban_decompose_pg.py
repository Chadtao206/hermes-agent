"""kanban_decompose routes through Postgres under backend=postgres."""
import json, uuid
import pytest

from hermes_cli import kanban_decompose
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


@pytest.fixture
def pg_store(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn); pg_pool.ensure_schema(pool)
    board = f"dec_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda n: True)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda: [])
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "orchestrator")
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s
    finally:
        s.close(); pool.close()


def _stub_decomposer(monkeypatch, payload: dict):
    raw = json.dumps(payload)
    class _Msg:
        def __init__(self): self.content = raw
    class _Choice:
        def __init__(self): self.message = _Msg()
    class _Resp:
        def __init__(self): self.choices = [_Choice()]
    class _Completions:
        def create(self, **kw): return _Resp()
    class _Chat:
        def __init__(self): self.completions = _Completions()
    class _Client:
        def __init__(self): self.chat = _Chat()
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "get_text_auxiliary_client", lambda *a, **k: (_Client(), "m"))
    monkeypatch.setattr(ac, "get_auxiliary_extra_body", lambda *a, **k: None)


def test_decompose_fanout_writes_postgres(pg_store, monkeypatch):
    root = pg_store.create_task(title="big", triage=True)
    _stub_decomposer(monkeypatch, {
        "fanout": True, "rationale": "split",
        "tasks": [
            {"title": "A", "body": "a", "assignee": "engineer", "parents": []},
            {"title": "B", "body": "b", "assignee": "reviewer", "parents": [0]},
        ]})
    outcome = kanban_decompose.decompose_task(root, author="alice")
    assert outcome.ok is True and outcome.fanout is True
    assert len(outcome.child_ids) == 2
    assert pg_store.get_task(root).status == "todo"
    assert "decomposed" in [e.kind for e in pg_store.list_events(root)]


def test_decompose_no_fanout_specifies_postgres(pg_store, monkeypatch):
    root = pg_store.create_task(title="single", triage=True)
    _stub_decomposer(monkeypatch, {
        "fanout": False, "rationale": "one unit",
        "title": "Tightened", "body": "spec", "assignee": "engineer"})
    outcome = kanban_decompose.decompose_task(root, author="alice")
    assert outcome.ok is True and outcome.fanout is False
    assert pg_store.get_task(root).status in ("todo", "ready")
    assert "specified" in [e.kind for e in pg_store.list_events(root)]


def test_list_triage_ids_reads_postgres(pg_store):
    a = pg_store.create_task(title="t1", triage=True)
    pg_store.create_task(title="live")
    assert a in kanban_decompose.list_triage_ids()


def test_decompose_degrades_on_store_error(monkeypatch):
    """A PG connectivity error degrades to ok=False, no raise, no DSN leak."""
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    class _BoomStore:
        def get_task(self, _):
            raise RuntimeError("connection to host=secret-host port=5432 failed")
    monkeypatch.setattr(kanban_decompose, "_pg_store", lambda: _BoomStore())
    outcome = kanban_decompose.decompose_task("t_whatever", author="alice")
    assert outcome.ok is False
    assert "RuntimeError" in outcome.reason
    assert "secret-host" not in outcome.reason
