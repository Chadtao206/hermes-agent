"""C2 / WS2-Task-3: the gateway watchdog keeps the writer daemon alive.

A dead writer-loop thread (recovery NOT exhausted) is revived in place so
in-process writes resume without a gateway restart. When recovery IS exhausted
(``health()["disabled"]``) the watchdog emits a high-severity alert exactly
once instead of churn-restarting a board a human must repair.
"""
import gateway.run as gr
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_daemon as wd


def _make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    return runner


def test_watchdog_revives_dead_writer_thread(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)

    started = gr._spawn_writer_daemons([db], auto_recovery=False)
    daemon = started[0]
    try:
        # Kill the writer-loop thread (queue sentinel → loop exits + closes conn).
        daemon._queue.put(None)
        daemon._writer_thread.join(timeout=5)
        assert not daemon.is_alive()

        runner = _make_runner()
        runner._kanban_writer_daemons = list(started)
        runner._writer_watchdog_tick()

        assert daemon.is_alive()
        # In-process writes work again after revival.
        new_id = daemon.execute("create_task", title="revived", assignee="worker")
        ro = kb.connect(db_path=db, readonly=True)
        try:
            assert ro.execute(
                "SELECT title FROM tasks WHERE id=?", (new_id,)
            ).fetchone()["title"] == "revived"
        finally:
            ro.close()
    finally:
        for d in started:
            wd.unregister_daemon(d.db_path)
            d.shutdown()


def test_watchdog_alerts_once_when_recovery_exhausted(tmp_path, monkeypatch, caplog):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)

    started = gr._spawn_writer_daemons([db], auto_recovery=False)
    daemon = started[0]
    try:
        # Simulate recovery exhaustion: the writer thread stays alive but the
        # board is disabled.
        daemon._disabled = {"reason": "recovery_exhausted", "method": "exhausted"}
        assert daemon.is_alive()

        runner = _make_runner()
        runner._kanban_writer_daemons = list(started)

        alerts = []
        monkeypatch.setattr(
            runner, "_emit_writer_daemon_alert",
            lambda db_path, health: alerts.append(str(db_path)),
            raising=False,
        )

        runner._writer_watchdog_tick()
        runner._writer_watchdog_tick()  # second tick must NOT re-alert

        assert alerts == [str(db.resolve())]
    finally:
        for d in started:
            wd.unregister_daemon(d.db_path)
            d.shutdown()


def test_watchdog_noop_when_flag_off(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)

    runner = _make_runner()
    runner._kanban_writer_daemons = []
    # Must not raise with the flag off and no daemons.
    runner._writer_watchdog_tick()
