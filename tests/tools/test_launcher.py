import pytest

from tools.claude_session.launcher import Launcher, HandshakeTimeout, StartupError


class FakeTmux:
    def __init__(self, wait_results):
        self.calls = []
        self._wait = list(wait_results)

    def run(self, args, timeout=None):
        self.calls.append(args)
        # a WAIT call is `wait-for <chan>` with no -S/-U (those are signal/unlock)
        if args and args[0] == "wait-for" and "-S" not in args and "-U" not in args:
            if not self._wait.pop(0):
                raise HandshakeTimeout(args[-1])
        return ""


class FakeTmuxWithPane(FakeTmux):
    """FakeTmux that also returns a fixed pane for capture-pane calls."""

    def __init__(self, wait_results, pane=""):
        super().__init__(wait_results)
        self._pane = pane

    def run(self, args, timeout=None):
        if args and args[0] == "capture-pane":
            self.calls.append(args)
            return self._pane
        return super().run(args, timeout=timeout)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # The first-prompt submit-retry uses time.sleep between confirmation Enters;
    # neutralize it so the suite stays fast and deterministic.
    monkeypatch.setattr("tools.claude_session.launcher.time.sleep", lambda *a, **k: None)


def test_launch_is_dialogfree_and_waits_plain(tmp_path):
    fake = FakeTmux([True])
    Launcher(tmux=fake, projects_dir=tmp_path).launch(
        name="t1", uuid="u1", workdir="/w", settings_json='{"hooks":{}}',
        model="opus", ready_timeout=5)
    flat = " ".join(" ".join(c) for c in fake.calls)
    assert "new-session" in flat and "-s t1" in flat
    assert "--session-id u1" in flat
    assert "--permission-mode bypassPermissions" in flat
    assert "--dangerously-skip-permissions" not in flat
    assert "--settings" in flat
    assert "wait-for cs-ready-u1" in flat
    assert "wait-for -L" not in flat            # must NOT use the mutex primitive


def test_send_is_literal_then_enter_then_waits(tmp_path):
    fake = FakeTmux([True])
    Launcher(tmux=fake, projects_dir=tmp_path).send(
        name="t1", uuid="u1", prompt="-weird --looking", done_timeout=5)
    sends = [c for c in fake.calls if c[:2] == ["send-keys", "-t"]]
    assert any("-l" in c and "--" in c for c in sends)
    assert ["send-keys", "-t", "t1", "Enter"] in fake.calls
    assert "wait-for cs-done-u1" in " ".join(" ".join(c) for c in fake.calls)


def test_send_text_is_literal_then_enter_and_does_not_wait(tmp_path):
    fake = FakeTmux([])
    Launcher(tmux=fake, projects_dir=tmp_path).send_text(name="t1", text="/compact")
    assert ["send-keys", "-t", "t1", "-l", "--", "/compact"] in fake.calls
    assert ["send-keys", "-t", "t1", "Enter"] in fake.calls
    assert not any(c and c[0] == "wait-for" for c in fake.calls)  # steer/slash don't block


def test_ready_timeout_raises(tmp_path):
    fake = FakeTmux([False])
    with pytest.raises(HandshakeTimeout):
        Launcher(tmux=fake, projects_dir=tmp_path).launch(
            name="t1", uuid="u1", workdir="/w", settings_json="{}",
            model="opus", ready_timeout=5)


def test_send_fails_fast_on_startup_model_error(tmp_path):
    # cs-done never fires (every slice times out) but the pane shows the
    # model-rejection banner → StartupError instead of burning the full
    # done_timeout. Regression for the 30-min "Initializing agent..." hang.
    fake = FakeTmuxWithPane(
        [False] * 8,
        pane="There's an issue with the selected model (claude-opus-4.8). "
             "It may not exist or you may not have access to it.",
    )
    with pytest.raises(StartupError):
        Launcher(tmux=fake, projects_dir=tmp_path).send(
            name="t1", uuid="u1", prompt="do work", done_timeout=300, startup_grace=15)
    # Bailed during the startup window, not after the whole budget: only one
    # short poll slice elapsed before the banner was seen.
    done_waits = [c for c in fake.calls
                  if c and c[0] == "wait-for" and "-S" not in c]
    assert done_waits == [["wait-for", "cs-done-u1"]]
    assert any(c and c[0] == "capture-pane" for c in fake.calls)


def test_send_returns_immediately_when_done_fires_first(tmp_path):
    # Healthy fast turn: cs-done arrives on the first slice → return without
    # ever scanning the pane (no false-positive surface).
    fake = FakeTmuxWithPane([True], pane="ignored")
    chan = Launcher(tmux=fake, projects_dir=tmp_path).send(
        name="t1", uuid="u1", prompt="do work", done_timeout=300, startup_grace=15)
    assert chan == "cs-done-u1"
    assert not any(c and c[0] == "capture-pane" for c in fake.calls)


def test_send_clean_pane_waits_past_grace_then_returns(tmp_path):
    # Slow but healthy turn: the grace window elapses on a clean pane (no
    # banner), then cs-done fires on the post-grace blocking wait. Must NOT
    # raise — the guard only trips on a known fatal banner.
    fake = FakeTmuxWithPane([False] * 5 + [True], pane="✻ Crunching… working")
    chan = Launcher(tmux=fake, projects_dir=tmp_path).send(
        name="t1", uuid="u1", prompt="do work", done_timeout=300, startup_grace=15)
    assert chan == "cs-done-u1"


def test_send_startup_grace_zero_does_single_blocking_wait(tmp_path):
    # Warm re-send path opts out (startup_grace=0): one plain wait, no polling
    # slices and no pane scan, identical to the pre-guard behaviour.
    fake = FakeTmuxWithPane([True], pane="There's an issue with the selected model")
    chan = Launcher(tmux=fake, projects_dir=tmp_path).send(
        name="t1", uuid="u1", prompt="do work", done_timeout=300, startup_grace=0)
    assert chan == "cs-done-u1"
    assert not any(c and c[0] == "capture-pane" for c in fake.calls)


def test_launch_disables_mcp(tmp_path):
    # Fix A: worker sessions launch with MCP off (--strict-mcp-config + empty
    # config) so the remote-MCP startup init can't widen the submit race.
    fake = FakeTmux([True])
    Launcher(tmux=fake, projects_dir=tmp_path).launch(
        name="t1", uuid="u1", workdir="/w", settings_json="{}",
        model="opus", ready_timeout=5)
    flat = " ".join(" ".join(c) for c in fake.calls)
    assert "--strict-mcp-config" in flat
    assert "--mcp-config" in flat
    assert "empty_mcp.json" in flat


def test_first_prompt_waits_for_input_ready(tmp_path):
    # send(..., workdir=) triggers the robust first-prompt path: must call
    # capture-pane to detect the ready indicator ("bypass permissions on")
    # and then send the literal prompt.
    fake = FakeTmuxWithPane([True], pane="bypass permissions on")
    Launcher(tmux=fake, projects_dir=tmp_path).send(
        name="t1", uuid="u1", prompt="do work", done_timeout=5,
        workdir="/w", submit_retries=0)
    assert any(c and c[0] == "capture-pane" for c in fake.calls)
    assert ["send-keys", "-t", "t1", "-l", "--", "do work"] in fake.calls


def test_first_prompt_resends_full_prompt_until_turn_starts(tmp_path):
    # No transcript → _turn_started is False for every retry check → the full
    # prompt is re-sent on each retry (initial + 2 retries = 3 literal sends).
    wd = tmp_path / "wd"
    wd.mkdir()
    fake = FakeTmuxWithPane([True], pane="bypass permissions on")
    Launcher(tmux=fake, projects_dir=tmp_path).send(
        name="t1", uuid="u1", prompt="do work", done_timeout=5,
        workdir=str(wd), submit_retries=2)
    literal_sends = [c for c in fake.calls
                     if c == ["send-keys", "-t", "t1", "-l", "--", "do work"]]
    assert len(literal_sends) > 1  # initial + at least one resend


def test_first_prompt_stops_once_turn_started(tmp_path):
    # If the transcript already has a 'user' record, _turn_started returns True
    # and the resend loop exits immediately → the literal prompt is sent EXACTLY
    # ONCE (no double-submit). This is the critical safety invariant.
    import re as _re
    workdir = str(tmp_path / "wd")
    proj_key = _re.sub(r"[/.]", "-", workdir)
    proj_dir = tmp_path / "projects" / proj_key
    proj_dir.mkdir(parents=True)
    (proj_dir / "u1.jsonl").write_text('{"type":"user","message":{}}\n')

    fake = FakeTmuxWithPane([True], pane="bypass permissions on")
    Launcher(tmux=fake, projects_dir=tmp_path / "projects").send(
        name="t1", uuid="u1", prompt="do work", done_timeout=5,
        workdir=workdir, submit_retries=2)
    literal_sends = [c for c in fake.calls
                     if c == ["send-keys", "-t", "t1", "-l", "--", "do work"]]
    assert len(literal_sends) == 1  # turn already started → no resend


def test_send_text_default_is_single_enter(tmp_path):
    # Steer/slash path (send_text directly) runs on a warm TUI → no retry, one Enter.
    fake = FakeTmux([])
    Launcher(tmux=fake, projects_dir=tmp_path).send_text(name="t1", text="/compact")
    enters = [c for c in fake.calls if c == ["send-keys", "-t", "t1", "Enter"]]
    assert len(enters) == 1


def test_turn_allowed_cap():
    assert Launcher.turn_allowed(0, max_turns=1) is True
    assert Launcher.turn_allowed(1, max_turns=1) is False
    assert Launcher.turn_allowed(99, max_turns=None) is True
