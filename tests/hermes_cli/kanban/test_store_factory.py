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


def test_factory_returns_postgres_store(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban import pg_pool
    monkeypatch.setattr(pg_pool, "get_pool", lambda dsn=None: object())
    from hermes_cli.kanban.store import kanban_store
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    s = kanban_store(board=None)
    assert isinstance(s, PostgresKanbanStore)
    s.close()


def test_resolve_backend_env_override_wins(monkeypatch):
    from hermes_cli.kanban import store as store_mod
    # Config says sqlite (default), env says postgres → env wins.
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    assert store_mod.resolve_backend() == "postgres"


def test_resolve_backend_env_invalid_falls_through(monkeypatch):
    from hermes_cli.kanban import store as store_mod
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "mariadb")
    # Invalid env value is ignored → falls through to config (sqlite default).
    assert store_mod.resolve_backend() == "sqlite"


def test_resolve_backend_no_env_unchanged(monkeypatch):
    from hermes_cli.kanban import store as store_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    # No env → existing config behavior (sqlite default in test env).
    assert store_mod.resolve_backend() == "sqlite"
