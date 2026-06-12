from __future__ import annotations

from typing import Any, Dict, Optional


def ready_channel(sid: str) -> str: return f"cs-ready-{sid}"
def done_channel(sid: str) -> str: return f"cs-done-{sid}"
def input_channel(sid: str) -> str: return f"cs-input-{sid}"


def _cmd(command: str) -> Dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def build_settings(sid: str, *, input_flag: str,
                   tool_call_cap: Optional[int] = None) -> Dict[str, Any]:
    """Per-session settings injected via `--settings '<json>'`.

    Hooks SIGNAL the tmux bus with `wait-for -S`; the launcher blocks on the
    paired plain `wait-for` (signals are latched, so signal-before-wait is safe).
    """
    hooks: Dict[str, Any] = {
        "SessionStart": [_cmd(f"tmux wait-for -S {ready_channel(sid)}")],
        "Stop": [_cmd(f"tmux wait-for -S {done_channel(sid)}")],
        "Notification": [
            _cmd(f"touch {input_flag} && tmux wait-for -S {input_channel(sid)}")
        ],
    }
    if tool_call_cap is not None:
        counter = f"{input_flag}.tools"
        hooks["PreToolUse"] = [_cmd(
            f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; "
            f"if [ $n -gt {tool_call_cap} ]; then echo 'tool cap' >&2; exit 2; fi"
        )]
    return {"hooks": hooks}
