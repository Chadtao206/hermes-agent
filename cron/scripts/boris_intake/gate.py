"""The authorization gate. Pure, ordered, fail-closed.

Check order is deliberate: cheap local checks run before any network call, and
the channel check runs before identity resolution so an unauthorized channel
costs zero Slack API calls.

The output carries no Slack free text. That is the property that makes prompt
injection via the request message structurally impossible — see the spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from boris_intake.config import Config
from boris_intake.grammar import mentions_bot, parse_request
from boris_intake.identity import is_trusted
from boris_intake.models import (
    GateResult, Identity, NormalizedEvent, PRLookup, ReasonCode, Rejection,
    RequestOrigin, ReviewCommand, ReviewPolicy,
)

if TYPE_CHECKING:  # a type only -- the pure gate imports no subprocess machinery
    from boris_intake.github import GitHubRetryBudget


@dataclass(frozen=True)
class GateDeps:
    identity: Callable[[str], Identity | None]
    #: Takes the message's shared retry budget as its last argument, so all of
    #: one message's PR reads spend one window between them. See
    #: `github.GitHubRetryBudget`.
    pr_info: Callable[[str, str, int, GitHubRetryBudget], PRLookup]
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

    "Boris never does" is an enforced property now, not an assumption, and it
    takes both of these to hold:

    * **Here: the marker must OPEN the text, not merely appear in it.** Both
      producers (`pr_remediation/slack_io.py::post_rereview` and
      `repost_rereview`) put `cfg.marker` on the first line, so `startswith` is
      the exact shape the loop emits. A substring test additionally admits any
      message that merely *quotes* the marker somewhere, and Boris's own
      verdict is exactly such a message: `result.render_slack` renders
      model-authored issue titles about an untrusted pull request, and
      `grammar.parse_request` then harvests every `Ylopo/*/pull/N` URL out of
      whatever is admitted, up to `Limits.max_prs_per_message`, all attributed
      to `loop_attributed_user_id`. That is the same laundering hazard
      `pr_discovery/post.py`'s module docstring is devoted to.
    * **There: `render_slack` neutralises the marker** so a verdict cannot
      carry it at all, whatever position it would land in. Deliberately
      belt-and-braces: either half alone is one careless edit from failing.

    `_is_discovery_post` deliberately does NOT get the same tightening --
    `pr_discovery/post.py::_message` composes the URL first and the marker
    second, so its producer's real messages do not start with it. Checked
    against that function, not assumed.
    """
    if not (cfg.loop_marker and cfg.loop_bot_user_id and cfg.loop_attributed_user_id):
        return False
    return (
        event.is_bot
        and event.user_id == cfg.loop_bot_user_id
        and not event.subtype
        and (event.text or "").startswith(cfg.loop_marker)
        and bool(event.thread_ts)
        and event.thread_ts != event.ts
    )


def _is_discovery_post(event: NormalizedEvent, cfg: Config) -> bool:
    """The discovery cron's own review request -- the second admitted bot message.

    Mirrors `_is_loop_repost` but inverted on threading: discovery posts are
    top-level, reposts are replies. Its own marker, not the loop's, is what
    separates it from Boris's own top-level messages, which carry parseable
    PR URLs and post as this same bot user.

    Still a substring test, unlike its mirror: `pr_discovery/post.py::_message`
    composes the PR URL first and the marker second, so a real discovery post
    does not start with the marker and `startswith` here would close the door
    on every one of them. What keeps Boris's own text from satisfying it is the
    other half of the loop door's defence -- `result.render_slack` neutralises
    both configured markers, so a verdict cannot carry either one at any
    position. See `_is_loop_repost`.
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


def evaluate(
    event: NormalizedEvent,
    cfg: Config,
    deps: GateDeps,
    budget: GitHubRetryBudget | None = None,
) -> GateResult:
    """Decide one message. `budget` is the retry window its PR reads share.

    A `None` budget makes every read single-shot, which is what this did before
    the retry existed -- so the live wiring passing one is a property in its own
    right, pinned by `test_daemon`, not something to assume from here.
    """
    # Mode gate first — cheapest possible check.
    if cfg.mode == "off":
        return _reject(ReasonCode.MODE_OFF)

    # 1: real human message, or the remediation loop's own marked re-review,
    #    or the discovery cron's own marked top-level request
    #    or a trusted teammate bot, attributed to its owner
    loop_repost = _is_loop_repost(event, cfg)
    discovery = _is_discovery_post(event, cfg)

    # Attribution, resolved once and carried on every result from here down. A
    # loop repost or discovery post resolves as Chad so every downstream
    # predicate, quota and audit row reads as his request; the ORIGIN is kept
    # alongside because the rejection-reply cooldown must not let one door's
    # rejections silence another's (see `GateResult.reply_cooldown_key`).
    origin = (
        RequestOrigin.LOOP if loop_repost
        else RequestOrigin.DISCOVERY if discovery
        else RequestOrigin.HUMAN
    )
    bot_owner = (
        cfg.trusted_bots.get(event.user_id or "", "")
        if event.is_bot and not event.subtype else ""
    )
    requester_user_id = (
        cfg.loop_attributed_user_id if (loop_repost or discovery)
        else bot_owner or (event.user_id or "")
    )

    def reject(reason: ReasonCode, detail: str = "") -> GateResult:
        return GateResult(
            rejections=(Rejection(reason, detail),),
            requester_user_id=requester_user_id,
            origin=origin,
        )

    if not (loop_repost or discovery or bot_owner) and (
            event.is_bot or not event.user_id or event.subtype):
        return reject(ReasonCode.NOT_HUMAN, event.subtype or "no user id")

    # 2: channel allowlist (also excludes DMs — no im channel is ever listed)
    if event.channel_id not in cfg.channels:
        return reject(ReasonCode.CHANNEL_NOT_ALLOWED, event.channel_id)

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
        return reject(ReasonCode.THREAD_REPLY_NO_MENTION)

    # 4: grammar. Everything not matched here is discarded.
    parsed = parse_request(event.text)
    if not parsed.refs:
        return reject(ReasonCode.NO_PR_URL)
    if not is_passive and not mentioned:
        return reject(ReasonCode.MENTION_REQUIRED)
    if parsed.unknown_flags:
        return reject(
            ReasonCode.UNKNOWN_FLAG,
            f"Unknown review flag: {parsed.unknown_flags[0]}. "
            "Supported flags: --ignore-ci",
        )

    # 3: identity. Fail closed on lookup failure.
    identity = deps.identity(requester_user_id)
    if identity is None:
        return reject(ReasonCode.IDENTITY_LOOKUP_FAILED, requester_user_id)
    if not is_trusted(identity, cfg):
        return reject(ReasonCode.IDENTITY_NOT_TRUSTED, identity.email or requester_user_id)

    # 8: rate limits, evaluated once per message
    verdict = deps.rate_verdict(requester_user_id)
    if verdict != "ok":
        return reject(ReasonCode.RATE_LIMITED, verdict)

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

        # 6: PR validation — head SHA is authoritative, never from Slack.
        # Three states, not two: only GitHub's own not-found answer justifies
        # PR_NOT_FOUND, which is terminal and asserts the pull request does not
        # exist. A read that failed is GITHUB_UNAVAILABLE, which says only
        # "could not check" and invites a re-post. Getting this backwards is the
        # 2026-08-17 incident; see `models.GitHubReadStatus`.
        lookup = deps.pr_info(ref.owner, ref.repo, ref.number, budget)
        if not lookup.found:
            rejections.append(
                Rejection(ReasonCode.PR_NOT_FOUND, slug)
                if lookup.absent
                else Rejection(
                    ReasonCode.GITHUB_UNAVAILABLE,
                    f"{slug} — {lookup.detail}" if lookup.detail else slug,
                )
            )
            continue
        info = lookup.info
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

    return GateResult(
        commands=tuple(commands),
        rejections=tuple(rejections),
        requester_user_id=requester_user_id,
        origin=origin,
    )
