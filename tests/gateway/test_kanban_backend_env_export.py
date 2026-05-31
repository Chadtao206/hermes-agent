import os

import gateway.run as run_mod


def test_export_kanban_backend_env_sets_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PG_DSN", raising=False)
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("postgres", "postgresql://u:p@h:6543/db"))
    run_mod._export_kanban_backend_env()
    assert os.environ["HERMES_KANBAN_BACKEND"] == "postgres"
    assert os.environ["HERMES_KANBAN_PG_DSN"] == "postgresql://u:p@h:6543/db"


def test_export_noop_when_sqlite(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("sqlite", None))
    run_mod._export_kanban_backend_env()
    assert os.environ.get("HERMES_KANBAN_BACKEND") is None


def test_export_does_not_overwrite_existing_dsn(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", "postgresql://existing@h/db")
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("postgres", "postgresql://new@h/db"))
    run_mod._export_kanban_backend_env()
    # existing env DSN is respected (not clobbered)
    assert os.environ["HERMES_KANBAN_PG_DSN"] == "postgresql://existing@h/db"
    assert os.environ["HERMES_KANBAN_BACKEND"] == "postgres"


def test_export_does_not_clobber_existing_backend(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "sqlite")
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("postgres", "postgresql://u:p@h/db"))
    run_mod._export_kanban_backend_env()
    assert os.environ["HERMES_KANBAN_BACKEND"] == "sqlite"


def test_resolve_falls_back_to_sqlite_when_resolve_backend_raises(monkeypatch):
    import hermes_cli.kanban.store as store_mod
    monkeypatch.setattr(store_mod, "resolve_backend",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert run_mod._resolve_kanban_backend_for_export() == ("sqlite", None)


def test_resolve_returns_postgres_none_when_dsn_raises(monkeypatch):
    import hermes_cli.kanban.store as store_mod
    import hermes_cli.kanban.pg_pool as pg_pool
    monkeypatch.setattr(store_mod, "resolve_backend", lambda: "postgres")
    monkeypatch.setattr(pg_pool, "resolve_dsn",
                        lambda: (_ for _ in ()).throw(RuntimeError("no dsn")))
    assert run_mod._resolve_kanban_backend_for_export() == ("postgres", None)
