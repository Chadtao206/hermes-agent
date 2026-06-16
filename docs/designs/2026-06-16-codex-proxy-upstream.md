# Codex Proxy Upstream — Design Spec

- **Date:** 2026-06-16
- **Status:** Approved (design); revised after adversarial review (v2); pending implementation plan
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

The intent is that exactly one process holds and rotates the Codex token, with profiles and the CLI holding only a dummy key. **This is not automatic.** Several paths resolve Codex credentials directly — usage accounting (`agent/account_usage.py`), the auxiliary refresh client (`agent/auxiliary_client.py`), and `resolve_codex_runtime_credentials` callers in `runtime_provider.py`/`models.py`/`run_agent.py` — and a migrated profile still *reads* the global pool through the read-only fallback (`auth.py:_global_auth_file_path`), so these can refresh and rotate the global token out-of-band, re-introducing `refresh_token_reused`. The single-refresher guarantee only holds once migration neutralizes those paths (see Migration §).

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
- **Raise the request-body cap.** `server.py:create_app` builds `web.Application()` (`server.py:92`) with aiohttp's 1 MB default `client_max_size`; `await request.read()` (`server.py:131`) will 413 on large Codex turns (full conversation input + `reasoning.encrypted_content` replay blobs). Pass a generous `client_max_size` (e.g. 64 MB). This is a pre-existing proxy limitation that affects all upstreams; Codex just hits it sooner.
- **Don't block the event loop on refresh.** `server.py:122` calls the synchronous `adapter.get_credential()` directly in the request coroutine. A blocking token refresh would stall *every* in-flight request on the proxy, not just the refreshing one. Wrap the call in `loop.run_in_executor`.

### 3. Registry + CLI — `adapters/__init__.py` + `hermes_cli/main.py`
- One line registering `CodexAdapter` in `ADAPTERS`.
- Add `codex` to the `--provider` choices/help text (`main.py:~13261`). `proxy status` / `proxy providers` pick it up automatically.

### 4. `codex-proxy` provider overlay — `hermes_cli/providers.py`
What the profiles point at. The real `openai-codex` overlay is left intact (it is the proxy's own upstream).
- New `HERMES_OVERLAYS["codex-proxy"]`: `transport="codex_responses"`, `auth_type="api_key"` (dummy), `base_url_override="http://127.0.0.1:8645/v1"`, `base_url_env_var` for override.
- **Critical (and larger than a one-liner).** "Is this the Codex backend?" is actually computed at **`agent/chat_completion_helpers.py:574`** (`is_codex_backend = agent.provider == "openai-codex" or (hostname == "chatgpt.com" and "/backend-api/codex" in base_url)`) — *not* at `transports/codex.py:220`/`agent_init.py:746`, which only consume the resolved flag. Pointed at localhost with provider `codex-proxy`, both disjuncts are false → `max_output_tokens` is wrongly sent (rejected by the Codex backend, `codex.py:239`) and session headers are skipped (`codex.py:220`). The fix is a **`force_codex_backend` flag threaded through multiple sites**: add the field to `HermesOverlay` and `ProviderDef` (`providers.py`), propagate it via `get_provider()`, stash it on the agent, and OR it into the `chat_completion_helpers.py:574` expression. `api_mode="codex_responses"` is separately ensured by the overlay's `transport` mapping (`providers.py` `TRANSPORT_TO_API_MODE`), so that leg is covered — but budget this as a ~4–5 file change, not one line.
- The localhost host also means `agent_init.py:746` will NOT attach the Cloudflare headers client-side — correct, because the proxy injects them (component 2).

### 5. `hermes proxy install` — `hermes_cli/proxy/cli.py` + `main.py`
- New subcommand + launchd plist, modeled on `hermes gateway install`.
- **Crash-loop hazard:** the gateway plist uses unconditional `KeepAlive=true`, but `proxy start` does an up-front `is_authenticated()` gate that `exit 2`s on bad/exhausted creds (`cli.py:46`). Copied naïvely, a dead token → exit 2 → relaunch → exit 2, a port flapping every ~10 s. Use **`KeepAlive={Crashed: true}`** (or drop the startup auth gate for the service and let it bind + serve 401s, which is the stable failure mode the Error-handling § assumes). Also: a transient 429 cooldown makes `is_authenticated()` false — don't let a rate-limit refuse to start the service.
- **Pin `HERMES_HOME` to the global root.** Under launchd there is no shell; `load_pool` resolves the store from `get_hermes_home()` (`auth.py:862`) and refresh write-back targets `_auth_file_path()`. The plist must set `HERMES_HOME` to `get_default_hermes_root()` explicitly, and `proxy install` must refuse (or loudly warn) if invoked from inside a named profile — otherwise the proxy reads/writes the wrong pool and silently re-creates the collision.
- Runs `hermes proxy start --provider codex` against the global-root `auth.json` `openai-codex` pool.

### 6. Profile migration (config + a required bypass-path audit)
- Switch `default`, `ops`, `researcher`, `reviewer` to the `codex-proxy` provider.
- Strip the `openai-codex` credentials from `profiles/{ops,researcher,reviewer}/auth.json` — they no longer authenticate to Codex directly; they use the dummy key against the proxy.
- The global `~/.hermes/auth.json` `openai-codex` pool is retained — it is the proxy's upstream credential.
- **Required (closes the single-refresher gap):** stripping per-profile creds is *not* sufficient, because the read-only global fallback (`auth.py:_global_auth_file_path`) still lets a migrated profile resolve the global token, and direct callers of `resolve_codex_runtime_credentials` will then refresh it. Audit and neutralize, for migrated profiles:
  - **Usage accounting** — `_fetch_codex_account_usage` / `fetch_account_usage(provider="openai-codex")` (`account_usage.py:351,542`) hit `chatgpt.com/.../usage` directly with `User-Agent: codex-cli` (no `_codex_cloudflare_headers`); after migration they either 401 (no local token) or resolve+rotate the global token and CF-403 on a non-residential IP. Decide: route through the proxy, or disable codex usage polling in migrated profiles. Document the choice.
  - **Auxiliary refresh** (`auxiliary_client.py` `_refresh_provider_credentials`, `force_refresh=True`) and any `runtime_provider`/`models`/`run_agent` resolution — confirm these don't fire for a `codex-proxy`-active profile, or guard them.

### 7. codex CLI / plugin config — `~/.codex/config.toml`
- A custom `model_provider` with `base_url = "http://127.0.0.1:8645/v1"`, `wire_api = "responses"`, and a dummy `env_key`, selected as the active provider. The `codex:rescue` plugin inherits this because it wraps the same CLI.

## Data flow

**Normal request:** client builds a Responses request → `POST 127.0.0.1:8645/v1/responses` with the dummy bearer → proxy strips the dummy `Authorization` + hop-by-hop headers (`_filter_request_headers`), calls `CodexAdapter.get_credential()` → selects/refreshes the `openai-codex` pool entry → returns `{bearer, base_url, extra_headers}` → `server.py` sets `Authorization: Bearer <real>` and merges the three Codex headers → forwards to `chatgpt.com/backend-api/codex/responses`; response streams back byte-for-byte.

**Refresh:** `get_credential` refreshes through the pool, which writes the rotated token back to the global `auth.json` under the cross-process flock (`_save_codex_tokens` re-reads inside the lock, `auth.py:3416`). Because the proxy is the *only* refresher, the single-use refresh token is never double-spent → `refresh_token_reused` cannot occur.

**Retry:** on upstream `401` → `get_retry_credential` does `pool.try_refresh_current()` then rotate; on `429` → `mark_exhausted_and_rotate`. One-shot, matching `XAIGrokAdapter.get_retry_credential`.

## Error handling

- **Two distinct failure modes (don't conflate them):** (a) proxy *down* → clients get connection-refused immediately (fail fast); (b) proxy *up with a dead token* → clients get **401** (from `get_credential` raising, `server.py:122`), not connection-refused. The spec must commit to (b) as the steady-state failure — which means *not* using the startup `is_authenticated()` gate for the installed service (see component 5), so the proxy stays bound and returns 401 rather than crash-looping.
- **Terminal refresh failure** (only reachable if the global creds get clobbered out-of-band) → proxy returns 401; `proxy status` shows "needs attention"; remedy is a one-time `hermes auth add openai-codex`.
- **Stream interruption** → `server.py:226` currently swallows a mid-stream `ClientError` and calls `write_eof()`, delivering a *silently truncated* SSE stream the client reads as a short completion. Also `sock_read=300` (`server.py:133`) caps inter-chunk gaps below what hermes' own codex path tolerates (`request_timeout_seconds` can be `None`). Make `sock_read` configurable/higher and surface stream errors (abort, don't `write_eof`) so truncation is detectable.
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

**Rollback (more involved than "no code to unwind"):** the `openai-codex` provider *overlay* is intact, but the per-profile credentials were deleted in step 6 and are single-use OAuth tokens — they can't be restored, only re-minted via a fresh `hermes auth add openai-codex` device login *per profile*, which itself rotates the global token and re-triggers the original firing squad. Full rollback therefore = (1) `hermes proxy stop` + unload the launchd service, (2) revert `~/.codex/config.toml`, (3) repoint profiles to `openai-codex` and re-auth each, (4) **restart the gateway / any long-lived process** so it drops the cached `codex-proxy` client. The low-risk escape hatch remains the Option 1 symlink.

## Security

The proxy binds `127.0.0.1` and accepts **any** bearer, then attaches the real ChatGPT-subscription credential to any request on an allowed path — there is no auth on the proxy itself (`server.py` validates the path, never the bearer). This is a genuine posture change: today spending the sub requires reading `auth.json` *and* reproducing the OAuth + Cloudflare-header machinery; after this, any local process (other CLIs, MCP servers, a malicious `postinstall`) can spend it with a trivial unauthenticated `POST http://127.0.0.1:8645/v1/responses`. **Mitigation to include:** require a shared-secret bearer — the "dummy" key becomes a real per-host token the proxy checks before attaching the credential, rejecting mismatches with 401. (This applies to the existing nous/xai upstreams too, but extending the surface to a paid subscription is what makes it worth fixing now.)

## Risks & open questions

1. **Gate 2 is the load-bearing justification — verify it in isolation FIRST.** The proxy's only advantage over the far cheaper Option 1 (symlink) is that it also fronts the external `codex` CLI. But ChatGPT-login mode binds to the reserved built-in `openai`/`chatgpt` provider; a custom `model_provider` with a dummy `env_key` pointed at localhost is exactly the unverified combination. Recommended sequencing: **spike Gate 2 as a throwaway** (`~/.codex/config.toml` → a hand-run `hermes proxy start --provider codex`, one prompt) *before* building `proxy install`, the overlay, and `force_codex_backend`. If Gate 2 fails, the CLI leg collapses to "stay logged out" — which is the symlink outcome at a fraction of the cost, and the proxy is not worth building.
2. **Cloudflare from non-residential IPs (Gate 3)** — out of code's control; relevant if the proxy ever runs on a VPS rather than the local Mac.
3. **`force_codex_backend` plumbing** — multi-site (~4–5 files), per component 4; not a one-liner. Must not regress direct `openai-codex` detection at `chat_completion_helpers.py:574`.
4. **Model selection / discovery** — the spec doesn't yet specify how a profile names its model under `codex-proxy` (e.g. `gpt-5.x`) or how default-model resolution maps to it; and `chatgpt.com/backend-api/codex` serves no standard `/v1/models`, so the `/models` allowed-path is largely inert (the context-window probe at `model_metadata.py:1345` hits chatgpt.com directly, not the proxy). Resolve model naming explicitly; treat `/models` as best-effort only.
5. **Body passthrough across two client shapes** — the proxy forwards the request body byte-for-byte, but the hermes codex transport and the codex-rs CLI produce *different* request bodies against the same upstream; Gate 2 must confirm the CLI's body (incl. any `service_tier`/fast-mode fields) is accepted as-is.

## Out of scope

- Health-checking the proxy from the gateway (possible follow-up).
- Routing nous/xai through the same proxy instance.
- API-key or multi-account strategies.
