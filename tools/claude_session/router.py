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
    # The binary exists and responds. Don't pin a major-version prefix: that
    # would silently force every call onto the -p fallback when Claude bumps to
    # 3.x. (`--session-id`, the only version-sensitive flag we rely on, has been
    # stable across 2.x.)
    return out.returncode == 0


def decide_path(*, no_tmux: bool, tmux_available: bool,
                claude_version_ok: bool) -> Decision:
    """Pre-flight path choice. Runtime-failure fallbacks (handshake timeouts)
    are handled in dispatch, not here."""
    if no_tmux or not tmux_available or not claude_version_ok:
        return Decision.PRINT
    return Decision.TMUX
