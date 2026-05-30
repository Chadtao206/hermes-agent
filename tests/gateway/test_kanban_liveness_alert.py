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
