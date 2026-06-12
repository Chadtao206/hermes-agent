# Design: Wire claude_session into Programmatic Delegation

## Problem

The `tools/claude_session/` tmux helper was built and merged, but it's only accessible when an agent manually follows the `claude-code` skill via terminal commands. Programmatic delegation (`delegate_tool(acp_command="claude")`) still uses `CopilotACPClient`, which spawns Claude Code as a subprocess with stdin/stdout JSON-RPC — missing all the tmux benefits:

- No watch/steer capabilities
- No warm multi-turn sessions  
- No hooks-based completion detection
- No session pool management

## Goal

Enable programmatic delegation to Claude Code via the tmux helper, while preserving the ability to fall back to subprocess mode when tmux is unavailable.

## Design

### Approach: New `ClaudeSessionClient` class

Create a new client class that:
1. Implements the same interface as `CopilotACPClient` (`chat.completions.create()`)
2. Uses `tools.claude_session.dispatch` under the hood
3. Manages warm sessions for multi-turn conversations
4. Falls back to `-p` mode automatically when tmux is unavailable

### Architecture

```
delegate_tool / run_agent
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│              create_openai_client()                          │
│                                                              │
│  provider == "claude-session"  ──▶  ClaudeSessionClient     │
│  provider == "copilot-acp"     ──▶  CopilotACPClient        │
│  else                          ──▶  OpenAI()                 │
└─────────────────────────────────────────────────────────────┘
        │
        ▼ (ClaudeSessionClient)
┌─────────────────────────────────────────────────────────────┐
│  1. Check if tmux available + claude version OK             │
│  2. If yes: use tools.claude_session.dispatch               │
│  3. If no:  fall back to claude -p subprocess               │
│  4. Convert response to OpenAI-compatible format            │
└─────────────────────────────────────────────────────────────┘
```

### New Files

#### 1. `agent/claude_session_client.py`

```python
"""OpenAI-compatible client that delegates to Claude Code via tmux.

Uses tools.claude_session for warm multi-turn sessions with watch/steer
capabilities. Falls back to claude -p when tmux is unavailable.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tools.claude_session.router import Decision, claude_version_ok, decide_path, tmux_available
from tools.claude_session.dispatch import run as dispatch_run
from tools.claude_session.cli import build_parser

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
    """OpenAI-client-compatible facade for Claude Code via tmux."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        cwd: str | None = None,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
        model: str | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "claude-session"
        self.base_url = base_url or CLAUDE_SESSION_MARKER_BASE_URL
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._model = model
        self.chat = _ClaudeSessionChatNamespace(self)
        self.is_closed = False
        self._session_name: str | None = None  # for warm sessions
        self._lock = threading.Lock()

    def close(self) -> None:
        self.is_closed = True
        if self._session_name:
            # Stop the warm session
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
        # Extract the task from the last user message
        task = self._extract_task(messages or [])
        if not task:
            return self._error_response("No user message found")

        effective_timeout = self._resolve_timeout(timeout)
        
        # Run via claude_session
        result = self._run_task(task, timeout=effective_timeout)
        
        # Convert to OpenAI-compatible response
        return self._to_openai_response(result, model)

    def _extract_task(self, messages: list[dict[str, Any]]) -> str:
        """Extract task from the last user message."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    texts = [p.get("text", "") for p in content 
                             if isinstance(p, dict) and p.get("type") == "text"]
                    return "\n".join(texts).strip()
        return ""

    def _resolve_timeout(self, timeout: Any) -> float:
        if timeout is None:
            return _DEFAULT_TIMEOUT_SECONDS
        if isinstance(timeout, (int, float)):
            return float(timeout)
        # httpx.Timeout object
        candidates = [getattr(timeout, a, None) 
                      for a in ("read", "write", "connect", "pool", "timeout")]
        numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
        return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS

    def _run_task(self, task: str, timeout: float) -> dict:
        """Run task via claude_session dispatch."""
        # Build args for the 'run' verb
        parser = build_parser()
        args = parser.parse_args([
            "run",
            "--task", task,
            "--workdir", self._cwd,
            "--timeout", str(int(timeout)),
        ])
        if self._max_turns:
            args.max_turns = self._max_turns
        if self._max_budget_usd:
            args.max_budget_usd = self._max_budget_usd
        if self._model:
            args.model = self._model

        # Capture JSON output
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            dispatch_run(args)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        # Parse the JSON result
        for line in output.strip().split("\n"):
            if line.strip():
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        return {"type": "result", "subtype": "error", "result": ""}

    def _run_verb(self, verb: str, **kwargs) -> dict:
        """Run a claude_session verb."""
        parser = build_parser()
        args_list = [verb]
        for k, v in kwargs.items():
            if v is not None:
                args_list.extend([f"--{k.replace('_', '-')}", str(v)])
        args = parser.parse_args(args_list)
        
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            dispatch_run(args)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        
        for line in output.strip().split("\n"):
            if line.strip():
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        return {}

    def _to_openai_response(self, result: dict, model: str | None) -> Any:
        """Convert claude_session result to OpenAI-compatible response."""
        text = result.get("result", "")
        usage = result.get("usage", {})
        
        assistant_message = SimpleNamespace(
            content=text,
            tool_calls=[],
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        
        usage_obj = SimpleNamespace(
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        
        choice = SimpleNamespace(
            message=assistant_message,
            finish_reason="stop",
        )
        
        return SimpleNamespace(
            choices=[choice],
            usage=usage_obj,
            model=model or "claude-session",
        )

    def _error_response(self, error: str) -> Any:
        assistant_message = SimpleNamespace(
            content=f"Error: {error}",
            tool_calls=[],
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        choice = SimpleNamespace(message=assistant_message, finish_reason="stop")
        usage = SimpleNamespace(
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(choices=[choice], usage=usage, model="claude-session")
```

### Integration Points

#### 2. Modify `agent/agent_runtime_helpers.py`

Add claude-session provider detection:

```python
def create_openai_client(agent, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
    # ... existing validation ...
    
    # NEW: Claude Session client (tmux-based)
    if agent.provider == "claude-session" or str(client_kwargs.get("base_url", "")).startswith("claude-session://"):
        from agent.claude_session_client import ClaudeSessionClient
        client = ClaudeSessionClient(**client_kwargs)
        _ra().logger.info(
            "Claude Session client created (%s, shared=%s) %s",
            reason, shared, agent._client_log_context(),
        )
        return client
    
    # Existing copilot-acp handling
    if agent.provider == "copilot-acp" or str(client_kwargs.get("base_url", "")).startswith("acp://copilot"):
        from agent.copilot_acp_client import CopilotACPClient
        # ... existing code ...
```

#### 3. Modify `hermes_cli/runtime_provider.py`

Add claude-session provider resolution:

```python
def resolve_runtime_provider(...) -> Dict[str, Any]:
    # ... existing code ...
    
    # NEW: Claude Session provider
    if provider == "claude-session":
        return {
            "provider": "claude-session",
            "api_mode": "chat_completions",
            "base_url": "claude-session://local",
            "api_key": "claude-session",
            "source": "claude-session",
            "requested_provider": requested_provider,
        }
    
    # ... rest of existing providers ...
```

#### 4. Modify `hermes_cli/auth.py`

Add to `PROVIDER_REGISTRY`:

```python
ProviderConfig(
    id="claude-session",
    display_name="Claude Code (tmux)",
    auth_type="none",  # No auth needed - claude handles its own
    inference_base_url="claude-session://local",
    api_key_env_vars=[],
),
```

### Usage

After integration, users can delegate to Claude Code via tmux:

```python
# In delegate_tool or config
delegate_task(
    goal="Add error handling to API calls",
    provider="claude-session",
    # OR
    acp_command="claude-session",  # if we want to keep acp_command pattern
)
```

Or in config.yaml:
```yaml
model:
  provider: claude-session
  default: claude-opus-4
```

### Fallback Behavior

The `ClaudeSessionClient` automatically falls back to `claude -p` when:
1. tmux is not available
2. Claude CLI version doesn't support hooks
3. tmux handshake times out

This matches the existing behavior in `tools/claude_session/dispatch.py`.

### Benefits

1. **Warm sessions**: Multi-turn conversations reuse the same Claude session
2. **Watch/steer**: Can monitor and redirect Claude mid-task
3. **Hooks-based completion**: Reliable completion detection via SessionStop hook
4. **Pool management**: Session limits prevent runaway resource usage
5. **Automatic fallback**: Degrades gracefully to `-p` mode

### Migration Path

1. **Phase 1**: Add `ClaudeSessionClient` and provider (backward compatible)
2. **Phase 2**: Update documentation and examples
3. **Phase 3**: Make `claude-session` the default for Claude Code delegation
4. **Phase 4**: Deprecate direct `claude -p` subprocess path

### Open Questions

1. Should warm sessions persist across delegate_task calls, or start fresh each time?
2. How to handle tool calls? Claude Code's tool results come via its own tooling, not through the ACP response format.
3. Should we support the full multi-turn warm session API, or just one-shot `run`?

## Alternatives Considered

### A. Modify CopilotACPClient directly
- **Rejected**: Would conflate GitHub Copilot and Claude Code paths
- The two have different protocols and capabilities

### B. Skill-based auto-delegation  
- **Rejected**: Requires agent to "decide" to use Claude Code
- Doesn't work for programmatic delegation where we want explicit control

### C. Replace delegate_tool entirely
- **Rejected**: Too invasive, breaks existing workflows
- New client class is additive and backward compatible
