def test_export_kanban_backend_env_sets_env(monkeypatch):
    import gateway.run as run_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_PG_DSN", raising=False)
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("postgres", "postgresql://u:p@h:6543/db"))
    run_mod._export_kanban_backend_env()
    import os
    assert os.environ["HERMES_KANBAN_BACKEND"] == "postgres"
    assert os.environ["HERMES_KANBAN_PG_DSN"] == "postgresql://u:p@h:6543/db"


def test_export_noop_when_sqlite(monkeypatch):
    import os
    import gateway.run as run_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("sqlite", None))
    run_mod._export_kanban_backend_env()
    assert os.environ.get("HERMES_KANBAN_BACKEND") in (None, "")


def test_export_does_not_overwrite_existing_dsn(monkeypatch):
    import os
    import gateway.run as run_mod
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", "postgresql://existing@h/db")
    monkeypatch.setattr(run_mod, "_resolve_kanban_backend_for_export",
                        lambda: ("postgres", "postgresql://new@h/db"))
    run_mod._export_kanban_backend_env()
    # existing env DSN is respected (not clobbered)
    assert os.environ["HERMES_KANBAN_PG_DSN"] == "postgresql://existing@h/db"
    assert os.environ["HERMES_KANBAN_BACKEND"] == "postgres"
