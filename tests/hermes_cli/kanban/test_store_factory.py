import hermes_cli.config as cfg


def test_kanban_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {})
    from hermes_cli.kanban.store import resolve_backend
    assert resolve_backend() == "sqlite"


def test_kanban_backend_reads_config(monkeypatch):
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"backend": "postgres"}})
    from hermes_cli.kanban.store import resolve_backend
    assert resolve_backend() == "postgres"
