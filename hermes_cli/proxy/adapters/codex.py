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

        # Lazy import: agent.auxiliary_client is a large (~5800-line) module and
        # the adapter registry imports every adapter eagerly — importing it at
        # module load would burden nous/xai-only proxy runs. No import cycle.
        from agent.auxiliary_client import codex_cloudflare_headers

        return UpstreamCredential(
            bearer=bearer,
            base_url=base_url or DEFAULT_CODEX_BASE_URL,
            extra_headers=codex_cloudflare_headers(bearer),
            expires_at=getattr(entry, "expires_at", None),
        )


__all__ = ["CodexAdapter"]
