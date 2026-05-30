import pytest

# Backends under test. Postgres is added in Phase 2 (an env-gated param).
_BACKENDS = ["sqlite"]


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path, monkeypatch):
    """Yield a fresh KanbanStore for each backend. SQLite uses an isolated
    tmp board DB; the store owns connection lifecycle."""
    backend = request.param
    if backend == "sqlite":
        db = tmp_path / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
        from hermes_cli import kanban_db as kb
        kb.connect(db_path=db, readonly=False, _bootstrap=True).close()
        from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
        s = SqliteKanbanStore(board=None)
        try:
            yield s
        finally:
            s.close()
    else:
        pytest.skip(f"backend {backend} not available in Phase 1")
