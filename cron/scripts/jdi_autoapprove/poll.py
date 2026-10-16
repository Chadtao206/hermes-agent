#!/usr/bin/env python3
"""One polling pass over the auto-approve channels. Run by cron; exits each tick.

WHY POLLING AND NOT SOCKET MODE — this is the load-bearing design constraint.
boris_intake already holds a Socket Mode connection on the shared app token, and
Slack delivers each payload to only ONE of an app's open connections ("each
payload may be sent to any of the connections"). A second socket process on the
same token would therefore steal roughly half of Boris's events and silently
break the live reviewer. Polling conversations.history touches no socket, so the
two pipelines cannot interfere.

Silence is success: stdout is delivered verbatim by `cron --no-agent`, so this
prints NOTHING on an ordinary tick. It speaks only for shadow-mode findings and
real failures.

Watermarks make the poll idempotent across ticks, and the event-dedup table
makes it idempotent even if a watermark is replayed.
"""
from __future__ import annotations

import fcntl
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slack_read import ENV_PATH, parse_env, slack_call  # noqa: E402

from boris_intake import audit  # noqa: E402
from boris_intake.identity import IdentityResolver  # noqa: E402
from boris_intake.models import NormalizedEvent  # noqa: E402

from jdi_autoapprove import github  # noqa: E402
from jdi_autoapprove.config import Config, load_config  # noqa: E402
from jdi_autoapprove.gate import GateDeps, evaluate  # noqa: E402
from jdi_autoapprove.selftest import run_selftest  # noqa: E402
from jdi_autoapprove.slack_io import SlackIO  # noqa: E402
from jdi_autoapprove.store import Store  # noqa: E402

STATE_DIR = Path("/Users/ctao/.hermes/state")
DB_PATH = STATE_DIR / "jdi_autoapprove.db"
AUDIT_PATH = STATE_DIR / "jdi_autoapprove_audit.jsonl"
LOCK_PATH = STATE_DIR / "jdi_autoapprove.lock"
VIEWER_CACHE = STATE_DIR / "jdi_autoapprove_viewer.txt"
HEARTBEAT = STATE_DIR / "jdi_autoapprove_heartbeat"
WATERMARK_PREFIX = "jdi_autoapprove_watermark_"

VIEWER_TTL = 86400
#: Cap per channel per tick. A backlog larger than this drains over subsequent
#: ticks rather than firing hundreds of approvals in one pass.
PAGE_LIMIT = 50


def normalize(raw: dict, channel: str) -> NormalizedEvent | None:
    ts = raw.get("ts")
    if not ts:
        return None
    return NormalizedEvent(
        channel_id=channel,
        channel_type=str(raw.get("channel_type") or "channel"),
        user_id=str(raw["user"]) if raw.get("user") else None,
        ts=str(ts),
        thread_ts=str(raw["thread_ts"]) if raw.get("thread_ts") else None,
        text=str(raw.get("text") or ""),
        is_bot=bool(raw.get("bot_id")),
        subtype=str(raw["subtype"]) if raw.get("subtype") else None,
    )


def cached_viewer(gh_token: str) -> str:
    """`gh api user` once a day rather than once a minute."""
    try:
        if time.time() - VIEWER_CACHE.stat().st_mtime < VIEWER_TTL:
            cached = VIEWER_CACHE.read_text().strip()
            if cached:
                return cached
    except OSError:
        pass
    login = github.viewer_login(gh_token)
    if login:
        VIEWER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        VIEWER_CACHE.write_text(login)
    return login


class Pipeline:
    def __init__(self, cfg: Config, deps: GateDeps, store: Store, slack: SlackIO,
                 token: str, audit_path: Path, approve=github.approve):
        self.cfg = cfg
        self.deps = deps
        self.store = store
        self.slack = slack
        self.token = token
        self.audit_path = Path(audit_path)
        self._approve = approve
        self.notes: list[str] = []      # lines worth surfacing to the operator

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
                **base, "decision": "skip",
                "reasons": [r.reason.value for r in result.rejections],
                "details": [r.detail for r in result.rejections],
            })
            # A tripped daily cap is the one skip an operator must hear about.
            for r in result.rejections:
                if r.reason.value == "daily_cap":
                    self.notes.append(f"PAUSED: {r.detail}")
            return

        approved, failed = [], []
        for cmd in result.commands:
            if not live:
                approved.append({"pr": cmd.slug, "head_sha": cmd.head_sha})
                self.notes.append(
                    f"[shadow] would approve {cmd.slug} @ {cmd.head_sha[:7]} "
                    f"(author {cmd.pr_author}, posted by {cmd.requester_email})")
                continue
            ok, detail = self._approve(
                cmd.owner, cmd.repo, cmd.pr_number, cmd.head_sha,
                self.cfg.approval_body, self.token)
            if ok:
                now = self.deps.now()
                self.store.record_approval(cmd.idempotency_key, cmd.slug, now)
                self.store.record_request(event.user_id, now)
                approved.append({"pr": cmd.slug, "head_sha": cmd.head_sha})
            else:
                failed.append({"pr": cmd.slug, "error": detail})
                self.notes.append(f"FAILED to approve {cmd.slug}: {detail}")

        if live:
            if approved:
                self.slack.add_reaction(event.channel_id, event.ts, self.cfg.reaction)
            if failed:
                self.slack.add_reaction(
                    event.channel_id, event.ts, self.cfg.failure_reaction)

        audit.write(self.audit_path, {
            **base,
            "decision": "approve" if approved else "approve_failed",
            "approved": approved,
            "failed": failed,
            "skipped": [{"reason": r.reason.value, "detail": r.detail}
                        for r in result.rejections],
        })


def _watermark_path(channel: str) -> Path:
    return STATE_DIR / f"{WATERMARK_PREFIX}{channel}.txt"


def poll_channels(cfg: Config, token: str, pipeline: Pipeline) -> None:
    for channel in cfg.channels:
        wm_file = _watermark_path(channel)
        try:
            oldest = wm_file.read_text().strip()
        except OSError:
            # First run for this channel: start from now, never retro-approve
            # the entire backlog sitting in the channel.
            wm_file.parent.mkdir(parents=True, exist_ok=True)
            wm_file.write_text(f"{time.time():.6f}")
            continue
        try:
            resp = slack_call(token, "conversations.history",
                              channel=channel, oldest=oldest, limit=PAGE_LIMIT)
        except Exception as exc:
            pipeline.notes.append(f"history failed for {channel}: {exc}")
            continue
        if not resp.get("ok"):
            pipeline.notes.append(f"history failed for {channel}: {resp.get('error')}")
            continue
        messages = resp.get("messages") or []
        for raw in sorted(messages, key=lambda m: m.get("ts", "0")):
            pipeline.handle(normalize(raw, channel))
        if messages:
            # Advance only after handling: a crash mid-batch replays, and the
            # event-dedup table absorbs the repeat.
            wm_file.write_text(max(m.get("ts", "0") for m in messages))


def main(argv: list[str]) -> int:
    full_selftest = "--selftest" in argv

    cfg = load_config()
    if cfg.mode == "off" and not full_selftest:
        return 0

    env = parse_env(ENV_PATH)
    token = env.get("TICKET_HUB_SLACK_BOT_TOKEN", "")
    if not token:
        print("jdi_autoapprove: TICKET_HUB_SLACK_BOT_TOKEN missing from .env")
        return 1

    gh_token = github.read_token(cfg.github_token_path)
    if not gh_token:
        print(f"jdi_autoapprove: no GitHub token at {cfg.github_token_path}")
        return 1
    viewer = cached_viewer(gh_token)

    # Local probes every tick (free, in-process). Network probes only when
    # explicitly asked for — they cost 4+ API calls and belong to deployment
    # verification, not to a once-a-minute loop.
    failures = run_selftest(cfg, slack_call if full_selftest else None,
                            token if full_selftest else "", viewer)
    if full_selftest:
        print("jdi_autoapprove selftest: "
              + ("PASS" if not failures else "FAIL"))
        for f in failures:
            print(f"  - {f}")
        print(f"  mode={cfg.mode} as={viewer or '?'} channels={list(cfg.channels)}")
        print(f"  reaction=:{cfg.reaction}: body={cfg.approval_body!r}")
        return 0 if not failures else 1
    if failures and cfg.mode == "live":
        cfg = replace(cfg, mode="shadow")
        print("jdi_autoapprove: self-test FAILED, holding in shadow:\n  "
              + "\n  ".join(failures))

    # Single-flight: a slow tick must never overlap the next and double-approve.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # previous tick still running; silence is correct

    try:
        store = Store(DB_PATH)
        store.prune()
        resolver = IdentityResolver(slack_call, token)
        deps = GateDeps(
            identity=resolver.resolve,
            pr_facts=lambda o, r, n: github.fetch_pr(o, r, n, gh_token),
            approved_at=store.approved_at,
            rate_verdict=lambda uid: store.rate_verdict(uid, cfg.limits, time.time()),
            viewer_login=viewer,
            now=time.time,
        )
        pipeline = Pipeline(cfg, deps, store,
                            SlackIO(slack_call, token, dry_run=cfg.mode != "live"),
                            gh_token, AUDIT_PATH)
        poll_channels(cfg, token, pipeline)
        HEARTBEAT.write_text(f"{time.time():.0f}")
        for note in pipeline.notes:
            print(note)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
