"""Tests for the codex-proxy provider overlay and Codex-backend detection."""
from __future__ import annotations

from hermes_constants import DEFAULT_CODEX_PROXY_BASE_URL


def test_default_codex_proxy_base_url_constant():
    """Sanity-check the shared constant so all three use-sites stay in sync."""
    assert DEFAULT_CODEX_PROXY_BASE_URL == "http://127.0.0.1:8645/v1"


def test_codex_proxy_overlay_registered():
    from hermes_cli.providers import HERMES_OVERLAYS
    ov = HERMES_OVERLAYS["codex-proxy"]
    assert ov.transport == "codex_responses"
    assert ov.auth_type == "api_key"
    assert ov.base_url_override == DEFAULT_CODEX_PROXY_BASE_URL


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


def test_codex_proxy_registered_as_api_key_provider():
    from hermes_cli.auth import PROVIDER_REGISTRY
    pc = PROVIDER_REGISTRY.get("codex-proxy")
    assert pc is not None, "codex-proxy must be in auth PROVIDER_REGISTRY"
    assert pc.auth_type == "api_key"
    assert pc.inference_base_url == DEFAULT_CODEX_PROXY_BASE_URL


def test_codex_proxy_api_key_resolves_from_pool(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # store a codex-proxy api key in the credential pool
    # PooledCredential stores the secret in `access_token` (see credential_pool.py:136)
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {},
        "credential_pool": {"codex-proxy": [{
            "id": "cp1", "label": "proxy-token", "auth_type": "api_key",
            "priority": 0, "source": "manual", "access_token": "tok-abc123",
        }]},
    }))
    from hermes_cli.auth import resolve_api_key_provider_credentials
    c = resolve_api_key_provider_credentials("codex-proxy")
    assert c["api_key"] == "tok-abc123"
    assert c["base_url"] == DEFAULT_CODEX_PROXY_BASE_URL


def test_codex_proxy_fallback_api_mode_is_codex_responses():
    """codex-proxy as a fallback provider must use codex_responses, not chat_completions."""
    import agent.chat_completion_helpers as h
    import inspect
    src = inspect.getsource(h)
    # Verify the elif branch for codex-proxy sets codex_responses
    assert 'fb_provider == "codex-proxy"' in src
    assert "codex_responses" in src


def test_codex_proxy_base_url_consistent_across_modules():
    """All three use-sites of the proxy base URL must resolve to the same value."""
    from hermes_cli.providers import HERMES_OVERLAYS
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_constants import DEFAULT_CODEX_PROXY_BASE_URL

    overlay_url = HERMES_OVERLAYS["codex-proxy"].base_url_override
    auth_url = PROVIDER_REGISTRY["codex-proxy"].inference_base_url

    assert overlay_url == DEFAULT_CODEX_PROXY_BASE_URL, (
        f"providers.py overlay URL {overlay_url!r} != constant {DEFAULT_CODEX_PROXY_BASE_URL!r}"
    )
    assert auth_url == DEFAULT_CODEX_PROXY_BASE_URL, (
        f"auth.py registry URL {auth_url!r} != constant {DEFAULT_CODEX_PROXY_BASE_URL!r}"
    )


def test_codex_proxy_vision_backend_in_auxiliary_client():
    """_resolve_strict_vision_backend must handle codex-proxy (mirrors openai-codex)."""
    import agent.auxiliary_client as ac
    import inspect
    src = inspect.getsource(ac._resolve_strict_vision_backend)
    assert "codex-proxy" in src, (
        "_resolve_strict_vision_backend must have a codex-proxy branch"
    )


def test_codex_proxy_context_window_branch_in_model_metadata():
    """model_metadata must have a codex-proxy branch that resolves via the real token."""
    import agent.model_metadata as mm
    import inspect
    src = inspect.getsource(mm.get_model_context_length)
    assert "codex-proxy" in src, (
        "get_model_context_length must contain a codex-proxy branch"
    )


def test_codex_proxy_runtime_provider_uses_responses_api(tmp_path, monkeypatch):
    """Primary codex-proxy resolution must not fall back to chat_completions."""
    import json

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "model:\n"
        "  default: gpt-5.5\n"
        "  provider: codex-proxy\n"
        f"  base_url: {DEFAULT_CODEX_PROXY_BASE_URL}\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {"codex-proxy": [{
            "id": "cp1",
            "label": "proxy-token",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "tok-abc123",
        }]},
    }), encoding="utf-8")

    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(
        requested="codex-proxy",
        explicit_base_url=DEFAULT_CODEX_PROXY_BASE_URL,
    )

    assert runtime["provider"] == "codex-proxy"
    assert runtime["base_url"] == DEFAULT_CODEX_PROXY_BASE_URL
    assert runtime["api_mode"] == "codex_responses"
