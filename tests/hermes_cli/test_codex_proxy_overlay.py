"""Tests for the codex-proxy provider overlay and Codex-backend detection."""
from __future__ import annotations


def test_codex_proxy_overlay_registered():
    from hermes_cli.providers import HERMES_OVERLAYS
    ov = HERMES_OVERLAYS["codex-proxy"]
    assert ov.transport == "codex_responses"
    assert ov.auth_type == "api_key"
    assert ov.base_url_override == "http://127.0.0.1:8645/v1"


def test_codex_proxy_resolves_codex_responses_api_mode():
    from hermes_cli.providers import get_provider, TRANSPORT_TO_API_MODE
    pdef = get_provider("codex-proxy")
    assert pdef is not None
    assert TRANSPORT_TO_API_MODE[pdef.transport] == "codex_responses"


def test_codex_proxy_counts_as_codex_backend():
    import agent.chat_completion_helpers as h
    import inspect
    src = inspect.getsource(h)
    assert '"codex-proxy"' in src or "'codex-proxy'" in src
