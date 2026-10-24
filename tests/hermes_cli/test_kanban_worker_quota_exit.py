"""A kanban worker whose turn fails purely on a provider quota wall must exit with the
EX_TEMPFAIL sentinel so the dispatcher requeues it without counting a failure.

Before this, only the fully-quiet ``-Q`` path carried the sentinel. The dispatcher
spawns ``chat -q``, so every quota-walled worker exited 0 and was booked as a
protocol violation (178 Boris runs through 2026-09-06, then gave_up -> blocked)."""

from types import SimpleNamespace

import pytest

import cli as cli_mod
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE


def _failed_turn(reason):
    return {
        "final_response": "API call failed after 3 retries: HTTP 429: The usage limit has been reached",
        "messages": [],
        "api_calls": 1,
        "completed": False,
        "failed": True,
        "error": "HTTP 429: The usage limit has been reached",
        "failure_reason": reason,
    }


class _OneShotCLI:
    """Just enough of HermesCLI for the non-quiet ``-q`` branch of main()."""

    turn_result: dict = {}
    calls: list = []

    def __init__(self, **_kwargs):
        self.console = SimpleNamespace(print=lambda *_a, **_kw: None)
        self.session_id = "sq-quota"
        self.agent = SimpleNamespace(session_id="sq-quota", platform="cli")

    def _claim_active_session(self, surface, *, stderr=False):
        return True

    def _show_security_advisories(self):
        pass

    def chat(self, query, images=None):
        self._last_turn_result = type(self).turn_result
        return self._last_turn_result.get("final_response")

    def _print_exit_summary(self, clear_screen=True):
        type(self).calls.append("summary")


def _isolate_main(monkeypatch, fake, calls):
    monkeypatch.setattr(cli_mod, "HermesCLI", fake)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: calls.append("finalize"))
    # main() installs real signal handlers and sets these directly; keep the test
    # process clean and let monkeypatch restore the env afterwards.
    monkeypatch.setattr(cli_mod, "_install_single_query_signal_handlers", lambda _cli: None)
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "0")
    monkeypatch.setenv("HERMES_INTERACTIVE", "0")


def _run_one_shot(monkeypatch, turn_result):
    calls: list = []
    fake = type("FakeCLI", (_OneShotCLI,), {"turn_result": turn_result, "calls": calls})
    _isolate_main(monkeypatch, fake, calls)
    # oneshot=True: the dispatcher's worker has no TTY; on a developer's terminal
    # main() would otherwise seed an interactive session instead.
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(query="work kanban task t_quota", quiet=False, toolsets="terminal", oneshot=True)
    return calls, excinfo.value.code


def test_single_query_mode_exits_tempfail_for_a_quota_walled_kanban_worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_quota")

    _calls, code = _run_one_shot(monkeypatch, _failed_turn("rate_limit"))

    assert code == KANBAN_RATE_LIMIT_EXIT_CODE


def test_single_query_mode_still_finalizes_before_the_tempfail_exit(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_quota")
    calls: list = []
    fake = type("FakeCLI", (_OneShotCLI,), {"turn_result": _failed_turn("billing"), "calls": calls})
    _isolate_main(monkeypatch, fake, calls)

    with pytest.raises(SystemExit):
        cli_mod.main(query="work kanban task t_quota", quiet=False, toolsets="terminal", oneshot=True)

    assert calls == ["summary", "finalize"]


def test_single_query_mode_returns_normally_after_a_completed_turn(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_quota")

    calls, code = _run_one_shot(monkeypatch, {"final_response": "done", "completed": True, "failed": False})

    assert (calls, code) == (["summary", "finalize"], 0)


def test_single_query_mode_ignores_quota_failures_outside_kanban(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    calls, code = _run_one_shot(monkeypatch, _failed_turn("rate_limit"))

    assert (calls, code) == (["summary", "finalize"], 1)


class _QuietCLI:
    def __init__(self, result):
        self.agent = SimpleNamespace(run_conversation=lambda **_kwargs: result)
        self.conversation_history: list = []
        self.session_id = "sq-quiet"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_failed_turn("rate_limit"), KANBAN_RATE_LIMIT_EXIT_CODE),
        (_failed_turn("billing"), KANBAN_RATE_LIMIT_EXIT_CODE),
        (_failed_turn("provider"), 1),
        ({"final_response": "done", "completed": True, "failed": False}, 0),
    ],
)
def test_fully_quiet_path_keeps_its_exit_code_contract(monkeypatch, capsys, result, expected):
    """-Q: 75 for a kanban quota wall, 1 for any other failure, 0 on success."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_quota")
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.setattr(cli_mod, "_sync_cli_session_id_from_agent", lambda _cli: None)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod._run_quiet_single_query(_QuietCLI(result), "work kanban task t_quota")

    assert excinfo.value.code == expected
    assert "session_id: sq-quiet" in capsys.readouterr().err
