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


def test_codex_proxy_registered_as_api_key_provider():
    from hermes_cli.auth import PROVIDER_REGISTRY
    pc = PROVIDER_REGISTRY.get("codex-proxy")
    assert pc is not None, "codex-proxy must be in auth PROVIDER_REGISTRY"
    assert pc.auth_type == "api_key"
    assert pc.inference_base_url == "http://127.0.0.1:8645/v1"


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
    assert c["base_url"] == "http://127.0.0.1:8645/v1"
