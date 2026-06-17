# Codex Proxy Upstream — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `openai-codex` upstream to `hermes proxy` so one launchd-managed proxy holds and refreshes the single Codex OAuth token, and all hermes profiles + the standalone `codex` CLI ride it via a dummy bearer.

**Architecture:** A `CodexAdapter` (near-clone of `XAIGrokAdapter`) resolves the Codex credential from the existing `openai-codex` `CredentialPool` and supplies the Cloudflare headers the ChatGPT backend requires. A small generic `extra_headers` hook on `UpstreamCredential` lets the server inject those. Profiles reach the proxy through a new `codex-proxy` provider overlay; Codex-backend request shaping is enabled by adding the provider name to two existing disjunctions (mirroring how `xai` is handled). An opt-in shared-secret bearer closes the local-spend hole, and `hermes proxy install` runs it as a crash-safe launchd service.

**Tech Stack:** Python 3, aiohttp (proxy server), pytest (tests run via `~/.hermes/hermes-agent/venv/bin/python -m pytest`), launchd (macOS service).

**Spec:** `docs/designs/2026-06-16-codex-proxy-upstream.md` (v3, adversarial-reviewed; Gate 2 verified).

---

## File structure

| File | Responsibility | Action |
|------|---------------|--------|
| `hermes_cli/proxy/adapters/base.py` | `UpstreamCredential` gains `extra_headers` | Modify |
| `hermes_cli/proxy/server.py` | merge `extra_headers`; raise body cap; optional bearer auth | Modify |
| `hermes_cli/proxy/adapters/codex.py` | `CodexAdapter` | Create |
| `hermes_cli/proxy/adapters/__init__.py` | register `codex` | Modify |
| `hermes_cli/proxy/cli.py` | `cmd_proxy_install` / `cmd_proxy_uninstall` | Modify |
| `hermes_cli/main.py` | `--provider` help + `install`/`uninstall` subparsers | Modify |
| `hermes_cli/providers.py` | `codex-proxy` overlay | Modify |
| `agent/chat_completion_helpers.py` | `is_codex_backend` includes `codex-proxy` | Modify (1 line) |
| `agent/agent_init.py` | `api_mode` includes `codex-proxy` | Modify (1 line) |
| `tests/hermes_cli/test_proxy.py` | adapter + server + CLI tests | Modify |
| `tests/hermes_cli/test_codex_proxy_overlay.py` | overlay + backend-detection tests | Create |

Run all tests with: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py tests/hermes_cli/test_codex_proxy_overlay.py -q`

---

### Task 1: `extra_headers` hook on `UpstreamCredential` + server merge

**Files:**
- Modify: `hermes_cli/proxy/adapters/base.py`
- Modify: `hermes_cli/proxy/server.py` (after the `Authorization` swap)
- Test: `tests/hermes_cli/test_proxy.py`

- [ ] **Step 1: Write the failing test** — append to `tests/hermes_cli/test_proxy.py`. `FakeAdapter` doesn't set headers, so add a subclass inline in the test.

```python
def test_server_injects_adapter_extra_headers():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))

        class HeaderAdapter(FakeAdapter):
            def get_credential(self):
                return UpstreamCredential(
                    bearer="real", base_url=f"{upstream_base}/v1",
                    extra_headers={"originator": "codex_cli_rs", "ChatGPT-Account-ID": "acct-1"},
                )

        adapter = HeaderAdapter(f"{upstream_base}/v1")

        async def echo_headers(request):
            captured["requests"].append({"originator": request.headers.get("originator"),
                                         "acct": request.headers.get("ChatGPT-Account-ID"),
                                         "auth": request.headers.get("Authorization")})
            return web.json_response({"ok": True})
        # reuse the running upstream's /v1/chat/completions echo which records headers? It records auth/body only.
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{proxy_base}/v1/chat/completions", json={}) as resp:
                    assert resp.status == 200
            req = captured["requests"][0]
            assert req["auth"] == "Bearer real"
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()
    asyncio.run(run())
```

To assert the headers actually arrive, extend `_build_fake_upstream`'s `echo` to also capture `originator`/`ChatGPT-Account-ID`. Add these two lines inside `echo` in `_build_fake_upstream`:

```python
        "originator": request.headers.get("originator"),
        "acct": request.headers.get("ChatGPT-Account-ID"),
```

Then assert in the test: `assert req["originator"] == "codex_cli_rs"` and `assert req["acct"] == "acct-1"`.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py::test_server_injects_adapter_extra_headers -q`
Expected: FAIL — `UpstreamCredential.__init__() got an unexpected keyword argument 'extra_headers'`.

- [ ] **Step 3: Add the field** — `hermes_cli/proxy/adapters/base.py`. Add `Mapping` to the typing import and the field to the dataclass:

```python
from typing import FrozenSet, Mapping, Optional
```

```python
    extra_headers: Optional[Mapping[str, str]] = None
    """Adapter-supplied headers merged onto the forwarded request (after the
    Authorization swap). ``None`` = no extra headers. Used by providers whose
    upstream needs more than a bearer (e.g. Codex/Cloudflare originator)."""
```

- [ ] **Step 4: Merge in the server** — `hermes_cli/proxy/server.py`, immediately after the line `fwd_headers["Authorization"] = f"{active_cred.token_type} {active_cred.bearer}"`:

```python
            if active_cred.extra_headers:
                fwd_headers.update(active_cred.extra_headers)
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -q`
Expected: PASS (new test + all existing proxy tests).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/proxy/adapters/base.py hermes_cli/proxy/server.py tests/hermes_cli/test_proxy.py
git commit -m "feat(proxy): add extra_headers hook on UpstreamCredential"
```

---

### Task 2: Raise the proxy request-body cap (fixes review B1)

**Files:**
- Modify: `hermes_cli/proxy/server.py` (`create_app`, the `web.Application()` call)
- Test: `tests/hermes_cli/test_proxy.py`

- [ ] **Step 1: Write the failing test**

```python
def test_server_accepts_large_body():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        big = "x" * (3 * 1024 * 1024)  # 3 MB, over aiohttp's 1 MB default
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions", json={"blob": big},
                ) as resp:
                    assert resp.status == 200, f"large body rejected: {resp.status}"
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py::test_server_accepts_large_body -q`
Expected: FAIL — status `413` (Request Entity Too Large).

- [ ] **Step 3: Raise the cap** — `hermes_cli/proxy/server.py`, change `app = web.Application()` to:

```python
    # Codex Responses bodies (full conversation + encrypted-reasoning replay)
    # routinely exceed aiohttp's 1 MB default client_max_size.
    app = web.Application(client_max_size=64 * 1024 * 1024)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py::test_server_accepts_large_body -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/proxy/server.py tests/hermes_cli/test_proxy.py
git commit -m "fix(proxy): raise request-body cap to 64MB for large Codex turns"
```

---

### Task 3: `CodexAdapter`

**Files:**
- Create: `hermes_cli/proxy/adapters/codex.py`
- Modify: `hermes_cli/proxy/adapters/__init__.py`
- Test: `tests/hermes_cli/test_proxy.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/hermes_cli/test_proxy.py`. Helper writes an `openai-codex` pool entry with a JWT carrying `chatgpt_account_id`:

```python
import base64

def _make_codex_jwt(account_id: str = "acct-xyz") -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    b = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"hdr.{b}.sig"

def _write_codex_pool_entry(hermes_home: Path, *, access_token=None) -> Path:
    access_token = access_token or _make_codex_jwt()
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1, "providers": {},
        "credential_pool": {"openai-codex": [{
            "id": "cdx1", "label": "gateway", "auth_type": "oauth", "priority": 0,
            "source": "manual:device_code", "access_token": access_token,
            "refresh_token": "cdx-refresh", "base_url": "https://chatgpt.com/backend-api/codex",
        }]},
    }))
    return auth_path

def test_codex_adapter_metadata():
    from hermes_cli.proxy.adapters.codex import CodexAdapter
    a = CodexAdapter()
    assert a.name == "codex"
    assert a.display_name == "OpenAI Codex (ChatGPT)"
    assert "/responses" in a.allowed_paths

def test_codex_adapter_get_credential_injects_cloudflare_headers(tmp_path, monkeypatch):
    from hermes_cli.proxy.adapters.codex import CodexAdapter
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_codex_pool_entry(tmp_path, access_token=_make_codex_jwt("acct-xyz"))
    cred = CodexAdapter().get_credential()
    assert cred.base_url == "https://chatgpt.com/backend-api/codex"
    assert cred.extra_headers["originator"] == "codex_cli_rs"
    assert cred.extra_headers["User-Agent"].startswith("codex_cli_rs/")
    assert cred.extra_headers["ChatGPT-Account-ID"] == "acct-xyz"

def test_codex_adapter_not_authenticated_when_empty(tmp_path, monkeypatch):
    from hermes_cli.proxy.adapters.codex import CodexAdapter
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": {}}))
    assert not CodexAdapter().is_authenticated()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -k codex_adapter -q`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.proxy.adapters.codex`.

- [ ] **Step 3: Create the adapter** — `hermes_cli/proxy/adapters/codex.py`:

```python
"""OpenAI Codex (ChatGPT) OAuth upstream adapter.

A near-clone of XAIGrokAdapter: same CredentialPool machinery (which already
refreshes/rotates openai-codex entries), plus the Cloudflare headers the
ChatGPT Codex backend requires (it 403s requests lacking an allowed originator).
"""
from __future__ import annotations

import logging
import threading
from typing import FrozenSet, Optional

from agent.credential_pool import CredentialPool, PooledCredential, load_pool
from hermes_cli.auth import DEFAULT_CODEX_BASE_URL
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

_POOL_PROVIDER = "openai-codex"
_ALLOWED_PATHS: FrozenSet[str] = frozenset({"/responses", "/models"})


class CodexAdapter(UpstreamAdapter):
    """Proxy upstream for the ChatGPT Codex backend via Hermes-managed OAuth."""

    auth_hint = "hermes auth add openai-codex --type oauth"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: Optional[CredentialPool] = None

    @property
    def name(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex (ChatGPT)"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        pool = self._load_pool()
        return bool(pool and pool.has_available())

    def get_credential(self) -> UpstreamCredential:
        with self._lock:
            pool = self._load_pool()
            if pool is None or not pool.has_credentials():
                raise RuntimeError(
                    "No OpenAI Codex credentials found. Run "
                    "`hermes auth add openai-codex --type oauth` first."
                )
            entry = pool.select()
            if entry is None:
                raise RuntimeError(
                    "No available OpenAI Codex credentials. Run "
                    "`hermes auth reset openai-codex` or re-authenticate with "
                    "`hermes auth add openai-codex --type oauth`."
                )
            self._pool = pool
            return self._credential_from_entry(entry)

    def get_retry_credential(
        self, *, failed_credential: UpstreamCredential, status_code: int
    ) -> Optional[UpstreamCredential]:
        if status_code not in {401, 429}:
            return None
        with self._lock:
            pool = self._pool or self._load_pool()
            if pool is None:
                return None
            if status_code == 429:
                refreshed = pool.mark_exhausted_and_rotate(status_code=status_code)
            else:
                refreshed = pool.try_refresh_current()
                if refreshed is None:
                    refreshed = pool.mark_exhausted_and_rotate(status_code=status_code)
            if refreshed is None:
                return None
            retry_cred = self._credential_from_entry(refreshed)
            if retry_cred.bearer == failed_credential.bearer:
                return None
            logger.info(
                "proxy: Codex upstream returned %s; retrying with rotated pool credential",
                status_code,
            )
            return retry_cred

    def _load_pool(self) -> Optional[CredentialPool]:
        try:
            return load_pool(_POOL_PROVIDER)
        except Exception as exc:
            logger.warning("proxy: failed to load Codex OAuth credential pool: %s", exc)
            return None

    def _credential_from_entry(self, entry: PooledCredential) -> UpstreamCredential:
        bearer = (
            getattr(entry, "runtime_api_key", None)
            or getattr(entry, "access_token", "")
            or ""
        )
        bearer = str(bearer).strip()
        if not bearer:
            raise RuntimeError(
                "Codex OAuth pool entry had no access token. Re-authenticate "
                "with `hermes auth add openai-codex --type oauth`."
            )
        base_url = (
            getattr(entry, "runtime_base_url", None)
            or getattr(entry, "base_url", None)
            or DEFAULT_CODEX_BASE_URL
        )
        base_url = str(base_url or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")

        # Reuse the canonical header builder (User-Agent / originator /
        # ChatGPT-Account-ID extracted from the JWT). Imported lazily to avoid
        # a heavy import at module load.
        from agent.auxiliary_client import _codex_cloudflare_headers

        return UpstreamCredential(
            bearer=bearer,
            base_url=base_url or DEFAULT_CODEX_BASE_URL,
            extra_headers=_codex_cloudflare_headers(bearer),
            expires_at=getattr(entry, "expires_at", None),
        )


__all__ = ["CodexAdapter"]
```

- [ ] **Step 4: Register it** — `hermes_cli/proxy/adapters/__init__.py`. Add the import and registry entry:

```python
from hermes_cli.proxy.adapters.codex import CodexAdapter
```

```python
ADAPTERS: Dict[str, Type[UpstreamAdapter]] = {
    "nous": NousPortalAdapter,
    "xai": XAIGrokAdapter,
    "codex": CodexAdapter,
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -k codex -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/proxy/adapters/codex.py hermes_cli/proxy/adapters/__init__.py tests/hermes_cli/test_proxy.py
git commit -m "feat(proxy): add CodexAdapter (ChatGPT backend via openai-codex pool)"
```

---

### Task 4: CLI surface — `--provider codex`

**Files:**
- Modify: `hermes_cli/main.py` (the `proxy start` `--provider` help text, ~line 13261)
- Test: `tests/hermes_cli/test_proxy.py`

- [ ] **Step 1: Write the failing test**

```python
def test_registry_lists_codex():
    assert "codex" in ADAPTERS

def test_get_adapter_returns_codex_instance():
    from hermes_cli.proxy.adapters.codex import CodexAdapter
    assert isinstance(get_adapter("codex"), CodexAdapter)
```

- [ ] **Step 2: Run to verify it passes already (registry done in Task 3)**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -k "lists_codex or returns_codex" -q`
Expected: PASS (these guard the registry; they pass once Task 3 landed).

- [ ] **Step 3: Update help text** — `hermes_cli/main.py`, the `--provider` argument help for `proxy start`. Change `"Upstream provider: nous or xai (default: nous). See ..."` to:

```python
        help="Upstream provider: nous, xai, or codex (default: nous). See `hermes proxy providers`.",
```

- [ ] **Step 4: Verify the CLI lists codex**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m hermes_cli.main proxy providers`
Expected output includes: `codex  — OpenAI Codex (ChatGPT)`

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/main.py tests/hermes_cli/test_proxy.py
git commit -m "feat(proxy): expose codex as a --provider choice"
```

---

### Task 5: `codex-proxy` provider overlay + Codex-backend detection

**Files:**
- Modify: `hermes_cli/providers.py` (`HERMES_OVERLAYS`)
- Modify: `agent/chat_completion_helpers.py` (line ~574, `is_codex_backend`)
- Modify: `agent/agent_init.py` (line ~295, `api_mode` resolution)
- Test: `tests/hermes_cli/test_codex_proxy_overlay.py` (create)

- [ ] **Step 1: Write the failing test** — create `tests/hermes_cli/test_codex_proxy_overlay.py`:

```python
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
    # The is_codex_backend disjunction must include codex-proxy so the
    # transport omits max_output_tokens and adds session headers even though
    # the base_url is localhost (not chatgpt.com).
    import agent.chat_completion_helpers as h
    import inspect
    src = inspect.getsource(h)
    assert '"codex-proxy"' in src or "'codex-proxy'" in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_codex_proxy_overlay.py -q`
Expected: FAIL — `KeyError: 'codex-proxy'`.

- [ ] **Step 3a: Add the overlay** — `hermes_cli/providers.py`, inside `HERMES_OVERLAYS`, after the `openai-codex` entry:

```python
    "codex-proxy": HermesOverlay(
        transport="codex_responses",
        auth_type="api_key",
        base_url_override="http://127.0.0.1:8645/v1",
        base_url_env_var="HERMES_CODEX_PROXY_BASE_URL",
    ),
```

- [ ] **Step 3b: Add to `is_codex_backend`** — `agent/chat_completion_helpers.py:574`, change the first disjunct from `agent.provider == "openai-codex"` to:

```python
            agent.provider in {"openai-codex", "codex-proxy"}
```

- [ ] **Step 3c: Add to `api_mode` resolution** — `agent/agent_init.py:295`, change `elif agent.provider == "openai-codex":` to:

```python
        elif agent.provider in {"openai-codex", "codex-proxy"}:
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_codex_proxy_overlay.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/providers.py agent/chat_completion_helpers.py agent/agent_init.py tests/hermes_cli/test_codex_proxy_overlay.py
git commit -m "feat(providers): add codex-proxy overlay + Codex-backend detection"
```

---

### Task 6: Opt-in shared-secret bearer on the proxy (fixes review security finding)

**Files:**
- Modify: `hermes_cli/proxy/server.py` (the proxy request handler, before credential attach)
- Test: `tests/hermes_cli/test_proxy.py`

Behavior: if `HERMES_PROXY_TOKEN` is set in the proxy's environment, the inbound bearer must equal it or the proxy returns 401 *before* attaching the real credential. If unset, behavior is unchanged (backward compatible for nous/xai users).

- [ ] **Step 1: Write the failing tests**

```python
def test_server_requires_token_when_configured(monkeypatch):
    async def run():
        monkeypatch.setenv("HERMES_PROXY_TOKEN", "s3cret")
        adapter = FakeAdapter("http://unused.example/v1")
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/v1/chat/completions", json={},
                                        headers={"Authorization": "Bearer wrong"}) as resp:
                    assert resp.status == 401
                    body = await resp.json()
                    assert body["error"]["type"] == "proxy_unauthorized"
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_server_allows_matching_token(monkeypatch):
    async def run():
        monkeypatch.setenv("HERMES_PROXY_TOKEN", "s3cret")
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{proxy_base}/v1/chat/completions", json={},
                                        headers={"Authorization": "Bearer s3cret"}) as resp:
                    assert resp.status == 200
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -k "requires_token or matching_token" -q`
Expected: FAIL — first test returns 200/404 (no token check), not 401.

- [ ] **Step 3: Add the check** — `hermes_cli/proxy/server.py`. Add `import os` if not present, and at the top of the proxy request handler (the `handle_proxy` coroutine), after the path-allow check and before resolving the credential:

```python
        required = os.environ.get("HERMES_PROXY_TOKEN", "").strip()
        if required:
            presented = (request.headers.get("Authorization", "") or "").removeprefix("Bearer ").strip()
            if presented != required:
                return web.json_response(
                    {"error": {"type": "proxy_unauthorized",
                               "message": "Proxy bearer token missing or incorrect."}},
                    status=401,
                )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -q`
Expected: PASS (all proxy tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/proxy/server.py tests/hermes_cli/test_proxy.py
git commit -m "feat(proxy): opt-in HERMES_PROXY_TOKEN bearer check"
```

---

### Task 7: `hermes proxy install` — crash-safe launchd service

**Files:**
- Modify: `hermes_cli/proxy/cli.py` (`cmd_proxy_install`, `cmd_proxy_uninstall`, dispatch)
- Modify: `hermes_cli/main.py` (`install` / `uninstall` subparsers under `proxy`)
- Test: `tests/hermes_cli/test_proxy.py`

- [ ] **Step 1: Write the failing test** — append to `tests/hermes_cli/test_proxy.py`:

```python
def test_build_codex_proxy_plist_pins_global_home_and_crash_safe(tmp_path):
    from hermes_cli.proxy.cli import build_proxy_plist
    plist = build_proxy_plist(
        python_path="/venv/bin/python", hermes_home="/Users/x/.hermes",
        port=8645, proxy_token="tok123",
    )
    assert "ai.hermes.codex-proxy" in plist
    assert "<string>proxy</string>" in plist and "<string>codex</string>" in plist
    assert "/Users/x/.hermes" in plist                 # HERMES_HOME pinned
    assert "HERMES_PROXY_TOKEN" in plist and "tok123" in plist
    # crash-safe KeepAlive: restart on failure only, not on clean exit
    assert "SuccessfulExit" in plist
    assert "<true/>" not in plist.split("KeepAlive")[1].split("</dict>")[0]


def test_proxy_install_refuses_named_profile(monkeypatch, tmp_path, capsys):
    from hermes_cli.proxy.cli import cmd_proxy_install
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "ops"))
    rc = cmd_proxy_install(__import__("unittest").mock.MagicMock())
    assert rc == 2
    assert "global" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -k "plist or install_refuses" -q`
Expected: FAIL — `cannot import name 'build_proxy_plist'`.

- [ ] **Step 3: Implement** — add to `hermes_cli/proxy/cli.py`:

```python
import os
import secrets
import subprocess
import sys
from pathlib import Path

_PLIST_LABEL = "ai.hermes.codex-proxy"


def build_proxy_plist(*, python_path: str, hermes_home: str, port: int,
                      proxy_token: str) -> str:
    """Generate a crash-safe launchd plist for the codex proxy.

    KeepAlive={SuccessfulExit: false} restarts only on failure (avoids a
    crash-loop if the service ever exits cleanly). HERMES_HOME is pinned to the
    global root so the proxy reads/writes the global openai-codex pool.
    """
    log_dir = Path(hermes_home) / "logs"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_path}</string>
    <string>-m</string><string>hermes_cli.main</string>
    <string>proxy</string><string>start</string>
    <string>--provider</string><string>codex</string>
    <string>--port</string><string>{port}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERMES_HOME</key><string>{hermes_home}</string>
    <key>HERMES_PROXY_TOKEN</key><string>{proxy_token}</string>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{log_dir}/codex-proxy.log</string>
  <key>StandardErrorPath</key><string>{log_dir}/codex-proxy.error.log</string>
</dict>
</plist>
"""


def _global_hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root().resolve()


def cmd_proxy_install(args) -> int:
    """Install the codex proxy as a launchd service (global root only)."""
    from hermes_cli.auth import get_hermes_home
    current = get_hermes_home().resolve()
    global_root = _global_hermes_root()
    if current != global_root:
        print(
            f"Refusing to install: HERMES_HOME is a profile ({current}). "
            f"Run from the global root ({global_root}) so the proxy reads the "
            f"global openai-codex pool.",
            file=sys.stderr,
        )
        return 2
    port = getattr(args, "port", None) or 8645
    token = os.environ.get("HERMES_PROXY_TOKEN") or secrets.token_urlsafe(24)
    plist = build_proxy_plist(
        python_path=sys.executable, hermes_home=str(global_root),
        port=port, proxy_token=token,
    )
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    rc = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    if rc.returncode != 0:
        print(f"launchctl load failed: {rc.stderr.decode()}", file=sys.stderr)
        return 1
    print(f"Installed {_PLIST_LABEL} on port {port}.")
    print(f"Proxy bearer token (use as the dummy key in clients):\n  {token}")
    return 0


def cmd_proxy_uninstall(args) -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if plist_path.exists():
        plist_path.unlink()
    print(f"Uninstalled {_PLIST_LABEL}.")
    return 0
```

Then wire dispatch in `cmd_proxy` (add before the help fallback):

```python
    if sub == "install":
        return cmd_proxy_install(args)
    if sub == "uninstall":
        return cmd_proxy_uninstall(args)
```

And in `hermes_cli/main.py`, add subparsers under `proxy_subparsers` (next to `status`/`providers`):

```python
    proxy_install = proxy_subparsers.add_parser(
        "install", help="Install the codex proxy as a launchd service")
    proxy_install.add_argument("--port", type=int, default=None)
    proxy_subparsers.add_parser(
        "uninstall", help="Uninstall the codex proxy launchd service")
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py -k "plist or install_refuses" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/proxy/cli.py hermes_cli/main.py tests/hermes_cli/test_proxy.py
git commit -m "feat(proxy): hermes proxy install — crash-safe launchd service"
```

---

### Task 8 (ops runbook — gated, not code): migrate consumers

This task changes config and verifies behavior; there is no code/TDD. Execute the steps in order and STOP if any gate fails.

- [ ] **Step 1: Build + full test sweep**

Run: `cd ~/.hermes/hermes-agent && venv/bin/python -m pytest tests/hermes_cli/test_proxy.py tests/hermes_cli/test_codex_proxy_overlay.py -q`
Expected: all pass.

- [ ] **Step 2: Install + start the service**

Run (from the global root, no profile): `cd ~/.hermes/hermes-agent && venv/bin/python -m hermes_cli.main proxy install`
Record the printed bearer token as `$TOKEN`. Confirm: `hermes proxy status` shows `codex — ready`, and `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8645/health` returns `200`.

- [ ] **Step 3: GATE 1 — one profile via the proxy.** Point only `ops` at `codex-proxy` (set its active provider to `codex-proxy`, model `gpt-5.5`, api key = `$TOKEN`). Run a one-shot prompt as the `ops` profile and confirm a real completion returns through the proxy (check `~/.hermes/logs/codex-proxy.log` for `POST /v1/responses → 200`). If it fails, STOP and debug before migrating anything else.

- [ ] **Step 4: Bypass-path audit (closes the single-refresher gap).** For the migrated `ops` profile, confirm Codex token rotation now happens ONLY in the proxy:
  - Disable / reroute codex usage polling for the profile (`agent/account_usage.py` `_fetch_codex_account_usage` hits chatgpt.com directly and would rotate the global token). Verify no `resolve_codex_runtime_credentials` call fires for a `codex-proxy`-active profile (grep the call sites; add a guard if any fires).
  - Document the decision (route through proxy vs disable) in the spec's Migration section.

- [ ] **Step 5: Migrate the rest.** Repeat Step 3's config for `default`, `researcher`, `reviewer`. Strip the `openai-codex` credentials from `profiles/{ops,researcher,reviewer}/auth.json`. Restart the gateway so it drops cached clients.

- [ ] **Step 6: GATE 2/3 — the codex CLI.** Add a custom `model_provider` to `~/.codex/config.toml` (`wire_api="responses"`, `base_url="http://127.0.0.1:8645/v1"`, `env_key` whose value = `$TOKEN`). Run `codex exec "say PONG"` and confirm success with no Cloudflare 403 (already verified in the 2026-06-16 spike with a throwaway proxy; re-confirm against the real service).

- [ ] **Step 7: Confirm single holder.** `hermes auth list openai-codex` should show the credential only in the global pool; profile pools empty. Watch `~/.hermes/logs/codex-proxy.log` over a few real requests — exactly one process refreshing.

---

## Self-review

- **Spec coverage:** component 1 → Task 3; component 2 (extra_headers) → Task 1; body cap → Task 2; component 3 (registry/CLI) → Tasks 3-4; component 4 (overlay + backend detection) → Task 5 (simplified to provider-name disjunctions, superseding the `force_codex_backend` flag); component 5 (launchd install) → Task 7; security mitigation → Task 6; components 6-7 (migration + CLI config) + bypass-path audit → Task 8. All spec sections mapped.
- **Placeholder scan:** every code step has complete code; every run step has a command + expected result. No TBDs.
- **Type consistency:** `UpstreamCredential(extra_headers=...)` defined in Task 1 and consumed in Tasks 1/3; `CodexAdapter` name `"codex"` consistent across Tasks 3-4; `build_proxy_plist` signature consistent between Task 7 impl and test; `HERMES_PROXY_TOKEN` consistent between Tasks 6 and 7.
- **Deviation from spec:** Task 5 implements Codex-backend detection by adding `"codex-proxy"` to the existing `is_codex_backend` / `api_mode` provider-name disjunctions (mirroring `xai`), rather than the spec's `force_codex_backend` flag through two dataclasses. This is functionally equivalent, idiomatic, and ~3 lines instead of ~5 files. Spec component 4 should be updated to match.
