# Codex Proxy Upstream — Design Spec

- **Date:** 2026-06-16
- **Status:** Approved (design); pending implementation plan
- **Area:** `hermes_cli/proxy/` (local OpenAI-compatible proxy), `hermes_cli/providers.py`, profile + `~/.codex` config
- **Related:** Option 3 of the "keep Codex authenticated across all hermes profiles" investigation

## Problem

OpenAI's ChatGPT-mode Codex backend keeps **one active OAuth refresh-token chain per `(client_id, account)`**. Today multiple consumers each hold their *own* independently-minted Codex OAuth login on the **same ChatGPT account**:

- `default` profile (global `~/.hermes/auth.json`)
- `ops`, `researcher`, `reviewer` profiles (each its own `profiles/<name>/auth.json`)
- the standalone `codex` CLI (`~/.codex/auth.json`) and the Claude Code `codex:rescue` plugin (which shells out to that CLI)

Whenever any one of them refreshes or re-logs-in, OpenAI rotates the token server-side and **orphans every other consumer**, which then fails with `refresh_token_reused` / HTTP 401 `token_expired`. The behavioural workaround ("don't run `codex login`") does not scale: the profiles alone are a circular firing squad.

## Goal

One process holds and refreshes the single Codex token; every other consumer rides it without ever holding or rotating a refresh token. Keep the free ChatGPT subscription (no API-key billing). Eliminate the collision **by construction**, not by discipline.

## Non-goals

- Brokering nous/xai through the same instance (they already have adapters; one proxy process serves one `--provider`).
- Changing the Codex request/response *shape* — the proxy is a credential-attaching forwarder, not a translator.
- Multi-account / API-key fallback (those were Options 2 and 4; explicitly not chosen).

## Approach (chosen: A)

Extend the existing `hermes proxy` adapter registry with a `CodexAdapter`, plus one **generic** capability — letting an adapter attach `extra_headers` to its resolved credential. The Codex backend sits behind Cloudflare, which 403s requests from non-residential IPs unless they advertise an allowed `originator`; the adapter supplies the required headers from the same token it attaches.

**Rejected alternatives:**
- **B — special-case Codex header logic inside `server.py`.** Punctures the adapter abstraction; provider-specific branches rot and conflict on upstream merges.
- **C — a standalone codex-proxy script outside `hermes_cli/proxy/`.** Duplicates the entire aiohttp forwarder and loses `proxy status`/registry integration for no benefit.

## Topology

```
   hermes profiles  default / ops / researcher / reviewer
   (codex-proxy        base_url = http://127.0.0.1:8645/v1
    provider,          api_key  = "local" (dummy)
    dummy bearer)
   codex CLI ───────┐  (~/.codex/config.toml custom model_provider → same localhost)
   + codex:rescue   │
                    ▼
        ┌────────────────────────────────┐
        │  hermes proxy (launchd service)  │   ← SOLE holder + SOLE refresher
        │  --provider codex  :8645         │     of the Codex OAuth token
        │  CodexAdapter                    │
        └───────────────┬──────────────────┘
   swaps dummy bearer → real OAuth token
   + injects User-Agent / originator / ChatGPT-Account-ID
                        ▼
        reads openai-codex pool from global ~/.hermes/auth.json
        (flock-coordinated refresh; only this process refreshes it)
                        ▼
        https://chatgpt.com/backend-api/codex/responses
```

The collision is structurally impossible: exactly one process ever holds or rotates the Codex token. Profiles and the CLI hold only a dummy key.

## Components

Each unit has one purpose, a defined interface, and explicit dependencies.

### 1. `CodexAdapter` — `hermes_cli/proxy/adapters/codex.py`
Near-clone of `XAIGrokAdapter` (`adapters/xai.py`).
- `name = "codex"`, `display_name = "OpenAI Codex (ChatGPT)"`, `auth_hint = "hermes auth add openai-codex --type oauth"`.
- `allowed_paths = {"/responses", "/models"}` (the Codex backend is the Responses API).
- `is_authenticated()` / `get_credential()` / `get_retry_credential()` delegate to `CredentialPool` via `load_pool("openai-codex")` — the same pool machinery xAI uses, which **already supports Codex OAuth refresh + rotation** (`_save_codex_tokens`, `refresh_codex_oauth_pure` in `hermes_cli/auth.py`).
- `get_credential()` returns `UpstreamCredential(bearer=access_token, base_url=DEFAULT_CODEX_BASE_URL, extra_headers=_codex_cloudflare_headers(access_token))`.
- **Depends on:** `agent.credential_pool`, `hermes_cli.auth.DEFAULT_CODEX_BASE_URL` (`auth.py:80`), `agent.auxiliary_client._codex_cloudflare_headers` (`auxiliary_client.py:431`).

### 2. `extra_headers` hook — `adapters/base.py` + `server.py`
- Add optional `extra_headers: Optional[Mapping[str, str]] = None` (treated as empty when `None`) to the frozen `UpstreamCredential` dataclass — avoids a mutable default on a frozen dataclass.
- In `server.py::_open_upstream`, after `fwd_headers["Authorization"] = ...`, merge `active_cred.extra_headers` (adapter-supplied headers win over passed-through client headers).
- nous/xai return no `extra_headers`, so their behaviour is unchanged. Generic and upstream-friendly.

### 3. Registry + CLI — `adapters/__init__.py` + `hermes_cli/main.py`
- One line registering `CodexAdapter` in `ADAPTERS`.
- Add `codex` to the `--provider` choices/help text (`main.py:~13261`). `proxy status` / `proxy providers` pick it up automatically.

### 4. `codex-proxy` provider overlay — `hermes_cli/providers.py`
What the profiles point at. The real `openai-codex` overlay is left intact (it is the proxy's own upstream).
- New `HERMES_OVERLAYS["codex-proxy"]`: `transport="codex_responses"`, `auth_type="api_key"` (dummy), `base_url_override="http://127.0.0.1:8645/v1"`, `base_url_env_var` for override.
- **Critical:** the transport detects "is this the Codex backend?" by sniffing `/backend-api/codex` in the base URL (`agent/transports/codex.py:220`, `agent_init.py:746`). Pointed at localhost, that goes false — which would wrongly send `max_output_tokens` (rejected by the Codex backend) and skip the session headers. The overlay therefore carries a **`force_codex_backend` flag** wired into the transport's `is_codex_backend` resolution so codex-backend request shaping is applied regardless of the localhost URL.
- The localhost host also means `agent_init.py:746` will NOT attach the Cloudflare headers client-side — correct, because the proxy injects them (component 2).

### 5. `hermes proxy install` — `hermes_cli/proxy/cli.py` + `main.py`
- New subcommand + launchd plist, modeled on `hermes gateway install`. `KeepAlive` so the proxy auto-starts at login, survives reboot, and restarts on crash.
- Runs `hermes proxy start --provider codex` in the global-root profile (so it reads the global `auth.json` `openai-codex` pool).

### 6. Profile migration (config only, no code)
- Switch `default`, `ops`, `researcher`, `reviewer` to the `codex-proxy` provider.
- Strip the `openai-codex` credentials from `profiles/{ops,researcher,reviewer}/auth.json` — they no longer authenticate to Codex directly; they use the dummy key against the proxy.
- The global `~/.hermes/auth.json` `openai-codex` pool is retained — it is the proxy's upstream credential.

### 7. codex CLI / plugin config — `~/.codex/config.toml`
- A custom `model_provider` with `base_url = "http://127.0.0.1:8645/v1"`, `wire_api = "responses"`, and a dummy `env_key`, selected as the active provider. The `codex:rescue` plugin inherits this because it wraps the same CLI.

## Data flow

**Normal request:** client builds a Responses request → `POST 127.0.0.1:8645/v1/responses` with the dummy bearer → proxy strips the dummy `Authorization` + hop-by-hop headers (`_filter_request_headers`), calls `CodexAdapter.get_credential()` → selects/refreshes the `openai-codex` pool entry → returns `{bearer, base_url, extra_headers}` → `server.py` sets `Authorization: Bearer <real>` and merges the three Codex headers → forwards to `chatgpt.com/backend-api/codex/responses`; response streams back byte-for-byte.

**Refresh:** `get_credential` refreshes through the pool, which writes the rotated token back to the global `auth.json` under the cross-process flock (`_save_codex_tokens` re-reads inside the lock, `auth.py:3416`). Because the proxy is the *only* refresher, the single-use refresh token is never double-spent → `refresh_token_reused` cannot occur.

**Retry:** on upstream `401` → `get_retry_credential` does `pool.try_refresh_current()` then rotate; on `429` → `mark_exhausted_and_rotate`. One-shot, matching `XAIGrokAdapter.get_retry_credential`.

## Error handling

- **Proxy down** → clients get connection-refused and **fail fast** (no silent fallback to direct Codex — that would resurrect the collision). launchd `KeepAlive` restarts it. Accepted single-point-of-failure.
- **Terminal refresh failure** (only reachable if the global creds get clobbered out-of-band) → proxy returns 401; `proxy status` shows "needs attention"; remedy is a one-time `hermes auth add openai-codex`.
- **Cloudflare 403 (`cf-mitigated: challenge`)** → means `originator`/`User-Agent` didn't satisfy CF from this IP. The injected headers match `codex_cli_rs`, which passes on a residential IP; a datacenter IP can still 403 regardless of auth correctness (environment issue, surfaced by Gate 3).
- **`max_output_tokens` rejection** → prevented by the `force_codex_backend` flag (component 4).

## Testing

- **Unit:** `CodexAdapter.get_credential` returns bearer+base_url+`extra_headers` (mocked pool); `_codex_cloudflare_headers` extracts `ChatGPT-Account-ID` from a fake JWT claim; unknown paths 404; `extra_headers` merge leaves nous/xai unaffected (empty default).
- **Integration:** run the proxy against a stub upstream — assert `Authorization` swapped, the three headers injected, request body byte-identical, and a stubbed `401` drives exactly one rotated retry.
- **End-to-end gates (must pass before migrating profiles):**
  - **Gate 1** — `hermes -z` through a profile on the `codex-proxy` provider returns a real completion.
  - **Gate 2** — the standalone `codex` CLI, pointed at the proxy, runs a trivial prompt (the riskiest leg).
  - **Gate 3** — no Cloudflare 403 from the operator's IP.

## Migration & rollback

**Sequence:** land code → `hermes proxy install` + start → **Gate 1 on `ops` only** → migrate the other three profiles → configure + verify the CLI (**Gates 2/3**) → confirm only the proxy holds a Codex token.

**Rollback:** the `openai-codex` provider overlay is left fully intact, so reverting = repoint profiles back to `openai-codex` and re-add the credential to each profile's pool (or fall back to the Option 1 symlink). No code to unwind.

## Risks & open questions

1. **codex CLI routing (Gate 2)** — ChatGPT-login mode hardwires `chatgpt.com`; the custom `model_provider` approach is plausible but unverified until tried. If the CLI refuses a non-chatgpt provider for Codex models, the CLI/plugin leg falls back to "stay logged out" while the profile leg still wins.
2. **Cloudflare from non-residential IPs (Gate 3)** — out of code's control; relevant if the proxy ever runs on a VPS rather than the local Mac.
3. **`force_codex_backend` plumbing** — needs a clean path from the overlay flag into the transport's `is_codex_backend` resolution without regressing direct `openai-codex` detection.

## Out of scope

- Health-checking the proxy from the gateway (possible follow-up).
- Routing nous/xai through the same proxy instance.
- API-key or multi-account strategies.
