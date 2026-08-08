#!/usr/bin/env python3
"""Socket Mode listener and pipeline wiring.

Deliberately NOT part of the Hermes gateway: a separate process has an
independent failure domain, deploys without a gateway restart, and is
structurally incapable of creating an agent session.

Socket Mode does not replay missed events, so every start runs a catch-up poll
of the allowlisted channels from the stored watermark.
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/Users/ctao/.hermes/scripts")

from slack_read import ENV_PATH, parse_env, slack_call  # noqa: E402

from boris_intake import audit  # noqa: E402
from boris_intake.config import Config, load_config  # noqa: E402
from boris_intake.dispatch import active_lane_count, create_lane as _create_lane  # noqa: E402
from boris_intake.gate import GateDeps, evaluate  # noqa: E402
from boris_intake.github import fetch_pr  # noqa: E402
from boris_intake.identity import IdentityResolver  # noqa: E402
from boris_intake.models import NormalizedEvent  # noqa: E402
from boris_intake.selftest import run_selftest  # noqa: E402
from boris_intake.slack_io import SlackIO  # noqa: E402
from boris_intake.store import Store  # noqa: E402

STATE_DIR = Path("/Users/ctao/.hermes/state")
DB_PATH = STATE_DIR / "boris_intake.db"
AUDIT_PATH = STATE_DIR / "boris_intake_audit.jsonl"
HEARTBEAT = STATE_DIR / "boris_intake_heartbeat"
WATERMARK_PREFIX = "boris_intake_watermark_"


def normalize(raw: dict) -> NormalizedEvent | None:
    channel = raw.get("channel")
    ts = raw.get("ts")
    if not channel or not ts:
        return None
    return NormalizedEvent(
        channel_id=str(channel),
        channel_type=str(raw.get("channel_type") or "channel"),
        user_id=str(raw["user"]) if raw.get("user") else None,
        ts=str(ts),
        thread_ts=str(raw["thread_ts"]) if raw.get("thread_ts") else None,
        text=str(raw.get("text") or ""),
        is_bot=bool(raw.get("bot_id")),
        subtype=str(raw["subtype"]) if raw.get("subtype") else None,
    )


class Pipeline:
    def __init__(self, cfg: Config, deps: GateDeps, store: Store, slack: SlackIO,
                 audit_path: Path, create_lane=_create_lane):
        self.cfg = cfg
        self.deps = deps
        self.store = store
        self.slack = slack
        self.audit_path = Path(audit_path)
        self._create_lane = create_lane

    def handle(self, event: NormalizedEvent | None) -> None:
        if event is None:
            return
        if self.store.seen_event(event.channel_id, event.ts):
            return
        self.store.record_event(event.channel_id, event.ts)

        result = evaluate(event, self.cfg, self.deps)
        live = self.cfg.mode == "live"
        base = {
            "mode": self.cfg.mode,
            "channel": event.channel_id,
            "ts": event.ts,
            "user": event.user_id,
            "text_excerpt": event.text[:400],
        }

        if not result.accepted:
            audit.write(self.audit_path, {
                **base, "decision": "reject",
                "reasons": [r.reason.value for r in result.rejections],
                "details": [r.detail for r in result.rejections],
            })
            self._maybe_reply_rejections(event, result)
            return

        lanes = []
        for cmd in result.commands:
            lane_id = None
            if live:
                try:
                    lane_id = self._create_lane(cmd)
                    self.store.record_lane(cmd.idempotency_key, lane_id)
                    self.store.record_request(event.user_id, self.deps.now())
                    self.slack.add_reviewing_reaction(cmd.channel_id, cmd.message_ts)
                    self.slack.reply_ack(cmd)
                except Exception as exc:
                    audit.write(self.audit_path, {
                        **base, "decision": "dispatch_error",
                        "pr": cmd.slug, "error": str(exc)[:300]})
                    continue
            lanes.append({"pr": cmd.slug, "head_sha": cmd.head_sha, "lane": lane_id})

        audit.write(self.audit_path, {
            **base, "decision": "accept", "lanes": lanes,
            "reasons": [r.reason.value for r in result.rejections],
        })
        self._maybe_reply_rejections(event, result)

    def _maybe_reply_rejections(self, event, result) -> None:
        loud = [r for r in result.rejections if not r.silent]
        if not loud or not event.user_id:
            return
        if not self.store.should_reply_rejection(
                event.user_id, self.deps.now(),
                self.cfg.limits.reject_reply_cooldown_seconds):
            return
        self.slack.reply_rejection(event.channel_id, event.ts, loud[0])


def _watermark_path(channel: str) -> Path:
    return STATE_DIR / f"{WATERMARK_PREFIX}{channel}.txt"


def catch_up(cfg: Config, token: str, pipeline: Pipeline) -> None:
    """Replay messages posted while the socket was down. Socket Mode never does."""
    for channel in cfg.channels:
        wm_file = _watermark_path(channel)
        try:
            oldest = wm_file.read_text().strip()
        except OSError:
            wm_file.parent.mkdir(parents=True, exist_ok=True)
            wm_file.write_text(f"{time.time():.6f}")
            continue
        resp = slack_call(token, "conversations.history", channel=channel,
                          oldest=oldest, limit=200)
        if not resp.get("ok"):
            continue
        messages = resp.get("messages") or []
        for raw in sorted(messages, key=lambda m: m.get("ts", "0")):
            raw.setdefault("channel", channel)
            pipeline.handle(normalize(raw))
        if messages:
            wm_file.write_text(max(m.get("ts", "0") for m in messages))


def build_pipeline(cfg: Config, token: str) -> Pipeline:
    store = Store(DB_PATH)
    resolver = IdentityResolver(slack_call, token)

    def rate_verdict(uid: str) -> str:
        # Refresh the concurrency figure from the board on every decision, so
        # the global cap reflects lanes that finished or were completed by hand.
        store.set_active_lane_count(active_lane_count())
        return store.rate_verdict(uid, cfg.limits, time.time())

    deps = GateDeps(
        identity=resolver.resolve,
        pr_info=fetch_pr,
        lane_for=store.lane_for,
        rate_verdict=rate_verdict,
        now=time.time,
    )
    return Pipeline(cfg, deps, store,
                    SlackIO(slack_call, token, dry_run=cfg.mode != "live"),
                    AUDIT_PATH)


def main() -> int:
    import asyncio
    import signal

    cfg = load_config()
    env = parse_env(ENV_PATH)
    token = env.get("TICKET_HUB_SLACK_BOT_TOKEN", "")
    app_token = env.get("TICKET_HUB_SLACK_APP_TOKEN", "")
    if not token or not app_token:
        sys.stderr.write("boris_intake: TICKET_HUB_SLACK_BOT_TOKEN / "
                         "TICKET_HUB_SLACK_APP_TOKEN missing from .env\n")
        return 2

    if cfg.mode == "live":
        failures = run_selftest(cfg)
        if failures:
            sys.stderr.write("boris_intake: self-test FAILED, dropping to shadow:\n  "
                             + "\n  ".join(failures) + "\n")
            cfg = replace(cfg, mode="shadow")

    pipeline = build_pipeline(cfg, token)
    catch_up(cfg, token, pipeline)

    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient

    async def on_request(cli: SocketModeClient, req: SocketModeRequest):
        await cli.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id))
        HEARTBEAT.write_text(f"{time.time():.0f}")
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") not in {"message", "app_mention"}:
            return
        try:
            pipeline.handle(normalize(event))
        except Exception as exc:
            sys.stderr.write(f"boris_intake: handler error: {exc}\n")
            sys.stderr.flush()

    async def run() -> None:
        # SocketModeClient (aiohttp backend) builds an aiohttp.ClientSession in
        # its constructor, which requires a RUNNING event loop — so it must be
        # created in here, not at module or main() scope.
        client = SocketModeClient(app_token=app_token,
                                  web_client=AsyncWebClient(token=token))
        client.socket_mode_request_listeners.append(on_request)
        await client.connect()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        sys.stderr.write(f"boris_intake: connected, mode={cfg.mode}, "
                         f"channels={list(cfg.channels)}\n")
        sys.stderr.flush()
        HEARTBEAT.write_text(f"{time.time():.0f}")
        await stop.wait()
        await client.close()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
