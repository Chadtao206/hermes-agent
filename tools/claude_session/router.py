from __future__ import annotations

import enum
import shutil
import subprocess


class Decision(enum.Enum):
    TMUX = "tmux"
    PRINT = "print"


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def claude_version_ok() -> bool:
    exe = shutil.which("claude")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return False
    return out.returncode == 0 and "2." in out.stdout


def decide_path(*, no_tmux: bool, tmux_available: bool,
                claude_version_ok: bool) -> Decision:
    """Pre-flight path choice. Runtime-failure fallbacks (handshake timeouts)
    are handled in dispatch, not here."""
    if no_tmux or not tmux_available or not claude_version_ok:
        return Decision.PRINT
    return Decision.TMUX
