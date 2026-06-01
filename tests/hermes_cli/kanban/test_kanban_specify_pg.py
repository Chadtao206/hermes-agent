"""kanban_specify routes its DB read+write through Postgres under backend=postgres."""
import uuid
import pytest

from hermes_cli import kanban_specify
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


@pytest.fixture
def pg_store(_pg_dsn, monkeypatch):
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    board = f"spec_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")
    monkeypatch.setattr("hermes_cli.kanban_db.get_current_board", lambda *a, **k: board)
    s = PostgresKanbanStore(board=board, pool=pool)
    try:
        yield s
    finally:
        s.close(); pool.close()


def _stub_llm(monkeypatch, title, body):
    class _Msg:
        def __init__(self): self.content = f'{{"title": "{title}", "body": "{body}"}}'
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


def test_specify_task_writes_postgres(pg_store, monkeypatch):
    tid = pg_store.create_task(title="rough", triage=True)
    _stub_llm(monkeypatch, "Tightened", "Goal body")
    outcome = kanban_specify.specify_task(tid, author="alice")
    assert outcome.ok is True
    t = pg_store.get_task(tid)
    assert t.status in ("todo", "ready")     # mutated LIVE pg, not frozen sqlite
    assert t.title == "Tightened"


def test_list_triage_ids_reads_postgres(pg_store, monkeypatch):
    a = pg_store.create_task(title="t1", triage=True)
    pg_store.create_task(title="live")  # ready, excluded
    ids = kanban_specify.list_triage_ids()
    assert a in ids


def test_specify_task_degrades_on_store_error(monkeypatch):
    """A Postgres connectivity error must degrade to ok=False, not raise,
    and must not leak a DSN/host into the reason."""
    monkeypatch.setattr("hermes_cli.kanban.store.resolve_backend", lambda: "postgres")

    class _BoomStore:
        def get_task(self, _):
            raise RuntimeError("connection to host=secret-host port=5432 failed")

    monkeypatch.setattr(kanban_specify, "_pg_store", lambda: _BoomStore())
    outcome = kanban_specify.specify_task("t_whatever", author="alice")
    assert outcome.ok is False
    assert "RuntimeError" in outcome.reason          # type name only
    assert "secret-host" not in outcome.reason        # no raw exception text / DSN
