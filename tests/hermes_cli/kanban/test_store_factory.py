import hermes_cli.config as cfg


def test_kanban_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {})
    from hermes_cli.kanban.store import resolve_backend
    assert resolve_backend() == "sqlite"


def test_kanban_backend_reads_config(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban.store import resolve_backend
    assert resolve_backend() == "postgres"


def test_factory_returns_sqlite_store_by_default(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "sqlite"}})
    from hermes_cli.kanban.store import kanban_store
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    s = kanban_store(board=None)
    try:
        assert isinstance(s, SqliteKanbanStore)
    finally:
        s.close()


def test_factory_postgres_not_available_phase1(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban.store import kanban_store
    import pytest
    with pytest.raises(NotImplementedError):
        kanban_store(board=None)
