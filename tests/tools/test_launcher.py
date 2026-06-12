import pytest

from tools.claude_session.launcher import Launcher, HandshakeTimeout


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


def test_turn_allowed_cap():
    assert Launcher.turn_allowed(0, max_turns=1) is True
    assert Launcher.turn_allowed(1, max_turns=1) is False
    assert Launcher.turn_allowed(99, max_turns=None) is True
