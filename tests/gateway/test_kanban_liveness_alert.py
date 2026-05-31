"""WS6 Task 3: the gateway liveness alerter dedups — one page per breach
window, re-arming after the breach clears."""
import types

import gateway.run as gr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_liveness as liv


def test_alert_fires_once_per_breach_window():
    sent = []
    state: dict = {}
    breaches = [liv.Breach("oldest_ready_age_seconds", 9000, 600)]
    gr._maybe_emit_liveness_alert(breaches, board="default", state=state, emit=sent.append)
    gr._maybe_emit_liveness_alert(breaches, board="default", state=state, emit=sent.append)
    assert len(sent) == 1  # same breach signature does not re-page


def test_alert_refires_after_clear():
    sent = []
    state: dict = {}
    b = [liv.Breach("oldest_ready_age_seconds", 9000, 600)]
    gr._maybe_emit_liveness_alert(b, board="default", state=state, emit=sent.append)
    gr._maybe_emit_liveness_alert([], board="default", state=state, emit=sent.append)  # cleared
    gr._maybe_emit_liveness_alert(b, board="default", state=state, emit=sent.append)  # re-breach
    assert len(sent) == 2


def test_alert_refires_when_breach_set_changes():
    sent = []
    state: dict = {}
    one = [liv.Breach("oldest_ready_age_seconds", 9000, 600)]
    two = [liv.Breach("oldest_ready_age_seconds", 9000, 600),
           liv.Breach("writer_daemon_disabled", 1, 0)]
    gr._maybe_emit_liveness_alert(one, board="default", state=state, emit=sent.append)
    gr._maybe_emit_liveness_alert(two, board="default", state=state, emit=sent.append)
    assert len(sent) == 2  # a new dimension joining the breach re-pages


def test_alert_is_per_board():
    sent = []
    state: dict = {}
    b = [liv.Breach("oldest_ready_age_seconds", 9000, 600)]
    gr._maybe_emit_liveness_alert(b, board="alpha", state=state, emit=sent.append)
    gr._maybe_emit_liveness_alert(b, board="beta", state=state, emit=sent.append)
    assert len(sent) == 2  # independent dedup state per board


def test_pg_branch_drives_snapshot(tmp_path, monkeypatch):
    """PG branch: when resolve_backend() returns 'postgres', the liveness snapshot
    is sourced from compute_board_liveness_pg, and a breaching snapshot triggers
    the alert.  Proves the PG code-path is actually exercised."""
    board_db = tmp_path / "nope.db"
    monkeypatch.setattr(
        kb, "list_boards",
        lambda include_archived=False: [{"slug": "default", "db_path": str(board_db)}],
    )
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: board_db)

    # Route _run_liveness_check_once into the postgres branch.
    monkeypatch.setattr(
        "hermes_cli.kanban.store.resolve_backend",
        lambda: "postgres",
        raising=False,
    )

    # Minimal fake psycopg pool/connection/cursor.
    class _FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return self
        def fetchone(self): return [0]

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self, **k): return _FakeCur()
        def execute(self, *a, **k): return self

    class _FakePool:
        def connection(self, *a, **k): return _FakeConn()

    monkeypatch.setattr(
        "hermes_cli.kanban.pg_pool.get_pool",
        lambda *a, **k: _FakePool(),
        raising=False,
    )

    # Force a breaching snapshot from the PG path.
    monkeypatch.setattr(
        "hermes_cli.kanban_liveness.compute_board_liveness_pg",
        lambda cur, board, *, now: liv.Liveness(oldest_ready_age_seconds=999999),
        raising=False,
    )

    sent = []
    fake = types.SimpleNamespace(
        _liveness_thresholds=lambda: {"oldest_ready_age_seconds": 600},
        _liveness_subsystem_flags=lambda slug, key: (False, False),
        _emit_liveness_alert=lambda alert: sent.append(alert),
    )
    gr.GatewayRunner._run_liveness_check_once(fake, {})

    assert len(sent) == 1, "expected exactly one breach alert from the PG snapshot"
    assert "oldest_ready_age_seconds" in sent[0]


def test_pg_failure_degrades_but_subsystem_breach_fires(tmp_path, monkeypatch):
    """PG branch: when the pool read raises, _run_liveness_check_once must NOT
    raise, and subsystem breaches (e.g. notifier_disabled) are still paged
    because subsystem flags are folded in after the zeroed degraded snapshot."""
    board_db = tmp_path / "nope.db"
    monkeypatch.setattr(
        kb, "list_boards",
        lambda include_archived=False: [{"slug": "default", "db_path": str(board_db)}],
    )
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: board_db)

    # Route into the postgres branch.
    monkeypatch.setattr(
        "hermes_cli.kanban.store.resolve_backend",
        lambda: "postgres",
        raising=False,
    )

    # Make the pool read blow up to simulate PG being unreachable.
    class _BrokenPool:
        def connection(self, *a, **k):
            raise RuntimeError("simulated PG connection failure")

    monkeypatch.setattr(
        "hermes_cli.kanban.pg_pool.get_pool",
        lambda *a, **k: _BrokenPool(),
        raising=False,
    )

    sent = []
    fake = types.SimpleNamespace(
        _liveness_thresholds=lambda: {},
        # notifier_disabled=True — subsystem breach that must still fire.
        _liveness_subsystem_flags=lambda slug, key: (True, False),
        _emit_liveness_alert=lambda alert: sent.append(alert),
    )

    # Must not raise even though PG is broken.
    gr.GatewayRunner._run_liveness_check_once(fake, {})

    assert len(sent) == 1, "expected subsystem breach alert despite PG failure"
    assert "notifier_disabled" in sent[0]


def test_subsystem_alert_fires_when_board_db_missing(tmp_path, monkeypatch):
    """A corrupt/missing board DB must NOT suppress the gateway-local subsystem
    alerts: the writer-daemon-recovery-exhausted page is exactly the case where
    the file is unreadable, so the checker must fold in subsystem flags even
    when it cannot open the board (it previously `continue`d first → silent)."""
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(
        kb, "list_boards",
        lambda include_archived=False: [{"slug": "default", "db_path": str(missing)}],
    )
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: missing)

    sent = []
    fake = types.SimpleNamespace(
        _liveness_thresholds=lambda: {},
        # (notifier_disabled, writer_disabled) — daemon recovery exhausted.
        _liveness_subsystem_flags=lambda slug, key: (False, True),
        _emit_liveness_alert=lambda alert: sent.append(alert),
    )
    gr.GatewayRunner._run_liveness_check_once(fake, {})
    assert len(sent) == 1
    assert "writer_daemon_disabled" in sent[0]
