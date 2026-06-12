from tools.claude_session.hooks import (
    build_settings, ready_channel, done_channel, input_channel,
)


def test_channels_are_namespaced():
    assert ready_channel("abc") == "cs-ready-abc"
    assert done_channel("abc") == "cs-done-abc"
    assert input_channel("abc") == "cs-input-abc"


def test_settings_signal_with_dash_S():
    h = build_settings("abc", input_flag="/tmp/abc.input")["hooks"]
    assert h["SessionStart"][0]["hooks"][0]["command"] == "tmux wait-for -S cs-ready-abc"
    assert h["Stop"][0]["hooks"][0]["command"] == "tmux wait-for -S cs-done-abc"
    notif = h["Notification"][0]["hooks"][0]["command"]
    assert "tmux wait-for -S cs-input-abc" in notif and "/tmp/abc.input" in notif


def test_pretooluse_cap_optional():
    assert "PreToolUse" not in build_settings("abc", input_flag="/f")["hooks"]
    capped = build_settings("abc", input_flag="/f", tool_call_cap=50)["hooks"]
    assert "50" in capped["PreToolUse"][0]["hooks"][0]["command"]
