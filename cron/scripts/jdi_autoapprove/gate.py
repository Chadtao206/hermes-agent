"""The authorization gate. Pure, ordered, fail-closed.

Check order is deliberate: cheap local checks run before any network call, and
the channel check runs before identity resolution so a message in an unwatched
channel costs zero Slack API calls.

Deliberately simpler than boris_intake's gate in one respect and stricter in
another. Simpler: these channels are fully passive by policy — a PR link IS the
request, no mention required, thread replies included. Stricter: the terminal
action is an irreversible-ish public approval rather than a review lane, so it
adds self-authored and already-approved skips and a global daily breaker.

There is deliberately NO CI check. That is the operator's explicit choice:
these channels approve on posting, unconditionally.

The output carries no Slack free text, which is what makes prompt injection via
the message structurally impossible — nothing from ``text`` reaches GitHub.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from boris_intake.grammar import extract_pr_refs
from boris_intake.identity import is_trusted
from boris_intake.models import Identity, NormalizedEvent

from jdi_autoapprove.config import Config
from jdi_autoapprove.models import (
    ApproveCommand, GateResult, PRFacts, ReasonCode, Rejection,
)


@dataclass(frozen=True)
class GateDeps:
    identity: Callable[[str], Identity | None]
    pr_facts: Callable[[str, str, int], PRFacts | None]
    approved_at: Callable[[str], float | None]
    rate_verdict: Callable[[str], str]
    viewer_login: str
    now: Callable[[], float]


def _reject(reason: ReasonCode, detail: str = "") -> GateResult:
    return GateResult(rejections=(Rejection(reason, detail),))


def evaluate(event: NormalizedEvent, cfg: Config, deps: GateDeps) -> GateResult:
    # Mode gate first — cheapest possible check.
    if cfg.mode == "off":
        return _reject(ReasonCode.MODE_OFF)

    # 1: real human message. Excludes bots, which is why a PR link posted by
    # dependabot/renovate or by another Hermes automation never self-approves.
    if event.is_bot or not event.user_id or event.subtype:
        return _reject(ReasonCode.NOT_HUMAN, event.subtype or "no user id")

    # 2: channel allowlist (also excludes DMs — no im channel is ever listed)
    if event.channel_id not in cfg.channels:
        return _reject(ReasonCode.CHANNEL_NOT_ALLOWED, event.channel_id)

    # 3: grammar. Everything not matched here is discarded.
    refs = extract_pr_refs(event.text)
    if not refs:
        return _reject(ReasonCode.NO_PR_URL)

    # 4: identity. Fail closed on lookup failure.
    identity = deps.identity(event.user_id)
    if identity is None:
        return _reject(ReasonCode.IDENTITY_LOOKUP_FAILED, event.user_id)
    if not is_trusted(identity, cfg):
        return _reject(ReasonCode.IDENTITY_NOT_TRUSTED, identity.email or event.user_id)

    # 5: rate limits + global daily breaker, evaluated once per message
    verdict = deps.rate_verdict(event.user_id)
    if verdict != "ok":
        code = (ReasonCode.DAILY_CAP if verdict.startswith("global cap")
                else ReasonCode.RATE_LIMITED)
        return _reject(code, verdict)

    allowed_orgs = {o.lower() for o in cfg.orgs}
    viewer = (deps.viewer_login or "").lower()
    commands: list[ApproveCommand] = []
    rejections: list[Rejection] = []

    for ref in refs:
        slug = f"{ref.owner}/{ref.repo}#{ref.number}"

        # 6: org allowlist
        if ref.owner.lower() not in allowed_orgs:
            rejections.append(Rejection(ReasonCode.ORG_NOT_ALLOWED, slug))
            continue

        # 7: PR validation — head SHA is authoritative, never from Slack
        facts = deps.pr_facts(ref.owner, ref.repo, ref.number)
        if facts is None:
            rejections.append(Rejection(ReasonCode.PR_NOT_FOUND, slug))
            continue
        if facts.state != "OPEN":
            rejections.append(Rejection(ReasonCode.PR_NOT_OPEN, f"{slug} is {facts.state}"))
            continue

        # 8: GitHub refuses a self-approval with a 422. Skip cleanly rather than
        # burning an API call to collect a guaranteed error.
        if viewer and facts.author.lower() == viewer:
            rejections.append(Rejection(ReasonCode.SELF_AUTHORED, f"{slug} authored by {viewer}"))
            continue

        # 9: noise reduction, not a review precondition — a PR a human already
        # approved does not need a second stamp on top.
        if cfg.skip_if_already_approved and facts.review_decision == "APPROVED":
            rejections.append(Rejection(ReasonCode.ALREADY_APPROVED, slug))
            continue

        cmd = ApproveCommand(
            owner=ref.owner, repo=ref.repo, pr_number=ref.number,
            head_sha=facts.head_sha, pr_author=facts.author,
            requester_user_id=event.user_id, requester_email=identity.email,
            channel_id=event.channel_id, message_ts=event.ts,
        )

        # 10: dedup on (repo, pr, head_sha) — a re-post at an unchanged head is
        # a no-op; a new commit is a new key and earns its own stamp.
        if deps.approved_at(cmd.idempotency_key) is not None:
            rejections.append(Rejection(
                ReasonCode.DUPLICATE, f"{slug} @ {cmd.head_sha[:7]}"))
            continue

        # 11: burst cap. The rate check above is per-message, so without this a
        # single paste of N links would approve N PRs on one check.
        if len(commands) >= cfg.limits.max_prs_per_message:
            rejections.append(Rejection(
                ReasonCode.RATE_LIMITED,
                f"{slug} — more than {cfg.limits.max_prs_per_message} PRs in one message"))
            continue

        commands.append(cmd)

    return GateResult(commands=tuple(commands), rejections=tuple(rejections))
