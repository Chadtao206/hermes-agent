"""The authorization gate. Pure, ordered, fail-closed.

Check order is deliberate: cheap local checks run before any network call, and
the channel check runs before identity resolution so an unauthorized channel
costs zero Slack API calls.

The output carries no Slack free text. That is the property that makes prompt
injection via the request message structurally impossible — see the spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from boris_intake.config import Config
from boris_intake.grammar import mentions_bot, parse_request
from boris_intake.identity import is_trusted
from boris_intake.models import (
    GateResult, Identity, NormalizedEvent, PRInfo, ReasonCode, Rejection,
    ReviewCommand, ReviewPolicy,
)


@dataclass(frozen=True)
class GateDeps:
    identity: Callable[[str], Identity | None]
    pr_info: Callable[[str, str, int], PRInfo | None]
    rate_verdict: Callable[[str], str]
    now: Callable[[], float]


def _permalink(channel: str, ts: str) -> str:
    return f"https://ylopo.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _reject(reason: ReasonCode, detail: str = "") -> GateResult:
    return GateResult(rejections=(Rejection(reason, detail),))


def _is_loop_repost(event: NormalizedEvent, cfg: Config) -> bool:
    """The remediation loop's own re-review request — the one admitted bot message.

    Boris posts its verdict as this same bot user, and that verdict contains a
    parseable PR URL, so identity alone cannot separate the two. The marker is
    what does: the loop emits it, Boris never does. Requiring a thread reply and
    no subtype keeps a stray top-level or edited message from qualifying.
    """
    if not (cfg.loop_marker and cfg.loop_bot_user_id and cfg.loop_attributed_user_id):
        return False
    return (
        event.is_bot
        and event.user_id == cfg.loop_bot_user_id
        and not event.subtype
        and cfg.loop_marker in (event.text or "")
        and bool(event.thread_ts)
        and event.thread_ts != event.ts
    )


def _is_discovery_post(event: NormalizedEvent, cfg: Config) -> bool:
    """The discovery cron's own review request -- the second admitted bot message.

    Mirrors `_is_loop_repost` but inverted on threading: discovery posts are
    top-level, reposts are replies. Its own marker, not the loop's, is what
    separates it from Boris's own top-level messages, which carry parseable
    PR URLs and post as this same bot user.
    """
    if not (cfg.discovery_marker and cfg.discovery_bot_user_id
            and cfg.loop_attributed_user_id):
        return False
    return (
        # Redundant with the user_id equality check below -- mirrors
        # _is_loop_repost's own is_bot check. Deliberate defence-in-depth, not
        # a coverage hole: keep it even though it can never fail on its own.
        event.is_bot
        and event.user_id == cfg.discovery_bot_user_id
        and not event.subtype
        and cfg.discovery_marker in (event.text or "")
        and (not event.thread_ts or event.thread_ts == event.ts)
    )


def evaluate(event: NormalizedEvent, cfg: Config, deps: GateDeps) -> GateResult:
    # Mode gate first — cheapest possible check.
    if cfg.mode == "off":
        return _reject(ReasonCode.MODE_OFF)

    # 1: real human message, or the remediation loop's own marked re-review,
    #    or the discovery cron's own marked top-level request
    loop_repost = _is_loop_repost(event, cfg)
    discovery = _is_discovery_post(event, cfg)
    if not (loop_repost or discovery) and (event.is_bot or not event.user_id or event.subtype):
        return _reject(ReasonCode.NOT_HUMAN, event.subtype or "no user id")

    # 2: channel allowlist (also excludes DMs — no im channel is ever listed)
    if event.channel_id not in cfg.channels:
        return _reject(ReasonCode.CHANNEL_NOT_ALLOWED, event.channel_id)

    mentioned = mentions_bot(event.text, cfg.bot_user_id)
    is_passive = event.channel_id in cfg.passive_channels

    # 1b: thread replies need an explicit mention — EXCEPT in a passive channel,
    # where a bare PR link is the request wherever it is posted. Re-posting under
    # the original review thread is the natural gesture after pushing a fix, and
    # silently ignoring it reads as broken. Safe because a repost at an unchanged
    # head is caught by the (repo, pr, head_sha) dedup below, and bot messages
    # (including Boris's own threaded ack) are already rejected as not_human.
    # Outside passive channels the mention is still required, so a link dropped
    # into an ordinary engineering discussion never starts a review.
    if (event.thread_ts and event.thread_ts != event.ts
            and not mentioned and not is_passive):
        return _reject(ReasonCode.THREAD_REPLY_NO_MENTION)

    # 4: grammar. Everything not matched here is discarded.
    parsed = parse_request(event.text)
    if not parsed.refs:
        return _reject(ReasonCode.NO_PR_URL)
    if not is_passive and not mentioned:
        return _reject(ReasonCode.MENTION_REQUIRED)
    if parsed.unknown_flags:
        return _reject(
            ReasonCode.UNKNOWN_FLAG,
            f"Unknown review flag: {parsed.unknown_flags[0]}. "
            "Supported flags: --ignore-ci",
        )

    # 3: identity. Fail closed on lookup failure. A loop repost or discovery
    #    post resolves as Chad so every downstream predicate, quota and audit
    #    row reads as his request.
    requester_user_id = (
        cfg.loop_attributed_user_id if (loop_repost or discovery) else event.user_id
    )
    identity = deps.identity(requester_user_id)
    if identity is None:
        return _reject(ReasonCode.IDENTITY_LOOKUP_FAILED, requester_user_id)
    if not is_trusted(identity, cfg):
        return _reject(ReasonCode.IDENTITY_NOT_TRUSTED, identity.email or requester_user_id)

    # 8: rate limits, evaluated once per message
    verdict = deps.rate_verdict(requester_user_id)
    if verdict != "ok":
        return _reject(ReasonCode.RATE_LIMITED, verdict)

    allowed_orgs = {o.lower() for o in cfg.orgs}
    policy = ReviewPolicy(
        ignore_ci="--ignore-ci" in parsed.flags,
        rubric_version=cfg.rubric_version,
    )
    commands: list[ReviewCommand] = []
    rejections: list[Rejection] = []

    for ref in parsed.refs:
        slug = f"{ref.owner}/{ref.repo}#{ref.number}"

        # 5: repo allowlist
        if ref.owner.lower() not in allowed_orgs:
            rejections.append(Rejection(ReasonCode.ORG_NOT_ALLOWED, slug))
            continue

        # 6: PR validation — head SHA is authoritative, never from Slack
        info = deps.pr_info(ref.owner, ref.repo, ref.number)
        if info is None:
            rejections.append(Rejection(ReasonCode.PR_NOT_FOUND, slug))
            continue
        if info.state != "OPEN":
            rejections.append(Rejection(ReasonCode.PR_NOT_OPEN, f"{slug} is {info.state}"))
            continue

        cmd = ReviewCommand(
            owner=ref.owner, repo=ref.repo, pr_number=ref.number,
            head_sha=info.head_sha, pr_author=info.author,
            requester_user_id=requester_user_id, requester_email=identity.email,
            channel_id=event.channel_id, message_ts=event.ts,
            root_thread_ts=event.thread_ts or event.ts,
            permalink=_permalink(event.channel_id, event.ts),
            policy=policy,
        )

        # 8b: burst cap. The rate check above is per-message, so without this a
        # single paste of N links would create N lanes on one check.
        if len(commands) >= cfg.limits.max_prs_per_message:
            rejections.append(Rejection(
                ReasonCode.RATE_LIMITED,
                f"{slug} — more than {cfg.limits.max_prs_per_message} PRs in one "
                "message; post the rest separately"))
            continue

        commands.append(cmd)

    return GateResult(commands=tuple(commands), rejections=tuple(rejections))
