from tools.claude_session.router import Decision, decide_path


def test_tmux_default_when_available():
    assert decide_path(no_tmux=False, tmux_available=True,
                       claude_version_ok=True) == Decision.TMUX


def test_no_tmux_override():
    assert decide_path(no_tmux=True, tmux_available=True,
                       claude_version_ok=True) == Decision.PRINT


def test_missing_tmux_falls_back():
    assert decide_path(no_tmux=False, tmux_available=False,
                       claude_version_ok=True) == Decision.PRINT


def test_bad_claude_version_falls_back():
    assert decide_path(no_tmux=False, tmux_available=True,
                       claude_version_ok=False) == Decision.PRINT
