"""Under backend=postgres the gateway must NOT start the sqlite writer daemon
or its watchdog (the daemon would needlessly open the frozen kanban.db + WAL).
Under sqlite, behavior is unchanged."""
import types

import gateway.run as gr


def _stub():
    s = types.SimpleNamespace(_kanban_writer_daemons=[])
    s._writer_daemon_should_run = gr.GatewayRunner._writer_daemon_should_run.__get__(s)
    return s


def _patch_backend(monkeypatch, backend):
    import hermes_cli.kanban.store as store_mod
    monkeypatch.setattr(store_mod, "resolve_backend", lambda: backend, raising=True)


def test_should_run_false_under_postgres_even_with_flag_on(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    _patch_backend(monkeypatch, "postgres")
    assert _stub()._writer_daemon_should_run() is False


def test_should_run_true_under_sqlite_with_flag_on(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    _patch_backend(monkeypatch, "sqlite")
    assert _stub()._writer_daemon_should_run() is True


def test_should_run_false_under_sqlite_with_flag_off(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: False, raising=True)
    _patch_backend(monkeypatch, "sqlite")
    assert _stub()._writer_daemon_should_run() is False


def test_should_run_falls_back_to_flag_when_resolve_raises(monkeypatch):
    import hermes_cli.kanban.store as store_mod
    import hermes_cli.kanban_db as kb
    def _boom():
        raise RuntimeError("config blip")
    monkeypatch.setattr(store_mod, "resolve_backend", _boom, raising=True)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    assert _stub()._writer_daemon_should_run() is True


def test_start_writer_daemon_is_noop_under_postgres(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    _patch_backend(monkeypatch, "postgres")
    called = {"spawn": False}
    monkeypatch.setattr(gr, "_spawn_writer_daemons",
                        lambda *a, **k: called.__setitem__("spawn", True) or [],
                        raising=True)
    s = _stub()
    gr.GatewayRunner._start_kanban_writer_daemon(s)
    assert called["spawn"] is False
    assert s._kanban_writer_daemons == []


def test_start_writer_daemon_spawns_under_sqlite(monkeypatch):
    import hermes_cli.kanban_db as kb
    monkeypatch.setattr(kb, "single_writer_enabled", lambda *a, **k: True, raising=True)
    monkeypatch.setattr(kb, "list_boards", lambda **k: [{"slug": "default"}], raising=True)
    monkeypatch.setattr(kb, "kanban_db_path", lambda **k: __import__("pathlib").Path("/tmp/x/kanban.db"), raising=True)
    _patch_backend(monkeypatch, "sqlite")
    spawned = {"called": False}
    monkeypatch.setattr(gr, "_spawn_writer_daemons",
                        lambda *a, **k: spawned.__setitem__("called", True) or ["d"],
                        raising=True)
    s = _stub()
    s._kanban_writer_recovery_cfg = lambda: (False, 5, 60)
    gr.GatewayRunner._start_kanban_writer_daemon(s)
    assert spawned["called"] is True
