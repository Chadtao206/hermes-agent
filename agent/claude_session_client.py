"""OpenAI-compatible client that delegates to Claude Code via tmux.

Uses tools.claude_session for warm multi-turn sessions with watch/steer
capabilities. Falls back to claude -p when tmux is unavailable.

This client implements the same interface as CopilotACPClient, allowing
drop-in replacement for programmatic Claude Code delegation.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CLAUDE_SESSION_MARKER_BASE_URL = "claude-session://local"
_DEFAULT_TIMEOUT_SECONDS = 600.0


class _ClaudeSessionChatCompletions:
    def __init__(self, client: "ClaudeSessionClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ClaudeSessionChatNamespace:
    def __init__(self, client: "ClaudeSessionClient"):
        self.completions = _ClaudeSessionChatCompletions(client)


class ClaudeSessionClient:
    """OpenAI-client-compatible facade for Claude Code via tmux.

    This client wraps tools.claude_session to provide Claude Code delegation
    with tmux-based session management. It exposes the same interface as
    CopilotACPClient (and the OpenAI SDK), enabling seamless integration
    with Hermes' existing delegation machinery.

    Features:
    - Automatic tmux/fallback routing via claude_session.router
    - Warm session support for multi-turn conversations
    - Watch/steer capabilities when using tmux mode
    - Hooks-based completion detection
    - Session pool management with configurable caps

    Usage:
        client = ClaudeSessionClient(cwd="/path/to/project")
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": "Add tests for auth.py"}],
            timeout=300,
        )
        print(response.choices[0].message.content)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        cwd: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        model: str | None = None,
        oneshot: bool = True,
        **_: Any,
    ):
        """Initialize the Claude Session client.

        Args:
            api_key: Ignored (Claude handles its own auth)
            base_url: Ignored (uses local tmux)
            cwd: Working directory for Claude sessions
            max_turns: Maximum turns before stopping
            max_budget_usd: Maximum cost before stopping
            model: Model override (e.g., "opus", "sonnet")
            oneshot: If True, tear down session after each task (default)
        """
        self.api_key = api_key or "claude-session"
        self.base_url = base_url or CLAUDE_SESSION_MARKER_BASE_URL
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._model = model
        self._oneshot = oneshot
        self.chat = _ClaudeSessionChatNamespace(self)
        self.is_closed = False
        self._session_name: str | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        """Close the client and any active session."""
        self.is_closed = True
        if self._session_name:
            self._run_verb("stop", name=self._session_name)
            self._session_name = None

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> Any:
        """Create a chat completion via Claude Code.

        Extracts the task from the last user message and runs it through
        claude_session. Returns an OpenAI-compatible response object.
        """
        task = self._extract_task(messages or [])
        if not task:
            return self._error_response("No user message found in messages")

        effective_timeout = self._resolve_timeout(timeout)
        result = self._run_task(task, timeout=effective_timeout, model=model)
        return self._to_openai_response(result, model)

    def _extract_task(self, messages: list[dict[str, Any]]) -> str:
        """Extract task text from the last user message."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    texts = [
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    return "\n".join(texts).strip()
        return ""

    def _resolve_timeout(self, timeout: Any) -> float:
        """Resolve timeout from various input formats."""
        if timeout is None:
            return _DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout, (int, float)):
            return float(timeout)
        # Handle httpx.Timeout objects
        candidates = [
            getattr(timeout, attr, None)
            for attr in ("read", "write", "connect", "pool", "timeout")
        ]
        numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
        return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS

    @staticmethod
    def _to_cli_model(model: str | None) -> str | None:
        """Translate a configured model id into the form ``claude --model`` accepts.

        Claude Code takes Anthropic's native dash-form ids (``claude-opus-4-8``)
        or aliases (``opus``), not the dot form (``claude-opus-4.8``). The CLI
        normalizes this for interactive/chat-driven agents, but callers that
        bypass that path (e.g. cron-dispatched agents) would otherwise pass the
        dot form straight through, parking the REPL on a "model may not exist"
        error. Normalizing here covers every caller that reaches dispatch.
        """
        if not model:
            return model
        try:
            from hermes_cli.model_normalize import normalize_model_for_provider
            return normalize_model_for_provider(model, "claude-session")
        except Exception:
            return model

    def _run_task(self, task: str, timeout: float, model: str | None = None) -> dict:
        """Run a task via claude_session dispatch.

        Uses the 'run' verb for one-shot tasks. The dispatch module handles
        tmux vs fallback routing automatically.
        """
        try:
            from tools.claude_session.cli import build_parser
            from tools.claude_session.dispatch import run as dispatch_run
        except ImportError as e:
            return {
                "type": "result",
                "subtype": "error",
                "result": f"claude_session not available: {e}",
                "session_id": "",
                "num_turns": 0,
                "total_cost_usd": 0.0,
                "usage": {},
            }

        parser = build_parser()
        args_list = [
            "run",
            "--task", task,
            "--workdir", self._cwd,
            "--timeout", str(int(timeout)),
        ]
        if self._oneshot:
            args_list.append("--oneshot")
        if self._max_turns:
            args_list.extend(["--max-turns", str(self._max_turns)])
        if self._max_budget_usd:
            args_list.extend(["--max-budget-usd", str(self._max_budget_usd)])
        effective_model = self._to_cli_model(model or self._model)
        if effective_model:
            args_list.extend(["--model", effective_model])

        args = parser.parse_args(args_list)

        # Capture JSON output from dispatch
        old_stdout = sys.stdout
        captured = io.StringIO()
        sys.stdout = captured
        try:
            dispatch_run(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        # Parse the JSON result (dispatch emits one JSON object per line)
        for line in output.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass

        return {
            "type": "result",
            "subtype": "no_output",
            "result": "",
            "session_id": "",
            "num_turns": 0,
            "total_cost_usd": 0.0,
            "usage": {},
        }

    def _run_verb(self, verb: str, **kwargs: Any) -> dict:
        """Run an arbitrary claude_session verb."""
        try:
            from tools.claude_session.cli import build_parser
            from tools.claude_session.dispatch import run as dispatch_run
        except ImportError:
            return {}

        parser = build_parser()
        args_list = [verb]
        for key, value in kwargs.items():
            if value is not None:
                flag = f"--{key.replace('_', '-')}"
                args_list.extend([flag, str(value)])

        args = parser.parse_args(args_list)

        old_stdout = sys.stdout
        captured = io.StringIO()
        sys.stdout = captured
        try:
            dispatch_run(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        for line in output.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        return {}

    def _to_openai_response(self, result: dict, model: str | None) -> Any:
        """Convert claude_session result to OpenAI-compatible response."""
        text = result.get("result", "")
        usage_data = result.get("usage", {})
        subtype = result.get("subtype", "")

        # Handle error subtypes
        if subtype in ("error", "pool_full", "unknown_session", "no_output"):
            text = text or f"Claude session error: {subtype}"

        assistant_message = SimpleNamespace(
            content=text,
            tool_calls=[],
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )

        input_tokens = usage_data.get("input_tokens", 0)
        output_tokens = usage_data.get("output_tokens", 0)
        usage_obj = SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )

        # Map subtype to finish_reason
        if subtype == "error_max_turns":
            finish_reason = "length"
        elif subtype == "error_budget":
            finish_reason = "length"
        elif subtype in ("error", "pool_full"):
            finish_reason = "stop"
        else:
            finish_reason = "stop"

        choice = SimpleNamespace(
            message=assistant_message,
            finish_reason=finish_reason,
        )

        return SimpleNamespace(
            choices=[choice],
            usage=usage_obj,
            model=model or "claude-session",
            # Include extra metadata for debugging
            _claude_session_id=result.get("session_id", ""),
            _claude_session_name=result.get("session_name"),
            _claude_num_turns=result.get("num_turns", 0),
            _claude_total_cost_usd=result.get("total_cost_usd", 0.0),
            _claude_subtype=subtype,
        )

    def _error_response(self, error: str) -> Any:
        """Generate an error response in OpenAI format."""
        assistant_message = SimpleNamespace(
            content=f"Error: {error}",
            tool_calls=[],
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        choice = SimpleNamespace(message=assistant_message, finish_reason="stop")
        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model="claude-session",
            _claude_session_id="",
            _claude_subtype="error",
        )
