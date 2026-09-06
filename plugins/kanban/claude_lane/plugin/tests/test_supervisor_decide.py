# tests/test_supervisor_decide.py
import importlib.util
import os
import types

import pytest

# The installed supervisor by default; CLAUDE_LANE_SUPERVISOR points this at the
# source-controlled copy so the same assertions run against the artifact BEFORE
# it is installed (checkout-pinned) and against the real installed file after.
_SUPERVISOR = os.environ.get("CLAUDE_LANE_SUPERVISOR") or os.path.expanduser(
    "~/.hermes/scripts/claude_kanban_supervisor.py")
_spec = importlib.util.spec_from_file_location("cks", _SUPERVISOR)
cks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cks)


def test_decide_terminal_when_card_left_running():
    d = cks.decide(status="done", worker_pid_matches=True, transcript_tail="",
                   idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("terminal", None)


def test_decide_reclaimed_when_pid_mismatch():
    d = cks.decide(status="running", worker_pid_matches=False, transcript_tail="",
                   idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("terminal", None)  # not ours anymore -> stop, do not block


def test_decide_weekly_cap_from_transcript():
    d = cks.decide(status="running", worker_pid_matches=True,
                   transcript_tail="Claude usage limit reached — resets at 4am",
                   idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("weekly_cap", None)


def test_decide_does_not_false_trigger_on_ratelimit_prose():
    # a card whose WORK mentions rate limiting must NOT be treated as a provider cap
    d = cks.decide(status="running", worker_pid_matches=True,
                   transcript_tail="Implemented the rate limit middleware; handles 429 and overloaded backends.",
                   idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("continue", None)


def test_decide_rate_limit_from_transcript():
    d = cks.decide(status="running", worker_pid_matches=True,
                   transcript_tail='API Error: 429 {"type":"rate_limit_error"}',
                   idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("rate_limit", None)


def test_decide_idle_block():
    d = cks.decide(status="running", worker_pid_matches=True, transcript_tail="",
                   idle_seconds=1300, elapsed=1400, idle_timeout=1200, max_runtime=5400)
    assert d[0] == "block"


def test_decide_max_runtime_block():
    d = cks.decide(status="running", worker_pid_matches=True, transcript_tail="",
                   idle_seconds=0, elapsed=5500, idle_timeout=1200, max_runtime=5400)
    assert d[0] == "block"


def test_decide_continue():
    d = cks.decide(status="running", worker_pid_matches=True, transcript_tail="working...",
                   idle_seconds=5, elapsed=30, idle_timeout=1200, max_runtime=5400)
    assert d == ("continue", None)


# -- MAJOR 3+6: transient read failure must not kill the live session --------
def test_caller_decision_transient_read_failure_continues():
    # read_ok=False (e.g. SQLite 'disk I/O error') -> do NOT act this tick.
    d = cks.caller_decision(False, None, my_pid=42, transcript_tail="",
                            idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("continue", None)


def test_caller_decision_genuinely_absent_card_terminal():
    # read_ok=True but card is None -> genuinely gone -> terminal.
    d = cks.caller_decision(True, None, my_pid=42, transcript_tail="",
                            idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("terminal", None)


def test_caller_decision_running_ours_continues():
    card = types.SimpleNamespace(status="running", worker_pid=42)
    d = cks.caller_decision(True, card, my_pid=42, transcript_tail="working...",
                            idle_seconds=5, elapsed=30, idle_timeout=1200, max_runtime=5400)
    assert d == ("continue", None)


def test_caller_decision_pid_mismatch_terminal():
    # ours-running but a DIFFERENT pid owns the card now -> terminal (stop, no block).
    card = types.SimpleNamespace(status="running", worker_pid=999)
    d = cks.caller_decision(True, card, my_pid=42, transcript_tail="",
                            idle_seconds=0, elapsed=10, idle_timeout=1200, max_runtime=5400)
    assert d == ("terminal", None)


# -- BLOCKER 1: supervisor shim calls must carry an authorizing env ----------
def test_hermes_task_shim_sets_authorizing_env(monkeypatch):
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cks.subprocess, "run", fake_run)
    cks._hermes_task_shim("t_1", "default", "heartbeat", "t_1", run_id=1514,
                          claim_lock="host:1")
    assert captured["env"] is not None
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_1"
    assert captured["env"]["HERMES_KANBAN_BOARD"] == "default"
    # ... and FENCED: without HERMES_KANBAN_RUN_ID the core heartbeat runs with
    # expected_run_id=None, which is how a run-N supervisor renewed run N+1's claim.
    assert captured["env"]["HERMES_KANBAN_RUN_ID"] == "1514"
    assert captured["env"]["HERMES_KANBAN_CLAIM_LOCK"] == "host:1"
    # argv after the shim path are passed through verbatim
    assert captured["cmd"][1:] == ["heartbeat", "t_1"]


@pytest.mark.parametrize("bad", [None, 0, -1])
def test_hermes_task_shim_refuses_an_unfenced_call(monkeypatch, bad):
    """There is no unfenced lifecycle call. The run is a required keyword and a
    non-positive one raises, so a bug upstream surfaces as a loud failure rather
    than a heartbeat that quietly addresses whichever run is current."""
    ran = []
    monkeypatch.setattr(cks.subprocess, "run",
                        lambda cmd, *a, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0))
    with pytest.raises(Exception):
        cks._hermes_task_shim("t_1", "default", "heartbeat", "t_1", run_id=bad)
    assert ran == []


# -- MAJOR 5 (security): fail-closed redaction masker ------------------------
def test_fallback_redact_masks_secret_shapes():
    assert "sk-ant-oat01-SECRETSECRET" not in cks._fallback_redact("x sk-ant-oat01-SECRETSECRET y")
    jwt = "eyJabc1234567890.payloadsegment.signature"
    assert jwt not in cks._fallback_redact("token " + jwt)
    assert cks._fallback_redact("") == ""
    assert cks._fallback_redact("no secrets here") == "no secrets here"


def test_caller_decision_has_no_ownership_override():
    """A foreign LIVE owner on a readable card is always a stand-down.

    There used to be an ``assume_ours`` escape hatch for a readopt that could
    not verify its ownership write, and it was permanent: the process ignored
    the pid check for its whole life, so a supervisor that never confirmed
    ownership kept running beside the one that had it (my_pid 11111 vs a card
    naming live pid 99999 still returned 'continue'). Ownership is now settled
    BEFORE the loop starts, so the override is gone entirely — passing it is a
    TypeError rather than a quietly-honoured request.
    """
    foreign = types.SimpleNamespace(status="running", worker_pid=99999)
    assert cks.caller_decision(True, foreign, 11111, transcript_tail="", idle_seconds=0,
                               elapsed=10, idle_timeout=1200, max_runtime=5400) \
        == ("terminal", None)
    with pytest.raises(TypeError):
        cks.caller_decision(True, foreign, 11111, transcript_tail="", idle_seconds=0,
                            elapsed=10, idle_timeout=1200, max_runtime=5400,
                            assume_ours=True)
    # An unrecorded owner is still tolerated: nobody has claimed the card, which
    # is not evidence against us (our own declaration can be delayed by a
    # transient shim failure).
    unowned = types.SimpleNamespace(status="running", worker_pid=None)
    assert cks.caller_decision(True, unowned, 11111, transcript_tail="", idle_seconds=0,
                               elapsed=10, idle_timeout=1200, max_runtime=5400) \
        == ("continue", None)


# -- cold-start flake detection: transient early pane death -> requeue, not block --
def test_coldstart_flake_when_no_progress_and_early():
    # Pane died ~1s in having written NOTHING to the transcript: the classic
    # cold-start flake (recovers on retry) -> requeue, do not terminally block.
    assert cks.is_coldstart_flake(made_progress=False, elapsed=1, coldstart_grace=60) is True


def test_not_coldstart_flake_when_progress_made():
    # claude wrote transcript output before the pane died -> a genuine mid-run
    # failure worth surfacing, NOT a startup flake -> block (caller default).
    assert cks.is_coldstart_flake(made_progress=True, elapsed=1, coldstart_grace=60) is False


def test_not_coldstart_flake_after_grace_window():
    # No output but the pane survived well past the cold-start window before
    # dying -> not a startup flake -> block.
    assert cks.is_coldstart_flake(made_progress=False, elapsed=120, coldstart_grace=60) is False


def test_coldstart_flake_at_grace_boundary_is_exclusive():
    # elapsed == grace is NOT within the window (strict <), so it blocks.
    assert cks.is_coldstart_flake(made_progress=False, elapsed=60, coldstart_grace=60) is False


def test_coldstart_requeue_exit_code_requeues_not_blocks():
    # The exit code must NOT be 0 (the dispatcher reads a clean exit on a still-
    # running task as a protocol violation and blocks it) and must NOT be the
    # rate-limit sentinel (75 carries quota-deferral semantics). Any other
    # nonzero lands in detect_crashed_workers' ``crashed`` bucket -> requeue.
    assert cks.COLDSTART_REQUEUE_EXIT_CODE != 0
    assert cks.COLDSTART_REQUEUE_EXIT_CODE != cks.KANBAN_RATE_LIMIT_EXIT_CODE


def test_cleanup_cfgdir_removes_dir(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "settings.json").write_text("{}", encoding="utf-8")
    cks._cleanup_cfgdir(str(d))
    assert not d.exists()
    cks._cleanup_cfgdir("")  # empty/no-op must not raise
