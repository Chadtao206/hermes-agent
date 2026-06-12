"""Tests for ClaudeSessionClient integration."""

import json
import pytest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from agent.claude_session_client import ClaudeSessionClient


def _can_import_claude_session() -> bool:
    """Check if claude_session module is importable."""
    try:
        from tools.claude_session.cli import build_parser
        return True
    except ImportError:
        return False


class TestClaudeSessionClient:
    """Unit tests for ClaudeSessionClient."""

    def test_init_defaults(self):
        """Client initializes with sensible defaults."""
        client = ClaudeSessionClient()
        assert client.api_key == "claude-session"
        assert client.base_url == "claude-session://local"
        assert client.is_closed is False
        assert hasattr(client, "chat")
        assert hasattr(client.chat, "completions")

    def test_init_with_options(self):
        """Client accepts configuration options."""
        client = ClaudeSessionClient(
            cwd="/tmp/project",
            max_turns=10,
            max_budget_usd=1.0,
            model="opus",
        )
        # macOS resolves /tmp -> /private/tmp
        assert client._cwd.endswith("/tmp/project")
        assert client._max_turns == 10
        assert client._max_budget_usd == 1.0
        assert client._model == "opus"

    def test_extract_task_from_string_content(self):
        """Extracts task from simple string message."""
        client = ClaudeSessionClient()
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Add tests for auth.py"},
        ]
        task = client._extract_task(messages)
        assert task == "Add tests for auth.py"

    def test_extract_task_from_list_content(self):
        """Extracts task from multipart message content."""
        client = ClaudeSessionClient()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please fix this bug"},
                    {"type": "image", "image_url": "..."},
                ],
            },
        ]
        task = client._extract_task(messages)
        assert task == "Please fix this bug"

    def test_extract_task_uses_last_user_message(self):
        """Uses the last user message, not the first."""
        client = ClaudeSessionClient()
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up question"},
        ]
        task = client._extract_task(messages)
        assert task == "Follow-up question"

    def test_extract_task_empty_messages(self):
        """Returns empty string for no messages."""
        client = ClaudeSessionClient()
        assert client._extract_task([]) == ""

    def test_resolve_timeout_none(self):
        """Default timeout when none provided."""
        client = ClaudeSessionClient()
        assert client._resolve_timeout(None) == 600.0

    def test_resolve_timeout_numeric(self):
        """Accepts numeric timeout."""
        client = ClaudeSessionClient()
        assert client._resolve_timeout(300) == 300.0
        assert client._resolve_timeout(300.5) == 300.5

    def test_resolve_timeout_httpx_object(self):
        """Extracts timeout from httpx.Timeout-like object."""
        client = ClaudeSessionClient()
        mock_timeout = SimpleNamespace(read=120, write=30, connect=10)
        assert client._resolve_timeout(mock_timeout) == 120.0

    def test_to_openai_response_success(self):
        """Converts successful result to OpenAI format."""
        client = ClaudeSessionClient()
        result = {
            "type": "result",
            "subtype": "success",
            "result": "Task completed successfully",
            "session_id": "abc123",
            "num_turns": 5,
            "total_cost_usd": 0.15,
            "usage": {"input_tokens": 100, "output_tokens": 200},
        }
        response = client._to_openai_response(result, "opus")

        assert response.model == "opus"
        assert len(response.choices) == 1
        assert response.choices[0].message.content == "Task completed successfully"
        assert response.choices[0].finish_reason == "stop"
        assert response.usage.prompt_tokens == 100
        assert response.usage.completion_tokens == 200
        assert response._claude_session_id == "abc123"

    def test_to_openai_response_max_turns(self):
        """Maps max_turns error to length finish_reason."""
        client = ClaudeSessionClient()
        result = {
            "type": "result",
            "subtype": "error_max_turns",
            "result": "",
            "usage": {},
        }
        response = client._to_openai_response(result, None)
        assert response.choices[0].finish_reason == "length"

    def test_error_response_format(self):
        """Error response has correct format."""
        client = ClaudeSessionClient()
        response = client._error_response("Something went wrong")

        assert response.model == "claude-session"
        assert "Error: Something went wrong" in response.choices[0].message.content
        assert response._claude_subtype == "error"

    def test_close_sets_flag(self):
        """Close sets is_closed flag."""
        client = ClaudeSessionClient()
        assert client.is_closed is False
        client.close()
        assert client.is_closed is True


class TestClaudeSessionClientIntegration:
    """Integration tests (require claude_session module)."""

    @pytest.mark.skipif(
        not _can_import_claude_session(),
        reason="claude_session module not available"
    )
    def test_run_task_import_available(self):
        """Verifies claude_session can be imported."""
        client = ClaudeSessionClient(cwd="/tmp")
        # Just verify the import path works
        from tools.claude_session.cli import build_parser
        assert build_parser is not None

    @pytest.mark.integration
    @pytest.mark.timeout(0)
    def test_run_task_live(self):
        """Live test against real Claude Code (gated)."""
        client = ClaudeSessionClient(
            cwd="/tmp",
            max_turns=1,
            oneshot=True,
        )
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": "echo 'hello world'"}],
            timeout=60,
        )
        # Just verify we get a response object back
        assert hasattr(response, "choices")
        assert len(response.choices) > 0
