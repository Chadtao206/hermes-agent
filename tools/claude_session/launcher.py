from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from .hooks import done_channel, ready_channel


class HandshakeTimeout(RuntimeError):
    """A tmux wait-for blocked past its timeout (ready/done never signalled)."""


class TmuxRunner:
    def run(self, args: List[str], timeout: Optional[float] = None) -> str:
        try:
            out = subprocess.run(["tmux", *args], capture_output=True,
                                 text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # plain wait-for (no -S/-U) that timed out = handshake failure
            if args and args[0] == "wait-for" and "-S" not in args and "-U" not in args:
                raise HandshakeTimeout(args[-1] if args else "?")
            raise
        return out.stdout


class Launcher:
    def __init__(self, *, tmux, projects_dir: Path):
        self.tmux = tmux
        self.projects_dir = Path(projects_dir)

    def launch(self, *, name: str, uuid: str, workdir: str, settings_json: str,
               model: str, ready_timeout: float, log_path: Optional[str] = None) -> None:
        self.tmux.run(["new-session", "-d", "-s", name, "-x", "200", "-y", "50"])
        if log_path:
            self.tmux.run(["pipe-pane", "-t", name, f"cat >> {shlex.quote(log_path)}"])
        claude_cmd = (
            f"cd {shlex.quote(workdir)} && claude --session-id {uuid} "
            f"--permission-mode bypassPermissions --model {shlex.quote(model)} "
            f"--settings {shlex.quote(settings_json)}"
        )
        self.tmux.run(["send-keys", "-t", name, claude_cmd, "Enter"])
        # Block on the SessionStart hook signal (plain wait-for; latched).
        self.tmux.run(["wait-for", ready_channel(uuid)], timeout=ready_timeout)

    def send(self, *, name: str, uuid: str, prompt: str, done_timeout: float) -> str:
        self.send_text(name=name, text=prompt)
        self.tmux.run(["wait-for", done_channel(uuid)], timeout=done_timeout)
        return done_channel(uuid)

    def send_text(self, *, name: str, text: str) -> None:
        # -l = literal (no key-name lookup), -- = end of options (text may start
        # with '-'). Submit with a SEPARATE Enter so the text isn't parsed as a
        # key name. Used for prompts (via send) and for steer/slash input.
        # Multi-line text must not contain submit-newlines.
        self.tmux.run(["send-keys", "-t", name, "-l", "--", text])
        self.tmux.run(["send-keys", "-t", name, "Enter"])

    def capture(self, *, name: str, lines: int = 60) -> str:
        return self.tmux.run(["capture-pane", "-t", name, "-p", "-S", f"-{lines}"])

    def kill(self, *, name: str) -> None:
        self.tmux.run(["kill-session", "-t", name])

    def pane_dead(self, *, name: str) -> bool:
        # `#{pane_dead}` prints "1" (dead) or "0" (alive). Empty output means tmux
        # couldn't find the session — treat "gone" as dead so the reaper reclaims
        # it. A transient tmux error also reads as gone; acceptable, since reaping
        # a momentarily-unqueryable session is self-healing (relaunched next run).
        out = self.tmux.run(["list-panes", "-t", name, "-F", "#{pane_dead}"])
        return out.strip().startswith("1") if out.strip() else True

    @staticmethod
    def turn_allowed(turns_used: int, *, max_turns: Optional[int]) -> bool:
        return max_turns is None or turns_used < max_turns
