"""WS6 Task 3: the gateway liveness alerter dedups — one page per breach
window, re-arming after the breach clears."""
import gateway.run as gr
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
