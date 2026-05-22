"""Tests for the WhatsApp Cloud API adapter (Phase 2).

Covers the outbound Graph API send path and the inbound verify-token
handshake. The webhook POST path is currently a stub (Phase 3 will add
signature verification + dispatch); we just confirm it accepts a body
and returns 200 here.

All tests are fixture-driven — no live network. httpx is patched so the
adapter never reaches graph.facebook.com, and the aiohttp server is
exercised with synthetic ``Request`` objects.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(**overrides):
    """Build a WhatsAppCloudAdapter with test attributes (bypass __init__).

    Mirrors the pattern in tests/gateway/test_whatsapp_*.py.
    """
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter

    adapter = WhatsAppCloudAdapter.__new__(WhatsAppCloudAdapter)
    adapter.platform = Platform.WHATSAPP_CLOUD
    adapter.config = MagicMock()
    adapter.config.extra = {}

    # Cloud-API-specific attributes
    adapter._phone_number_id = overrides.pop("phone_number_id", "1234567890")
    adapter._access_token = overrides.pop("access_token", "test-token")
    adapter._app_id = overrides.pop("app_id", "")
    adapter._app_secret = overrides.pop("app_secret", "")
    adapter._waba_id = overrides.pop("waba_id", "")
    adapter._verify_token = overrides.pop("verify_token", "")
    adapter._webhook_host = "127.0.0.1"
    adapter._webhook_port = 8090
    adapter._webhook_path = "/whatsapp/webhook"
    adapter._health_path = "/health"
    adapter._api_version = overrides.pop("api_version", "v20.0")
    adapter._runner = None
    adapter._http_client = None

    # Behavior-mixin contract
    adapter._reply_prefix = None
    adapter._dm_policy = "open"
    adapter._allow_from = set()
    adapter._group_policy = "open"
    adapter._group_allow_from = set()
    adapter._mention_patterns = []

    # Webhook dispatch state (Phase 3)
    from collections import OrderedDict
    adapter._seen_wamids = OrderedDict()
    adapter._duplicate_count = 0
    adapter._accepted_count = 0
    adapter._rejected_signature_count = 0

    # Phase 4 state — one-shot warnings.
    adapter._warned_no_ffmpeg = False

    # BasePlatformAdapter contract — minimum to keep send/lifecycle happy
    adapter._running = True
    adapter._message_handler = None
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._background_tasks = set()
    adapter._auto_tts_disabled_chats = set()

    # Apply any leftover overrides directly
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


def _mock_httpx_response(status_code: int, json_body: dict):
    """Build an httpx-Response-like mock the adapter's ``send`` will accept."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body)
    resp.text = json.dumps(json_body)
    return resp


# ---------------------------------------------------------------------------
# Outbound send via Graph API
# ---------------------------------------------------------------------------

class TestSendText:
    """Outbound text-message path."""

    @pytest.mark.asyncio
    async def test_send_builds_correct_url(self):
        adapter = _make_adapter(phone_number_id="9999", api_version="v20.0")
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.abc"}]}
            )
        )

        await adapter.send("15551234567", "hello")

        called_url = adapter._http_client.post.call_args.args[0]
        assert called_url == "https://graph.facebook.com/v20.0/9999/messages"

    @pytest.mark.asyncio
    async def test_send_includes_bearer_auth(self):
        adapter = _make_adapter(access_token="my-secret-token")
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.abc"}]}
            )
        )

        await adapter.send("15551234567", "hi")

        headers = adapter._http_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-secret-token"
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_send_payload_shape(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.abc"}]}
            )
        )

        await adapter.send("15551234567", "hello world")

        payload = adapter._http_client.post.call_args.kwargs["json"]
        assert payload["messaging_product"] == "whatsapp"
        assert payload["recipient_type"] == "individual"
        assert payload["to"] == "15551234567"
        assert payload["type"] == "text"
        assert payload["text"]["body"] == "hello world"
        assert payload["text"]["preview_url"] is True

    @pytest.mark.asyncio
    async def test_send_returns_wamid(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.HBgL...="}]}
            )
        )

        result = await adapter.send("15551234567", "hi")

        assert result.success is True
        assert result.message_id == "wamid.HBgL...="

    @pytest.mark.asyncio
    async def test_send_applies_markdown_conversion(self):
        """Mixin's format_message should run before send."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.x"}]}
            )
        )

        await adapter.send("15551234567", "**bold** text")

        payload = adapter._http_client.post.call_args.kwargs["json"]
        assert payload["text"]["body"] == "*bold* text"

    @pytest.mark.asyncio
    async def test_send_reply_to_attaches_context_first_chunk_only(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.x"}]}
            )
        )

        await adapter.send("15551234567", "short reply", reply_to="wamid.original")

        payload = adapter._http_client.post.call_args.kwargs["json"]
        assert payload["context"] == {"message_id": "wamid.original"}

    @pytest.mark.asyncio
    async def test_send_long_message_chunked(self):
        """Messages over the chunk limit are split into multiple POSTs."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                200, {"messages": [{"id": "wamid.x"}]}
            )
        )

        # MAX_MESSAGE_LENGTH = 4096 from the mixin. 8500 chars forces 2+ chunks.
        long_text = "a" * 8500
        await adapter.send("15551234567", long_text)

        # At least 2 POST calls
        assert adapter._http_client.post.call_count >= 2
        # Second call should NOT have context (only first chunk gets reply_to)
        first_call = adapter._http_client.post.call_args_list[0]
        second_call = adapter._http_client.post.call_args_list[1]
        # No reply_to passed → no context anywhere, but verify structure anyway
        assert "context" not in second_call.kwargs["json"]

    @pytest.mark.asyncio
    async def test_send_graph_error_returns_failure(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(
            return_value=_mock_httpx_response(
                400,
                {
                    "error": {
                        "message": "Invalid parameter",
                        "type": "OAuthException",
                        "code": 100,
                        "fbtrace_id": "abc",
                    }
                },
            )
        )

        result = await adapter.send("15551234567", "hi")

        assert result.success is False
        assert "graph error 100" in result.error
        assert "Invalid parameter" in result.error

    @pytest.mark.asyncio
    async def test_send_empty_content_no_request(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock()

        result = await adapter.send("15551234567", "")
        assert result.success is True
        assert result.message_id is None
        adapter._http_client.post.assert_not_called()

        result = await adapter.send("15551234567", "   \n  ")
        assert result.success is True
        adapter._http_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_not_connected_returns_failure(self):
        adapter = _make_adapter()
        adapter._http_client = None

        result = await adapter.send("15551234567", "hi")
        assert result.success is False
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_send_network_exception_returns_failure(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=RuntimeError("boom"))

        result = await adapter.send("15551234567", "hi")
        assert result.success is False
        assert "boom" in result.error


# ---------------------------------------------------------------------------
# Inbound webhook verify (GET) handshake
# ---------------------------------------------------------------------------

def _verify_request(query: dict):
    """Build a minimal aiohttp.web.Request stub for verify tests."""
    request = MagicMock()
    request.query = query
    return request


class TestWebhookVerify:
    """GET <webhook>?hub.mode=...&hub.verify_token=...&hub.challenge=..."""

    @pytest.mark.asyncio
    async def test_verify_echoes_challenge_on_match(self):
        adapter = _make_adapter(verify_token="shared-secret-123")
        request = _verify_request({
            "hub.mode": "subscribe",
            "hub.verify_token": "shared-secret-123",
            "hub.challenge": "abc-12345",
        })

        response = await adapter._handle_verify(request)

        assert response.status == 200
        assert response.text == "abc-12345"
        assert response.content_type == "text/plain"

    @pytest.mark.asyncio
    async def test_verify_rejects_token_mismatch(self):
        adapter = _make_adapter(verify_token="shared-secret-123")
        request = _verify_request({
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "abc-12345",
        })

        response = await adapter._handle_verify(request)

        assert response.status == 403

    @pytest.mark.asyncio
    async def test_verify_rejects_wrong_mode(self):
        adapter = _make_adapter(verify_token="shared-secret-123")
        request = _verify_request({
            "hub.mode": "unsubscribe",
            "hub.verify_token": "shared-secret-123",
            "hub.challenge": "abc-12345",
        })

        response = await adapter._handle_verify(request)

        assert response.status == 400

    @pytest.mark.asyncio
    async def test_verify_rejects_missing_challenge(self):
        adapter = _make_adapter(verify_token="shared-secret-123")
        request = _verify_request({
            "hub.mode": "subscribe",
            "hub.verify_token": "shared-secret-123",
        })

        response = await adapter._handle_verify(request)

        assert response.status == 400

    @pytest.mark.asyncio
    async def test_verify_refuses_when_token_unconfigured(self):
        """An empty verify_token must NOT match an empty incoming token —
        otherwise an attacker who guesses the misconfiguration could
        subscribe their own webhook URL.
        """
        adapter = _make_adapter(verify_token="")
        request = _verify_request({
            "hub.mode": "subscribe",
            "hub.verify_token": "",
            "hub.challenge": "abc",
        })

        response = await adapter._handle_verify(request)

        assert response.status == 503  # service refuses to perform handshake


# ---------------------------------------------------------------------------
# Inbound webhook POST — signature verification + dispatch (Phase 3)
# ---------------------------------------------------------------------------

import hashlib
import hmac as _hmac_lib


def _sign(secret: str, body: bytes) -> str:
    """Compute the X-Hub-Signature-256 header value Meta would send."""
    digest = _hmac_lib.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def _post_request(body: bytes, headers: dict | None = None):
    """Build a minimal aiohttp.web.Request stub for POST tests."""
    request = MagicMock()
    request.read = AsyncMock(return_value=body)
    request.headers = headers or {}
    return request


# A realistic Meta inbound text-message payload, modelled on the
# get-started docs sample.
_SAMPLE_INBOUND_TEXT_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "215589313241560883",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15551797781",
                            "phone_number_id": "7794189252778687",
                        },
                        "contacts": [
                            {
                                "profile": {"name": "Jessica Laverdetman"},
                                "wa_id": "13557825698",
                            }
                        ],
                        "messages": [
                            {
                                "from": "13557825698",
                                "id": "wamid.HBgLMTM1NTc4MjU2OTgVAGHAYWYET688aASGNTI1QzZFQjhEMDk2QQA=",
                                "timestamp": "1758254144",
                                "text": {"body": "Hi!"},
                                "type": "text",
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


class TestWebhookSignature:
    """X-Hub-Signature-256 HMAC verification."""

    @pytest.mark.asyncio
    async def test_valid_signature_accepted(self):
        adapter = _make_adapter(app_secret="signing-key-123")
        # Patch the dispatcher to a no-op so we don't depend on
        # MessageEvent construction here (covered separately).
        adapter._dispatch_payload = AsyncMock()
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        request = _post_request(body, {"X-Hub-Signature-256": _sign("signing-key-123", body)})

        response = await adapter._handle_webhook(request)

        assert response.status == 200
        adapter._dispatch_payload.assert_called_once()

    @pytest.mark.asyncio
    async def test_tampered_body_rejected(self):
        adapter = _make_adapter(app_secret="signing-key-123")
        adapter._dispatch_payload = AsyncMock()
        original = b'{"object":"whatsapp_business_account"}'
        tampered = b'{"object":"evil_payload"}'
        sig_for_original = _sign("signing-key-123", original)
        request = _post_request(tampered, {"X-Hub-Signature-256": sig_for_original})

        response = await adapter._handle_webhook(request)

        assert response.status == 401
        adapter._dispatch_payload.assert_not_called()
        assert adapter._rejected_signature_count == 1

    @pytest.mark.asyncio
    async def test_missing_signature_header_rejected(self):
        adapter = _make_adapter(app_secret="signing-key-123")
        adapter._dispatch_payload = AsyncMock()
        body = b'{"object":"whatsapp_business_account"}'
        request = _post_request(body, {})

        response = await adapter._handle_webhook(request)

        assert response.status == 401
        adapter._dispatch_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_signature_format_rejected(self):
        adapter = _make_adapter(app_secret="signing-key-123")
        adapter._dispatch_payload = AsyncMock()
        body = b"{}"
        # Missing the required ``sha256=`` prefix
        request = _post_request(body, {"X-Hub-Signature-256": "deadbeef"})

        response = await adapter._handle_webhook(request)
        assert response.status == 401

    @pytest.mark.asyncio
    async def test_unconfigured_app_secret_refuses_503(self):
        """Don't quietly accept webhooks when we can't authenticate them."""
        adapter = _make_adapter(app_secret="")
        adapter._dispatch_payload = AsyncMock()
        body = b'{"object":"whatsapp_business_account"}'
        request = _post_request(body, {"X-Hub-Signature-256": "sha256=deadbeef"})

        response = await adapter._handle_webhook(request)

        assert response.status == 503
        adapter._dispatch_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_signature_uses_constant_time_compare(self):
        """Smoke-test: equivalent signatures with case differences both pass."""
        adapter = _make_adapter(app_secret="key")
        adapter._dispatch_payload = AsyncMock()
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        proper = _sign("key", body)
        # Capitalize hex — hmac.compare_digest is case-sensitive but our
        # implementation lowercases both sides so case differences in the
        # incoming header don't accidentally fail valid signatures.
        upper = proper.upper().replace("SHA256=", "sha256=")
        request = _post_request(body, {"X-Hub-Signature-256": upper})

        response = await adapter._handle_webhook(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_oversize_body_rejected_before_signature(self):
        """3MB cap per Meta — refuse without computing HMAC over giant junk."""
        adapter = _make_adapter(app_secret="key")
        adapter._dispatch_payload = AsyncMock()
        body = b"x" * (4 * 1024 * 1024)
        request = _post_request(body, {"X-Hub-Signature-256": "sha256=ignored"})

        response = await adapter._handle_webhook(request)
        assert response.status == 413
        adapter._dispatch_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_body_rejected(self):
        adapter = _make_adapter(app_secret="key")
        request = MagicMock()
        request.read = AsyncMock(side_effect=RuntimeError("read failed"))
        request.headers = {}

        response = await adapter._handle_webhook(request)
        assert response.status == 400


class TestWebhookReplay:
    """wamid dedup — Meta retries failed deliveries up to 7 days."""

    @pytest.mark.asyncio
    async def test_duplicate_wamid_not_redispatched(self):
        adapter = _make_adapter(app_secret="key")
        adapter.handle_message = AsyncMock()
        body = json.dumps(_SAMPLE_INBOUND_TEXT_PAYLOAD).encode("utf-8")
        sig = _sign("key", body)

        # First delivery
        await adapter._handle_webhook(_post_request(body, {"X-Hub-Signature-256": sig}))
        # Second delivery (same payload, valid signature, same wamid)
        await adapter._handle_webhook(_post_request(body, {"X-Hub-Signature-256": sig}))

        # handle_message fires once, even though the webhook fired twice
        assert adapter.handle_message.call_count == 1
        assert adapter._duplicate_count == 1
        assert adapter._accepted_count == 1

    def test_dedup_cache_evicts_oldest(self):
        from gateway.platforms.whatsapp_cloud import WAMID_DEDUP_CACHE_SIZE
        adapter = _make_adapter()
        # Fill the cache plus 5 extra
        for i in range(WAMID_DEDUP_CACHE_SIZE + 5):
            assert adapter._dedup_wamid(f"wamid_{i}") is True
        assert len(adapter._seen_wamids) == WAMID_DEDUP_CACHE_SIZE
        # The first 5 should have been evicted
        assert "wamid_0" not in adapter._seen_wamids
        assert "wamid_4" not in adapter._seen_wamids
        assert "wamid_5" in adapter._seen_wamids
        assert f"wamid_{WAMID_DEDUP_CACHE_SIZE + 4}" in adapter._seen_wamids

    def test_dedup_no_wamid_lets_through(self):
        """Defensive — Meta should always populate ``id``, but we don't
        want to silently drop messages if it's missing."""
        adapter = _make_adapter()
        assert adapter._dedup_wamid("") is True
        assert adapter._dedup_wamid("") is True  # both pass


class TestWebhookDispatch:
    """End-to-end dispatch from a verified payload to handle_message."""

    @pytest.mark.asyncio
    async def test_text_message_dispatched_with_event_shape(self):
        adapter = _make_adapter(app_secret="key")
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture
        body = json.dumps(_SAMPLE_INBOUND_TEXT_PAYLOAD).encode("utf-8")
        sig = _sign("key", body)
        request = _post_request(body, {"X-Hub-Signature-256": sig})

        response = await adapter._handle_webhook(request)

        assert response.status == 200
        assert len(captured) == 1
        event = captured[0]
        assert event.text == "Hi!"
        assert event.message_id == (
            "wamid.HBgLMTM1NTc4MjU2OTgVAGHAYWYET688aASGNTI1QzZFQjhEMDk2QQA="
        )
        assert event.source.platform == Platform.WHATSAPP_CLOUD
        assert event.source.chat_id == "13557825698"
        assert event.source.user_name == "Jessica Laverdetman"
        assert event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_dispatch_filters_via_mixin_gating(self):
        adapter = _make_adapter(app_secret="key")
        adapter._dm_policy = "disabled"  # block all DMs
        adapter.handle_message = AsyncMock()
        body = json.dumps(_SAMPLE_INBOUND_TEXT_PAYLOAD).encode("utf-8")
        sig = _sign("key", body)

        response = await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )

        assert response.status == 200
        adapter.handle_message.assert_not_called()
        # Gated messages don't increment the accepted counter
        assert adapter._accepted_count == 0

    @pytest.mark.asyncio
    async def test_dispatch_handler_exception_does_not_crash(self):
        """If the agent dispatch raises, we still return 200 to Meta so
        retries don't multiply the bug into a 7-day storm."""
        adapter = _make_adapter(app_secret="key")
        adapter.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        body = json.dumps(_SAMPLE_INBOUND_TEXT_PAYLOAD).encode("utf-8")
        sig = _sign("key", body)

        response = await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_dispatch_ignores_non_message_field(self):
        """``field: 'statuses'`` etc. should not produce MessageEvents."""
        adapter = _make_adapter(app_secret="key")
        adapter.handle_message = AsyncMock()
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "x",
                    "changes": [
                        {
                            "field": "account_alerts",
                            "value": {"some": "alert"},
                        }
                    ],
                }
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        sig = _sign("key", body)

        response = await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )
        assert response.status == 200
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_ignores_non_waba_object(self):
        adapter = _make_adapter(app_secret="key")
        adapter.handle_message = AsyncMock()
        payload = {"object": "page", "entry": []}
        body = json.dumps(payload).encode("utf-8")
        sig = _sign("key", body)

        response = await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )
        assert response.status == 200
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_handles_button_reply(self):
        adapter = _make_adapter(app_secret="key")
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "x",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "1"},
                                "contacts": [
                                    {"profile": {"name": "U"}, "wa_id": "1555"}
                                ],
                                "messages": [
                                    {
                                        "from": "1555",
                                        "id": "wamid.button1",
                                        "timestamp": "0",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "button_reply",
                                            "button_reply": {
                                                "id": "yes",
                                                "title": "Yes please",
                                            },
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        sig = _sign("key", body)

        response = await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )
        assert response.status == 200
        assert len(captured) == 1
        assert captured[0].text == "Yes please"

    @pytest.mark.asyncio
    async def test_dispatch_propagates_reply_to(self):
        """``context.id`` on inbound = user replied to one of our messages."""
        adapter = _make_adapter(app_secret="key")
        captured = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture

        payload_with_ctx = json.loads(
            json.dumps(_SAMPLE_INBOUND_TEXT_PAYLOAD)
        )  # deep copy
        msg = payload_with_ctx["entry"][0]["changes"][0]["value"]["messages"][0]
        msg["context"] = {"id": "wamid.our_outbound", "from": "15551797781"}
        body = json.dumps(payload_with_ctx).encode("utf-8")
        sig = _sign("key", body)

        await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )
        assert len(captured) == 1
        assert captured[0].reply_to_message_id == "wamid.our_outbound"

    @pytest.mark.asyncio
    async def test_invalid_json_after_signature_returns_400(self):
        """Pathological case: signature passes but body isn't JSON."""
        adapter = _make_adapter(app_secret="key")
        body = b"not-json"
        sig = _sign("key", body)
        response = await adapter._handle_webhook(
            _post_request(body, {"X-Hub-Signature-256": sig})
        )
        assert response.status == 400


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_reports_config_visibility(self):
        adapter = _make_adapter(
            phone_number_id="555",
            verify_token="secret",
            app_secret="signing-key",
        )
        request = MagicMock()

        response = await adapter._handle_health(request)

        # web.json_response stores the dict on .text as JSON
        body = json.loads(response.text)
        assert body["status"] == "ok"
        assert body["platform"] == "whatsapp_cloud"
        assert body["phone_number_id"] == "555"
        assert body["verify_token_configured"] is True
        assert body["app_secret_configured"] is True
        assert body["accepted"] == 0
        assert body["duplicates"] == 0
        assert body["rejected_signature"] == 0
        # ffmpeg_present is True/False depending on the test host;
        # just verify the key is exposed.
        assert "ffmpeg_present" in body
        assert isinstance(body["ffmpeg_present"], bool)

    @pytest.mark.asyncio
    async def test_health_flags_missing_secrets(self):
        adapter = _make_adapter(verify_token="", app_secret="")
        request = MagicMock()

        response = await adapter._handle_health(request)
        body = json.loads(response.text)
        assert body["verify_token_configured"] is False
        assert body["app_secret_configured"] is False


# ---------------------------------------------------------------------------
# Mixin contract — gating still works on the cloud adapter
# ---------------------------------------------------------------------------

class TestMixinInherited:
    """Sanity-check: the Cloud adapter inherits the same gating behavior
    as the Baileys adapter via WhatsAppBehaviorMixin.
    """

    def test_format_message_converts_markdown(self):
        adapter = _make_adapter()
        assert adapter.format_message("**bold**") == "*bold*"
        assert adapter.format_message("# Title") == "*Title*"

    def test_should_process_message_dm_open(self):
        adapter = _make_adapter()
        adapter._dm_policy = "open"
        assert adapter._should_process_message({
            "chatId": "15551234567@c.us",
            "senderId": "15551234567@c.us",
            "isGroup": False,
            "body": "hi",
        }) is True

    def test_should_process_message_dm_disabled(self):
        adapter = _make_adapter()
        adapter._dm_policy = "disabled"
        assert adapter._should_process_message({
            "chatId": "15551234567@c.us",
            "senderId": "15551234567@c.us",
            "isGroup": False,
            "body": "hi",
        }) is False

    def test_broadcast_chats_filtered(self):
        adapter = _make_adapter()
        assert adapter._should_process_message({
            "chatId": "status@broadcast",
            "isGroup": False,
            "body": "x",
        }) is False


# ---------------------------------------------------------------------------
# Outbound media — link mode + upload mode (Phase 4)
# ---------------------------------------------------------------------------

import os as _os
import tempfile as _tempfile
from unittest.mock import patch as _patch


def _mock_upload_response(media_id: str = "media_abc123"):
    """Graph /media POST response shape."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"id": media_id})
    resp.text = json.dumps({"id": media_id})
    return resp


def _mock_message_response(wamid: str = "wamid.outbound1"):
    """Graph /messages POST response shape."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"messages": [{"id": wamid}]})
    resp.text = json.dumps({"messages": [{"id": wamid}]})
    return resp


def _tmpfile(suffix: str = ".jpg", content: bytes = b"\xff\xd8\xff\xe0") -> str:
    """Write a small temp file and return its path. Caller cleans up."""
    fd, path = _tempfile.mkstemp(suffix=suffix)
    with _os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return path


class TestSendImage:
    """send_image — public URL takes the link path; local file uploads first."""

    @pytest.mark.asyncio
    async def test_send_image_link_mode_skips_upload(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=_mock_message_response())

        result = await adapter.send_image("15551234567", "https://cdn.example.com/cat.jpg")

        assert result.success is True
        # Exactly one POST — straight to /messages, no /media upload
        assert adapter._http_client.post.call_count == 1
        url = adapter._http_client.post.call_args.args[0]
        assert url.endswith("/messages")
        payload = adapter._http_client.post.call_args.kwargs["json"]
        assert payload["type"] == "image"
        assert payload["image"] == {"link": "https://cdn.example.com/cat.jpg"}

    @pytest.mark.asyncio
    async def test_send_image_local_path_uploads_then_sends(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=[
            _mock_upload_response("media_uploaded_id"),
            _mock_message_response(),
        ])
        path = _tmpfile(".jpg")
        try:
            result = await adapter.send_image_file("15551234567", path)
            assert result.success is True
            assert adapter._http_client.post.call_count == 2

            upload_url = adapter._http_client.post.call_args_list[0].args[0]
            send_url = adapter._http_client.post.call_args_list[1].args[0]
            assert upload_url.endswith("/media")
            assert send_url.endswith("/messages")

            send_payload = adapter._http_client.post.call_args_list[1].kwargs["json"]
            assert send_payload["image"] == {"id": "media_uploaded_id"}
        finally:
            _os.unlink(path)

    @pytest.mark.asyncio
    async def test_send_image_caption_attached(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=_mock_message_response())

        await adapter.send_image(
            "15551234567", "https://cdn.example.com/cat.jpg", caption="cute cat"
        )
        payload = adapter._http_client.post.call_args.kwargs["json"]
        assert payload["image"]["caption"] == "cute cat"

    @pytest.mark.asyncio
    async def test_send_image_oversize_rejected_locally(self):
        """Don't round-trip to Graph just to be told the file's too big."""
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock()
        # 6MB > 5MB image cap
        path = _tmpfile(".jpg", content=b"x" * (6 * 1024 * 1024))
        try:
            result = await adapter.send_image_file("15551234567", path)
            assert result.success is False
            assert "5242880" in result.error or "cap is" in result.error
            # Never even POSTed
            adapter._http_client.post.assert_not_called()
        finally:
            _os.unlink(path)

    @pytest.mark.asyncio
    async def test_send_image_missing_local_file_returns_failure(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock()

        result = await adapter.send_image_file(
            "15551234567", "/nonexistent/path/foo.jpg"
        )
        assert result.success is False
        assert "File not found" in result.error
        adapter._http_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_upload_failure_returns_failure(self):
        adapter = _make_adapter()
        # First call (upload) fails with a Graph error
        upload_fail = MagicMock()
        upload_fail.status_code = 400
        upload_fail.json = MagicMock(return_value={
            "error": {"code": 100, "message": "Bad media"}
        })
        upload_fail.text = '{"error":{"code":100,"message":"Bad media"}}'
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=upload_fail)

        path = _tmpfile(".jpg")
        try:
            result = await adapter.send_image_file("15551234567", path)
            assert result.success is False
            assert "graph error 100" in result.error
            # Only the upload call — never reached /messages
            assert adapter._http_client.post.call_count == 1
        finally:
            _os.unlink(path)


class TestSendVideo:
    @pytest.mark.asyncio
    async def test_send_video_link_mode(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=_mock_message_response())

        await adapter.send_video("15551234567", "https://cdn.example.com/v.mp4", caption="clip")
        payload = adapter._http_client.post.call_args.kwargs["json"]
        assert payload["type"] == "video"
        assert payload["video"]["link"] == "https://cdn.example.com/v.mp4"
        assert payload["video"]["caption"] == "clip"


class TestSendMethodsAcceptBaseClassKwargs:
    """Regression: every send_* method must absorb ``metadata=`` (and any
    other future kwargs) without raising TypeError.

    base.BasePlatformAdapter.send_multiple_images and friends pass
    ``metadata=...`` to send_image; if a subclass forgets ``**kwargs``,
    the agent crashes mid-send_multiple_images instead of just sending
    the image. This test guards against that for every Cloud send_*
    surface.
    """

    @pytest.mark.asyncio
    async def test_send_image_accepts_metadata(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=_mock_message_response())
        # Should not raise TypeError.
        result = await adapter.send_image(
            "15551234567", "https://cdn.example.com/x.jpg",
            metadata={"trace_id": "abc"},
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_image_file_accepts_metadata(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=[
            _mock_upload_response(),
            _mock_message_response(),
        ])
        path = _tmpfile(".jpg")
        try:
            result = await adapter.send_image_file(
                "15551234567", path, metadata={"x": 1},
            )
            assert result.success is True
        finally:
            _os.unlink(path)

    @pytest.mark.asyncio
    async def test_send_video_accepts_metadata(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=_mock_message_response())
        result = await adapter.send_video(
            "15551234567", "https://cdn.example.com/v.mp4",
            metadata={"x": 1},
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_voice_accepts_metadata(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(return_value=_mock_message_response())
        result = await adapter.send_voice(
            "15551234567", "https://cdn.example.com/a.ogg",
            metadata={"x": 1},
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_document_accepts_metadata(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=[
            _mock_upload_response(),
            _mock_message_response(),
        ])
        path = _tmpfile(".pdf", content=b"%PDF")
        try:
            result = await adapter.send_document(
                "15551234567", path, metadata={"x": 1},
            )
            assert result.success is True
        finally:
            _os.unlink(path)


class TestSendDocument:
    @pytest.mark.asyncio
    async def test_send_document_filename_attached(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=[
            _mock_upload_response("doc_id"),
            _mock_message_response(),
        ])
        path = _tmpfile(".pdf", content=b"%PDF-1.4 ...")
        try:
            await adapter.send_document(
                "15551234567", path, caption="Q3 report",
                file_name="report.pdf",
            )
            send_payload = adapter._http_client.post.call_args_list[1].kwargs["json"]
            assert send_payload["type"] == "document"
            assert send_payload["document"]["id"] == "doc_id"
            assert send_payload["document"]["caption"] == "Q3 report"
            assert send_payload["document"]["filename"] == "report.pdf"
        finally:
            _os.unlink(path)


class TestSendVoice:
    """MP3 voice with ffmpeg present -> opus; without ffmpeg -> MP3 fallback."""

    @pytest.mark.asyncio
    async def test_send_voice_no_ffmpeg_falls_back_to_mp3(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=[
            _mock_upload_response("audio_id"),
            _mock_message_response(),
        ])
        # Simulate ffmpeg absent — adapter._convert_to_opus returns None
        adapter._convert_to_opus = AsyncMock(return_value=None)

        path = _tmpfile(".mp3", content=b"ID3\x04\x00\x00\x00\x00")
        try:
            result = await adapter.send_voice("15551234567", path)
            assert result.success is True
            # Adapter still uploaded + sent the MP3 as audio
            assert adapter._http_client.post.call_count == 2
            send_payload = adapter._http_client.post.call_args_list[1].kwargs["json"]
            assert send_payload["type"] == "audio"
            assert send_payload["audio"]["id"] == "audio_id"
        finally:
            _os.unlink(path)

    @pytest.mark.asyncio
    async def test_send_voice_ffmpeg_present_uses_opus(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        adapter._http_client.post = AsyncMock(side_effect=[
            _mock_upload_response("voice_id"),
            _mock_message_response(),
        ])
        # Pretend ffmpeg conversion succeeded by returning a fake opus path.
        opus_path = _tmpfile(".ogg", content=b"OggS")
        adapter._convert_to_opus = AsyncMock(return_value=opus_path)

        mp3_path = _tmpfile(".mp3", content=b"ID3")
        try:
            result = await adapter.send_voice("15551234567", mp3_path)
            assert result.success is True
            # Conversion was invoked with the original MP3
            uploaded_path = adapter._convert_to_opus.call_args.args[0]
            assert uploaded_path == mp3_path
            send_payload = adapter._http_client.post.call_args_list[1].kwargs["json"]
            assert send_payload["type"] == "audio"
        finally:
            _os.unlink(mp3_path)
            if _os.path.exists(opus_path):
                _os.unlink(opus_path)

    @pytest.mark.asyncio
    async def test_warn_once_no_ffmpeg_actually_only_warns_once(self):
        adapter = _make_adapter()
        adapter._warned_no_ffmpeg = False
        adapter._warn_once_no_ffmpeg()
        assert adapter._warned_no_ffmpeg is True
        # Second call: no-op (we just verify no exception + flag stays True)
        adapter._warn_once_no_ffmpeg()
        assert adapter._warned_no_ffmpeg is True


# ---------------------------------------------------------------------------
# Inbound media — Graph two-step download (Phase 4)
# ---------------------------------------------------------------------------

class TestDownloadMedia:
    """Two-step Graph media download: meta -> temp URL -> bytes."""

    @pytest.mark.asyncio
    async def test_two_step_download_writes_cache_file(self, tmp_path):
        from gateway.platforms import whatsapp_cloud as wac

        adapter = _make_adapter()
        adapter._http_client = MagicMock()

        # Step 1 — metadata returns temp URL + mime
        meta_resp = MagicMock(status_code=200)
        meta_resp.json = MagicMock(return_value={
            "url": "https://lookaside.fbsbx.com/whatsapp/m/...",
            "mime_type": "image/jpeg",
            "sha256": "abc",
            "file_size": 12345,
            "id": "media_xyz",
            "messaging_product": "whatsapp",
        })
        # Step 2 — bytes
        blob_resp = MagicMock(status_code=200, content=b"\xff\xd8\xff\xe0jpegdata")

        adapter._http_client.get = AsyncMock(side_effect=[meta_resp, blob_resp])

        with _patch.object(wac, "_INBOUND_MEDIA_CACHE", tmp_path):
            local_path, mime = await adapter._download_media_to_cache("media_xyz")

        assert mime == "image/jpeg"
        assert local_path is not None
        assert _os.path.exists(local_path)
        assert _os.path.basename(local_path).startswith("media_xyz")
        assert _os.path.basename(local_path).endswith(".jpg")
        with open(local_path, "rb") as fh:
            assert fh.read() == b"\xff\xd8\xff\xe0jpegdata"

    @pytest.mark.asyncio
    async def test_metadata_failure_returns_none(self):
        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        meta_fail = MagicMock(status_code=404)
        meta_fail.json = MagicMock(return_value={"error": {"code": 100}})
        adapter._http_client.get = AsyncMock(return_value=meta_fail)

        local_path, mime = await adapter._download_media_to_cache("missing")
        assert local_path is None and mime is None

    @pytest.mark.asyncio
    async def test_bytes_failure_returns_none(self, tmp_path):
        from gateway.platforms import whatsapp_cloud as wac

        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        meta_resp = MagicMock(status_code=200)
        meta_resp.json = MagicMock(return_value={
            "url": "https://lookaside.fbsbx.com/...",
            "mime_type": "image/jpeg",
        })
        blob_fail = MagicMock(status_code=403, content=b"")
        adapter._http_client.get = AsyncMock(side_effect=[meta_resp, blob_fail])

        with _patch.object(wac, "_INBOUND_MEDIA_CACHE", tmp_path):
            local_path, mime = await adapter._download_media_to_cache("x")
        assert local_path is None

    @pytest.mark.asyncio
    async def test_metadata_includes_auth_header(self):
        adapter = _make_adapter(access_token="bearer-tok")
        adapter._http_client = MagicMock()
        adapter._http_client.get = AsyncMock(return_value=MagicMock(status_code=500))
        await adapter._download_media_to_cache("x")
        headers = adapter._http_client.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer bearer-tok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mime,expected_ext", [
        # Regression for the ".oga vs .ogg" voice-note bug — Python's
        # mimetypes module returns the RFC-correct .oga which downstream
        # STT pipelines reject.
        ("audio/ogg", ".ogg"),
        ("audio/ogg; codecs=opus", ".ogg"),
        ("audio/x-opus+ogg", ".ogg"),
        ("audio/opus", ".ogg"),
        # iOS voice memos arrive as audio/mp4 — must become .m4a, not .mp4.
        ("audio/mp4", ".m4a"),
        ("audio/x-m4a", ".m4a"),
        # JPEG should never land as .jpe (legacy IANA).
        ("image/jpeg", ".jpg"),
    ])
    async def test_extension_overrides_for_real_world_mimes(self, tmp_path, mime, expected_ext):
        from gateway.platforms import whatsapp_cloud as wac

        adapter = _make_adapter()
        adapter._http_client = MagicMock()
        meta_resp = MagicMock(status_code=200)
        meta_resp.json = MagicMock(return_value={
            "url": "https://lookaside.fbsbx.com/test",
            "mime_type": mime,
        })
        blob_resp = MagicMock(status_code=200, content=b"x")
        adapter._http_client.get = AsyncMock(side_effect=[meta_resp, blob_resp])

        with _patch.object(wac, "_INBOUND_MEDIA_CACHE", tmp_path):
            local_path, _ = await adapter._download_media_to_cache("media_x")

        assert local_path is not None
        assert local_path.endswith(expected_ext), (
            f"mime {mime!r} should map to {expected_ext} but got {local_path}"
        )


class TestInboundMediaDispatch:
    """End-to-end: webhook with image_id -> adapter downloads -> MessageEvent.media_urls populated."""

    @pytest.mark.asyncio
    async def test_inbound_image_populates_media_urls(self, tmp_path):
        from gateway.platforms import whatsapp_cloud as wac

        adapter = _make_adapter(app_secret="key")
        captured: list = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture

        # Mock the two-step Graph download
        meta_resp = MagicMock(status_code=200)
        meta_resp.json = MagicMock(return_value={
            "url": "https://lookaside.fbsbx.com/whatsapp/m/abc",
            "mime_type": "image/jpeg",
        })
        blob_resp = MagicMock(status_code=200, content=b"\xff\xd8\xff\xe0fake_jpeg")
        adapter._http_client = MagicMock()
        adapter._http_client.get = AsyncMock(side_effect=[meta_resp, blob_resp])

        # Build an inbound image webhook payload
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "x",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "1"},
                        "contacts": [{"profile": {"name": "U"}, "wa_id": "1555"}],
                        "messages": [{
                            "from": "1555",
                            "id": "wamid.img1",
                            "timestamp": "0",
                            "type": "image",
                            "image": {
                                "id": "media_image_abc",
                                "mime_type": "image/jpeg",
                                "sha256": "...",
                                "caption": "look at this",
                            },
                        }],
                    },
                }],
            }],
        }
        body = json.dumps(payload).encode("utf-8")
        sig = _sign("key", body)

        with _patch.object(wac, "_INBOUND_MEDIA_CACHE", tmp_path):
            response = await adapter._handle_webhook(
                _post_request(body, {"X-Hub-Signature-256": sig})
            )

        assert response.status == 200
        assert len(captured) == 1
        event = captured[0]
        # Caption became the body
        assert event.text == "look at this"
        # Cached file path populated
        assert len(event.media_urls) == 1
        assert _os.path.exists(event.media_urls[0])
        assert event.media_types[0] == "image/jpeg"
        from gateway.platforms.base import MessageType
        assert event.message_type == MessageType.PHOTO

    @pytest.mark.asyncio
    async def test_inbound_text_document_injected_into_body(self, tmp_path):
        """A .txt document should have its content prepended to the body."""
        from gateway.platforms import whatsapp_cloud as wac

        adapter = _make_adapter(app_secret="key")
        captured: list = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture

        text_content = b"hello\nthis is the file\n"
        meta_resp = MagicMock(status_code=200)
        meta_resp.json = MagicMock(return_value={
            "url": "https://lookaside.fbsbx.com/whatsapp/m/doc",
            "mime_type": "text/plain",
        })
        blob_resp = MagicMock(status_code=200, content=text_content)
        adapter._http_client = MagicMock()
        adapter._http_client.get = AsyncMock(side_effect=[meta_resp, blob_resp])

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "x",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "1"},
                        "contacts": [{"profile": {"name": "U"}, "wa_id": "1555"}],
                        "messages": [{
                            "from": "1555",
                            "id": "wamid.doc1",
                            "timestamp": "0",
                            "type": "document",
                            "document": {
                                "id": "media_doc_abc",
                                "mime_type": "text/plain",
                                "filename": "notes.txt",
                            },
                        }],
                    },
                }],
            }],
        }
        body = json.dumps(payload).encode("utf-8")
        sig = _sign("key", body)

        with _patch.object(wac, "_INBOUND_MEDIA_CACHE", tmp_path):
            await adapter._handle_webhook(
                _post_request(body, {"X-Hub-Signature-256": sig})
            )

        assert len(captured) == 1
        event = captured[0]
        assert "hello\nthis is the file" in event.text
        assert "[Content of" in event.text
        # File still available in media_urls for the agent's other tools
        assert len(event.media_urls) == 1

    @pytest.mark.asyncio
    async def test_inbound_image_download_failure_still_dispatches(self, tmp_path):
        """If the binary fetch fails we still want the agent to see the
        message metadata + caption — better than silently dropping."""
        from gateway.platforms import whatsapp_cloud as wac

        adapter = _make_adapter(app_secret="key")
        captured: list = []

        async def _capture(event):
            captured.append(event)

        adapter.handle_message = _capture
        adapter._http_client = MagicMock()
        # Metadata fetch fails
        adapter._http_client.get = AsyncMock(return_value=MagicMock(status_code=500))

        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "x",
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "1"},
                        "contacts": [{"profile": {"name": "U"}, "wa_id": "1555"}],
                        "messages": [{
                            "from": "1555",
                            "id": "wamid.bad_img",
                            "timestamp": "0",
                            "type": "image",
                            "image": {"id": "borked", "mime_type": "image/jpeg"},
                        }],
                    },
                }],
            }],
        }
        body = json.dumps(payload).encode("utf-8")
        sig = _sign("key", body)

        with _patch.object(wac, "_INBOUND_MEDIA_CACHE", tmp_path):
            response = await adapter._handle_webhook(
                _post_request(body, {"X-Hub-Signature-256": sig})
            )

        assert response.status == 200
        assert len(captured) == 1
        # Agent gets the event, just with empty media_urls
        assert captured[0].media_urls == []
