"""
WhatsApp Cloud API adapter — official Meta WhatsApp Business Platform.

This adapter is a *complement* to ``whatsapp.py`` (the Baileys bridge), not
a replacement. The two are independent:

- ``whatsapp.py``      — unofficial Baileys bridge, personal accounts, no
                         public URL needed, account-ban risk.
- ``whatsapp_cloud.py`` (this file) — official Meta Cloud API, Business
                         account required, public webhook URL required,
                         token-based auth.

Both share gating / mention / formatting behavior via ``WhatsAppBehaviorMixin``.

Phase scope (this file evolves across phases):
- Phase 2 — outbound text via Graph API + webhook server with verify-token
            handshake.
- Phase 3 — X-Hub-Signature-256 HMAC verification (raw body, constant-time)
            + wamid replay protection + dispatch via handle_message. Phase 3
            adapter is end-to-end usable for text DMs.
- Phase 4 — media upload + send (image/video/audio/document) and inbound
            media download via the Graph media endpoint.
- Phase 5 — 24-hour conversation window + template fallback.

Required env vars to enable the adapter:
- WHATSAPP_CLOUD_PHONE_NUMBER_ID  (the Graph URL path component)
- WHATSAPP_CLOUD_ACCESS_TOKEN     (System User permanent token)

Optional / Phase-3+:
- WHATSAPP_CLOUD_APP_ID
- WHATSAPP_CLOUD_APP_SECRET       (HMAC key for X-Hub-Signature-256)
- WHATSAPP_CLOUD_WABA_ID          (analytics / future use)
- WHATSAPP_CLOUD_VERIFY_TOKEN     (hub.verify_token shared secret)
- WHATSAPP_CLOUD_WEBHOOK_HOST     (default 0.0.0.0)
- WHATSAPP_CLOUD_WEBHOOK_PORT     (default 8090)
- WHATSAPP_CLOUD_WEBHOOK_PATH     (default /whatsapp/webhook)
- WHATSAPP_CLOUD_API_VERSION      (default v20.0)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections import OrderedDict
from typing import Any, Dict, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin

logger = logging.getLogger(__name__)


DEFAULT_API_VERSION = "v20.0"
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8090
DEFAULT_WEBHOOK_PATH = "/whatsapp/webhook"
GRAPH_API_BASE = "https://graph.facebook.com"
# Meta retries failed webhooks for up to 7 days. We don't need to remember
# every wamid for the full retry window — the practical risk is duplicate
# delivery within minutes, not days. 5000 entries with FIFO eviction is
# plenty for normal traffic and bounds memory.
WAMID_DEDUP_CACHE_SIZE = 5000


def check_whatsapp_cloud_requirements() -> bool:
    """Return whether transport dependencies are available.

    aiohttp is needed for the webhook server (inbound). httpx is needed
    for Graph API calls (outbound). Both ship with hermes-agent's default
    dependency set, so this should always be True in normal installs.
    """
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


class WhatsAppCloudAdapter(WhatsAppBehaviorMixin, BasePlatformAdapter):
    """WhatsApp Business Cloud API adapter.

    Outbound: HTTPS POST to ``graph.facebook.com/<api_version>/<phone_id>/messages``.
    Inbound: aiohttp server accepting Meta's webhook payloads.

    The mixin must come first in the bases list so its ``format_message``
    overrides ``BasePlatformAdapter.format_message`` (the base provides a
    generic implementation that does not convert markdown to WhatsApp
    syntax). The Baileys adapter does the same.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WHATSAPP_CLOUD)
        extra = config.extra or {}

        # Required
        self._phone_number_id: str = str(extra.get("phone_number_id", "")).strip()
        self._access_token: str = str(extra.get("access_token", "")).strip()

        # Optional / used in later phases
        self._app_id: str = str(extra.get("app_id", "")).strip()
        self._app_secret: str = str(extra.get("app_secret", "")).strip()
        self._waba_id: str = str(extra.get("waba_id", "")).strip()
        self._verify_token: str = str(extra.get("verify_token", "")).strip()

        # Webhook server config
        self._webhook_host: str = str(extra.get("webhook_host", DEFAULT_WEBHOOK_HOST))
        self._webhook_port: int = int(extra.get("webhook_port", DEFAULT_WEBHOOK_PORT))
        self._webhook_path: str = self._normalize_path(
            extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        )
        self._health_path: str = self._normalize_path(
            extra.get("health_path", "/health")
        )

        # Graph API
        self._api_version: str = str(extra.get("api_version", DEFAULT_API_VERSION))

        # Behavior-mixin contract: these names are read by the mixin's
        # gating methods. Derived from env / config the same way the
        # Baileys adapter derives them.
        import os

        self._reply_prefix: Optional[str] = extra.get("reply_prefix")
        self._dm_policy: str = str(
            extra.get("dm_policy") or os.getenv("WHATSAPP_DM_POLICY", "open")
        ).strip().lower()
        self._allow_from: set[str] = self._coerce_allow_list(
            extra.get("allow_from") or extra.get("allowFrom")
        )
        self._group_policy: str = str(
            extra.get("group_policy") or os.getenv("WHATSAPP_GROUP_POLICY", "open")
        ).strip().lower()
        self._group_allow_from: set[str] = self._coerce_allow_list(
            extra.get("group_allow_from") or extra.get("groupAllowFrom")
        )
        self._mention_patterns = self._compile_mention_patterns()

        # Webhook dedup state — wamid → True. OrderedDict gives O(1) FIFO
        # eviction. In-memory only; Phase 5 may promote to SessionDB if we
        # decide we need replay protection across gateway restarts.
        self._seen_wamids: "OrderedDict[str, bool]" = OrderedDict()
        self._duplicate_count: int = 0
        self._accepted_count: int = 0
        self._rejected_signature_count: int = 0

        # Runtime
        self._runner = None
        self._http_client: Optional["httpx.AsyncClient"] = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _normalize_path(path: Any) -> str:
        raw = str(path or "").strip() or "/"
        return raw if raw.startswith("/") else f"/{raw}"

    def _graph_url(self, path: str) -> str:
        """Build a Graph API URL for this adapter's phone-number scope."""
        if path.startswith("/"):
            path = path[1:]
        return f"{GRAPH_API_BASE}/{self._api_version}/{self._phone_number_id}/{path}"

    def _effective_reply_prefix(self) -> str:
        """Cloud API has no self-chat concept — never prepend a reply prefix.

        Override the mixin default which keys off WHATSAPP_MODE=self-chat
        (a Baileys-only setting).
        """
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        return ""

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> bool:
        if not check_whatsapp_cloud_requirements():
            self._set_fatal_error(
                "whatsapp_cloud_deps_missing",
                "aiohttp and httpx are required for whatsapp_cloud — "
                "reinstall hermes-agent.",
                retryable=False,
            )
            return False
        if not self._phone_number_id or not self._access_token:
            self._set_fatal_error(
                "whatsapp_cloud_unconfigured",
                "WHATSAPP_CLOUD_PHONE_NUMBER_ID and WHATSAPP_CLOUD_ACCESS_TOKEN "
                "are required.",
                retryable=False,
            )
            return False

        # Outbound HTTP client. Tighter keepalive matches other platform
        # adapters so idle CLOSE_WAIT drains promptly (#18451).
        from gateway.platforms._http_client_limits import platform_httpx_limits

        self._http_client = httpx.AsyncClient(
            timeout=30.0, limits=platform_httpx_limits()
        )

        # Inbound webhook server.
        app = web.Application()
        app.router.add_get(self._health_path, self._handle_health)
        app.router.add_get(self._webhook_path, self._handle_verify)
        app.router.add_post(self._webhook_path, self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
        await site.start()

        self._mark_connected()
        logger.info(
            "[whatsapp_cloud] Listening on %s:%d%s (Graph %s, phone_id=%s)",
            self._webhook_host,
            self._webhook_port,
            self._webhook_path,
            self._api_version,
            self._phone_number_id,
        )
        if not self._verify_token:
            logger.warning(
                "[whatsapp_cloud] WHATSAPP_CLOUD_VERIFY_TOKEN is not set — "
                "the GET subscription handshake will fail until it is."
            )
        if not self._app_secret:
            logger.warning(
                "[whatsapp_cloud] WHATSAPP_CLOUD_APP_SECRET is not set — "
                "incoming webhook POSTs will be refused with 503. Set "
                "the app secret to enable inbound message delivery."
            )
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("[whatsapp_cloud] webhook server cleanup failed")
            self._runner = None
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                logger.exception("[whatsapp_cloud] http client close failed")
            self._http_client = None
        self._mark_disconnected()

    # ------------------------------------------------------------------ outbound
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message via Graph API.

        ``chat_id`` is the recipient's WhatsApp ID (``wa_id``) — typically
        their phone number with country code, no plus sign.
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self._outgoing_chunk_limit())

        url = self._graph_url("messages")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        last_message_id: Optional[str] = None
        for idx, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": chat_id,
                "type": "text",
                "text": {"body": chunk, "preview_url": True},
            }
            if reply_to and idx == 0:
                # Quote the user's message on the first chunk only.
                payload["context"] = {"message_id": reply_to}
            try:
                resp = await self._http_client.post(url, headers=headers, json=payload)
            except Exception as exc:
                logger.exception("[whatsapp_cloud] send failed")
                return SendResult(success=False, error=str(exc))

            if resp.status_code != 200:
                # Meta returns structured errors in the body — surface them
                # to the caller so log lines have actionable context.
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text[:500]}
                error_msg = self._format_graph_error(body, resp.status_code)
                logger.warning(
                    "[whatsapp_cloud] send rejected (status=%d): %s",
                    resp.status_code,
                    error_msg,
                )
                return SendResult(success=False, error=error_msg)

            try:
                data = resp.json()
                ids = data.get("messages") or []
                if ids:
                    last_message_id = ids[0].get("id")
            except Exception:
                pass

        return SendResult(success=True, message_id=last_message_id)

    @staticmethod
    def _format_graph_error(body: Dict[str, Any], status_code: int) -> str:
        err = (body or {}).get("error") or {}
        # Graph API error shape:
        # {"error": {"message": "...", "type": "...", "code": ..., "fbtrace_id": "..."}}
        message = err.get("message") or body.get("raw") or "unknown error"
        code = err.get("code")
        if code is not None:
            return f"graph error {code} (HTTP {status_code}): {message}"
        return f"HTTP {status_code}: {message}"

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # Cloud API doesn't expose a direct "chat info" endpoint the way
        # Slack/Discord do — we just echo the wa_id. Profile name (when
        # known) flows in via webhook ``contacts[].profile.name`` and is
        # cached on the MessageEvent, not here.
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------ inbound
    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response(
            {
                "status": "ok",
                "platform": self.platform.value,
                "phone_number_id": self._phone_number_id,
                "webhook_path": self._webhook_path,
                "verify_token_configured": bool(self._verify_token),
                "app_secret_configured": bool(self._app_secret),
                "accepted": self._accepted_count,
                "duplicates": self._duplicate_count,
                "rejected_signature": self._rejected_signature_count,
            }
        )

    async def _handle_verify(self, request: "web.Request") -> "web.Response":
        """Meta subscription verification handshake.

        Meta calls GET ``<webhook>?hub.mode=subscribe&hub.verify_token=...
        &hub.challenge=...``. We must echo the challenge as plain text iff
        ``hub.mode == "subscribe"`` AND ``hub.verify_token`` matches the
        shared secret. Constant-time comparison.
        """
        if not self._verify_token:
            # Misconfigured server — refuse rather than silently accepting
            # any verify_token, which would let an attacker subscribe.
            return web.Response(status=503, text="verify_token not configured")

        mode = request.query.get("hub.mode", "")
        token = request.query.get("hub.verify_token", "")
        challenge = request.query.get("hub.challenge", "")

        if mode != "subscribe":
            return web.Response(status=400, text="bad mode")

        # Constant-time compare to avoid token-length / token-content leaks
        # via timing. ``hmac.compare_digest`` works on str.
        import hmac as _hmac

        if not _hmac.compare_digest(token, self._verify_token):
            return web.Response(status=403, text="verify_token mismatch")
        if not challenge:
            return web.Response(status=400, text="missing challenge")
        return web.Response(text=challenge, content_type="text/plain")

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """Inbound webhook POST handler.

        Lifecycle:
          1. Read raw bytes (signature is over the raw body — JSON parsing
             must NOT happen first, or the bytes change).
          2. Verify ``X-Hub-Signature-256`` HMAC against ``app_secret``.
          3. Parse JSON.
          4. Walk ``entry[].changes[].value.{messages, statuses, contacts}``.
          5. Per-message: dedup by wamid, build MessageEvent, dispatch via
             ``handle_message`` (which runs the mixin's gating).
          6. Always respond 200 once we've ack'd a valid request — Meta
             retries on non-200 for up to 7 days, and we don't want to
             multiply downstream agent work because of a transient bug
             during dispatch.
        """
        try:
            raw = await request.read()
        except Exception:
            return web.Response(status=400)

        # Meta's documented max payload is 3MB. Reject earlier than aiohttp
        # would so we don't even compute HMAC over giant junk.
        if len(raw) > 3 * 1024 * 1024:
            return web.Response(status=413)

        # Refuse to accept anything if app_secret isn't configured. Without
        # it we can't authenticate the sender, and the handler would be a
        # data-injection point. Same defensive posture as the GET verify
        # handshake refusing when verify_token is empty.
        if not self._app_secret:
            logger.error(
                "[whatsapp_cloud] webhook POST refused: app_secret unset. "
                "Set WHATSAPP_CLOUD_APP_SECRET to enable inbound delivery."
            )
            return web.Response(status=503, text="app_secret not configured")

        signature_header = request.headers.get("X-Hub-Signature-256", "")
        if not self._verify_signature(raw, signature_header):
            self._rejected_signature_count += 1
            logger.warning(
                "[whatsapp_cloud] rejected webhook: invalid X-Hub-Signature-256 "
                "(header=%r, body_len=%d)",
                signature_header,
                len(raw),
            )
            return web.Response(status=401)

        # Parse only AFTER signature passes — bad JSON from an attacker is
        # already filtered out, this just guards against Meta sending
        # something malformed.
        import json as _json

        try:
            payload = _json.loads(raw)
        except Exception:
            logger.warning("[whatsapp_cloud] webhook body is not valid JSON")
            return web.Response(status=400)

        if not isinstance(payload, dict):
            return web.Response(status=400)

        await self._dispatch_payload(payload)
        return web.Response(status=200)

    # ------------------------------------------------------------------ signature
    def _verify_signature(self, raw_body: bytes, header: str) -> bool:
        """Verify the X-Hub-Signature-256 HMAC.

        Meta sends ``sha256=<hex>``; we compute the same HMAC with
        ``app_secret`` as the key and ``raw_body`` (UTF-8 bytes, not
        re-serialized JSON) as the message. Constant-time compare.
        """
        if not self._app_secret or not header:
            return False
        if not header.startswith("sha256="):
            return False
        expected_hex = header[len("sha256="):].strip()
        if not expected_hex:
            return False
        computed = hmac.new(
            self._app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed.lower(), expected_hex.lower())

    # ------------------------------------------------------------------ dispatch
    def _dedup_wamid(self, wamid: str) -> bool:
        """Return True if this wamid is being seen for the first time.

        Returns False (and increments duplicate counter) if the wamid is
        already in the in-memory cache. Cache is FIFO-evicted at
        ``WAMID_DEDUP_CACHE_SIZE``.
        """
        if not wamid:
            # No wamid means we can't dedup — let it through. Meta should
            # always populate ``id``, but be defensive.
            return True
        if wamid in self._seen_wamids:
            self._duplicate_count += 1
            return False
        self._seen_wamids[wamid] = True
        # Trim oldest entries to stay under the cap.
        while len(self._seen_wamids) > WAMID_DEDUP_CACHE_SIZE:
            self._seen_wamids.popitem(last=False)
        return True

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Walk a verified Meta webhook payload and dispatch each message.

        Payload shape (truncated):
          {object, entry: [{id, changes: [{value: {messages, contacts,
          statuses, metadata}, field: "messages"}]}]}

        We surface ``messages`` events as MessageEvents; ``statuses``
        events (sent/delivered/read/failed) are logged but not dispatched
        — the agent doesn't currently consume delivery receipts and
        forwarding them would create noisy synthetic events.
        """
        if payload.get("object") != "whatsapp_business_account":
            logger.debug(
                "[whatsapp_cloud] ignoring non-WABA payload (object=%r)",
                payload.get("object"),
            )
            return
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                if change.get("field") != "messages":
                    # Other fields (account_alerts, template_status_update,
                    # etc.) are subscription-dependent and not message
                    # ingress. Silent skip.
                    continue
                value = change.get("value") or {}
                contacts = value.get("contacts") or []
                metadata = value.get("metadata") or {}
                # Build a wa_id → profile-name index for the messages we're
                # about to surface.
                contacts_by_waid: Dict[str, str] = {}
                for contact in contacts:
                    if not isinstance(contact, dict):
                        continue
                    wa_id = str(contact.get("wa_id") or "").strip()
                    profile = contact.get("profile") or {}
                    name = str(profile.get("name") or "").strip()
                    if wa_id:
                        contacts_by_waid[wa_id] = name

                for raw_message in value.get("messages") or []:
                    if not isinstance(raw_message, dict):
                        continue
                    wamid = str(raw_message.get("id") or "").strip()
                    if not self._dedup_wamid(wamid):
                        logger.debug(
                            "[whatsapp_cloud] duplicate wamid %s, skipping",
                            wamid,
                        )
                        continue
                    event = self._build_message_event_from_cloud(
                        raw_message, contacts_by_waid, metadata
                    )
                    if event is None:
                        continue
                    self._accepted_count += 1
                    try:
                        await self.handle_message(event)
                    except Exception:
                        # Dispatch errors must not bubble out — Meta would
                        # retry the whole batch, multiplying the bug.
                        logger.exception(
                            "[whatsapp_cloud] handle_message raised for wamid %s",
                            wamid,
                        )

                # Log status updates at debug level — useful for diagnosing
                # "did Meta accept my outbound" without flooding INFO logs.
                for status in value.get("statuses") or []:
                    if isinstance(status, dict):
                        logger.debug(
                            "[whatsapp_cloud] status %s for %s",
                            status.get("status"),
                            status.get("id"),
                        )

    def _build_message_event_from_cloud(
        self,
        raw_message: Dict[str, Any],
        contacts_by_waid: Dict[str, str],
        metadata: Dict[str, Any],
    ) -> Optional[MessageEvent]:
        """Convert a Cloud-API message object into a Hermes MessageEvent.

        Phase 3 only handles ``type: "text"`` end-to-end. Non-text types
        produce a MessageEvent with the appropriate ``MessageType`` and
        empty media list — Phase 4 will populate media URLs by issuing
        ``GET /{media_id}`` against the Graph API to download the binary.

        Returns None if the message is filtered out by the mixin's gating
        (broadcast filter, allow-list, mention requirements).
        """
        msg_type_str = str(raw_message.get("type") or "text").lower()
        body = ""
        if msg_type_str == "text":
            text = raw_message.get("text") or {}
            body = str(text.get("body") or "")
        elif msg_type_str in {"button", "interactive"}:
            # Quick-reply buttons. Treat the button payload as text so the
            # agent can reason about the user's choice.
            if msg_type_str == "button":
                body = str((raw_message.get("button") or {}).get("text") or "")
            else:
                inter = raw_message.get("interactive") or {}
                # button_reply / list_reply both expose ``title``
                inner = inter.get("button_reply") or inter.get("list_reply") or {}
                body = str(inner.get("title") or "")

        message_type = {
            "text": MessageType.TEXT,
            "image": MessageType.PHOTO,
            "video": MessageType.VIDEO,
            "audio": MessageType.VOICE,
            "voice": MessageType.VOICE,
            "document": MessageType.DOCUMENT,
            "sticker": MessageType.PHOTO,
            "button": MessageType.TEXT,
            "interactive": MessageType.TEXT,
            "location": MessageType.TEXT,
            "contacts": MessageType.TEXT,
        }.get(msg_type_str, MessageType.TEXT)

        sender_id = str(raw_message.get("from") or "").strip()
        sender_name = contacts_by_waid.get(sender_id, "")

        # Cloud API doesn't have a separate "chat" entity for DMs — chat_id
        # equals the sender's wa_id. Group support is deferred to v2.
        chat_id = sender_id

        # Build the data dict the mixin's _should_process_message expects.
        # Cloud API uses different field names from Baileys, so we adapt.
        gating_data = {
            "chatId": chat_id,
            "senderId": sender_id,
            "isGroup": False,  # Phase 3 = DM only
            "body": body,
        }
        if not self._should_process_message(gating_data):
            return None

        # context.id is set when the user replied to one of our messages.
        context = raw_message.get("context") or {}
        reply_to_id = str(context.get("id") or "").strip() or None

        source = self.build_source(
            chat_id=chat_id,
            chat_name=sender_name or chat_id,
            chat_type="dm",
            user_id=sender_id,
            user_name=sender_name or None,
        )

        # Cloud API timestamps are unix seconds (string). MessageEvent
        # doesn't enforce a type but downstream code formats with it.
        return MessageEvent(
            text=body,
            message_type=message_type,
            source=source,
            raw_message=raw_message,
            message_id=str(raw_message.get("id") or "") or None,
            reply_to_message_id=reply_to_id,
        )
