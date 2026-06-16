from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from .hooks import done_channel, ready_channel


class HandshakeTimeout(RuntimeError):
    """A tmux wait-for blocked past its timeout (ready/done never signalled)."""


class StartupError(RuntimeError):
    """Claude Code parked on a fatal startup banner (e.g. a rejected --model).

    The REPL printed the error but never ran a turn, so the Stop hook never
    fires and cs-done never arrives. Raised so the caller can fail fast / fall
    back instead of blocking for the entire done_timeout.
    """


# Banners Claude Code prints for a fatal startup condition that leaves the REPL
# idle without running a turn. Matched case-insensitively against the captured
# pane, and only during the startup grace window after a prompt is sent, so
# normal turn output that happens to quote one of these never trips the guard.
_STARTUP_ERROR_PATTERNS: "tuple[str, ...]" = (
    "issue with the selected model",   # invalid / inaccessible --model
)
# How long after sending a prompt to watch for a startup-error banner, and how
# long each poll slice waits for cs-done before re-checking the pane.
_STARTUP_GRACE_SECONDS = 15.0
_STARTUP_POLL_SECONDS = 3.0

# First-prompt submission can race claude's startup: the cs-ready SessionStart
# hook fires before the TUI input handler is mounted, so the prompt (or its
# Enter) can be dropped → no turn ever runs → cs-done never fires → 30-min
# deadline → task auto-blocks. The robust fix: wait for an input-ready indicator
# in the pane ("bypass permissions on" / "for shortcuts"), send the full prompt,
# then check the transcript for a user record and re-send the FULL prompt if not
# present (gated on "no turn started" → can never double-submit).
_SUBMIT_RETRIES = 2
_SUBMIT_RETRY_DELAY = 1.2
# Worker sessions launch with MCP DISABLED: they use built-in tools + gh, not
# claude's MCP servers, and the (remote) MCP startup init is what widens the
# submit race above. --strict-mcp-config + an empty config file = zero servers.
_EMPTY_MCP_CONFIG = Path.home() / ".hermes" / "state" / "claude_session" / "empty_mcp.json"
_INPUT_READY_TIMEOUT = 12.0   # max wait for the TUI input box after cs-ready
_INPUT_READY_POLL = 0.5


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
        self._ensure_empty_mcp_config()
        claude_cmd = (
            f"cd {shlex.quote(workdir)} && claude --session-id {uuid} "
            f"--permission-mode bypassPermissions --model {shlex.quote(model)} "
            f"--strict-mcp-config --mcp-config {shlex.quote(str(_EMPTY_MCP_CONFIG))} "
            f"--settings {shlex.quote(settings_json)}"
        )
        self.tmux.run(["send-keys", "-t", name, claude_cmd, "Enter"])
        # Block on the SessionStart hook signal (plain wait-for; latched).
        self.tmux.run(["wait-for", ready_channel(uuid)], timeout=ready_timeout)

    def send(self, *, name: str, uuid: str, prompt: str, done_timeout: float,
             startup_grace: float = _STARTUP_GRACE_SECONDS,
             workdir=None,
             submit_retries: int = _SUBMIT_RETRIES,
             submit_retry_delay: float = _SUBMIT_RETRY_DELAY) -> str:
        if workdir is not None:
            # first prompt: races startup → input-ready wait + verify/resend
            self._submit_first_prompt(name=name, uuid=uuid, prompt=prompt, workdir=workdir,
                                      retries=submit_retries, delay=submit_retry_delay)
        else:
            self.send_text(name=name, text=prompt)   # warm re-send: TUI already ready
        return self._wait_for_done(
            name=name, uuid=uuid, done_timeout=done_timeout,
            startup_grace=startup_grace,
        )

    def _wait_for_done(self, *, name: str, uuid: str, done_timeout: float,
                       startup_grace: float) -> str:
        # During the startup window, wait for cs-done in short slices and scan
        # the pane between slices. A fatal startup error (e.g. a rejected
        # --model) parks the REPL so cs-done would otherwise never arrive and we
        # would block for the full done_timeout. Past the window, a single
        # blocking wait covers the remaining budget — the cheap path that keeps
        # healthy long-running turns unaffected. startup_grace=0 disables the
        # guard (warm re-sends, where startup already succeeded).
        chan = done_channel(uuid)
        waited = 0.0
        grace = min(startup_grace, done_timeout)
        while waited < grace:
            slice_ = min(_STARTUP_POLL_SECONDS, grace - waited)
            try:
                self.tmux.run(["wait-for", chan], timeout=slice_)
                return chan
            except HandshakeTimeout:
                waited += slice_
                banner = self._startup_error(name=name)
                if banner:
                    raise StartupError(f"{name}: {banner}")
        remaining = done_timeout - waited
        if remaining > 0:
            self.tmux.run(["wait-for", chan], timeout=remaining)
        return chan

    def _submit_first_prompt(self, *, name, uuid, prompt, workdir, retries, delay):
        # SessionStart fires before the input handler is mounted, so input sent
        # immediately can be dropped entirely. Wait for input-ready, send, then
        # verify a turn started and re-send the FULL prompt if not. The re-send is
        # GATED on "no turn started yet" → it can never double-submit.
        self._await_input_ready(name=name)
        self.send_text(name=name, text=prompt)
        for _ in range(max(0, retries)):
            if delay > 0:
                time.sleep(delay)
            if self._turn_started(uuid=uuid, workdir=workdir):
                return
            self.send_text(name=name, text=prompt)

    def _await_input_ready(self, *, name, timeout=_INPUT_READY_TIMEOUT, poll=_INPUT_READY_POLL):
        waited = 0.0
        while waited < timeout:
            try:
                pane = self.capture(name=name, lines=40)
            except Exception:
                pane = ""
            s = " ".join(pane.split()).lower()
            if "bypass permissions on" in s or "for shortcuts" in s:
                return
            time.sleep(poll)
            waited += poll
        # proceed best-effort even if not detected

    def _turn_started(self, *, uuid, workdir) -> bool:
        # A submitted prompt writes a 'user' record to the session transcript
        # (projects_dir/<encoded-workdir>/<uuid>.jsonl). Reliable, no double-submit.
        try:
            proj = re.sub(r"[/.]", "-", str(workdir))
            path = self.projects_dir / proj / f"{uuid}.jsonl"
            if not path.exists():
                return False
            for line in path.read_text(errors="replace").splitlines():
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if (o.get("type") or o.get("role")) == "user":
                    return True
        except Exception:
            pass
        return False

    def _startup_error(self, *, name: str) -> Optional[str]:
        """Return the matched fatal-startup banner in the pane, or None."""
        try:
            pane = self.capture(name=name)
        except Exception:
            return None
        text = " ".join(pane.split()).lower()
        return next((p for p in _STARTUP_ERROR_PATTERNS if p in text), None)

    def send_text(self, *, name: str, text: str) -> None:
        # -l literal, -- end-of-opts; SEPARATE Enter to submit.
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
    def _ensure_empty_mcp_config() -> None:
        # Idempotently materialize the empty MCP config the launch command points
        # at, so --mcp-config never references a missing file. Best-effort: if the
        # write fails, claude surfaces its own config error rather than us hanging.
        try:
            if not _EMPTY_MCP_CONFIG.exists():
                _EMPTY_MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                _EMPTY_MCP_CONFIG.write_text('{"mcpServers": {}}')
        except OSError:
            pass

    @staticmethod
    def turn_allowed(turns_used: int, *, max_turns: Optional[int]) -> bool:
        return max_turns is None or turns_used < max_turns
