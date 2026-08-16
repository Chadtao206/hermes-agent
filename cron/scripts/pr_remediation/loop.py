"""The remediation loop tick: verdict routing and round accounting.

Two orderings carry the whole design.

*Reads before writes.* The hold label and the live author are checked before
anything is written, so a PR Chad has taken back is never touched. `has_hold`
fails closed (an unreadable GitHub is a hold), and an unreadable author is
treated as "unknown", not as "reassigned" -- escalating a transient read blip
would burn the loop permanently on a network hiccup.

*Lease before side effects.* Every irreversible act -- labelling the PR,
dispatching a remediation lane, posting to Slack -- happens only after
`store.claim()` has leased this exact loop, and every `transition()` result is
checked. Without the lease a rival tick can advance the row underneath us, and
the closing compare-and-swap then silently returns False *after* the lane
exists: an orphaned lane, a lost round, and a duplicate Slack post. With it,
the only surviving failure is a lease that lapses mid-batch, which shows up as
a False from `transition()` and is recorded as `orphaned_lane` rather than
counted as progress.

Three invariants sit on top of those orderings.

*One lane per loop.* A round may start only while no lane is in flight -- from
`observed`, `awaiting_head_move` or `rereview_posted`, never from
`remediating` with a lane recorded, and never while a lane this loop opened but
failed to record is still outstanding. Two verdicts for one PR can both be
unseen in a single sweep (a re-review completing just behind its review), and
without this rule the second one opens a second lane against the same branch:
exactly the collision this service exists to remove. Kanban's idempotency key
does not cover the orphaned case, because that key embeds the head sha and the
orphan's own push is what moves the head -- so the trail, not the row, has to
answer for it, and the orphan is *adopted* (the lost write retried, nothing
else) rather than replaced. The in-flight lane, not a new verdict, is what
drives the loop's next step; the refused verdict is written to the trail as
`verdict_deferred` rather than silently dropped.

*An escalation is not finished until a human has it.* Escalation exists
because a person has to intervene, so a notification that failed does NOT close
the loop. The loop stays non-terminal with an `escalation_pending` note, and a
later tick re-posts -- only the post, never the remediation side effects and
never another round. Retries are bounded by NOTIFY_ATTEMPTS: a permanently
misconfigured channel would otherwise pin the loop open forever, and a loop
that never goes terminal blocks every later loop on that PR
(`active_for_pr`). At the bound the loop closes with `escalation_unreachable`
in the trail and an ERROR in the log. That budget is spread across ticks by
design, so one sweep spends at most one attempt on a loop.

"Notified" and "closed" are two debts, not one: the notice is recorded before
the closing compare-and-swap (so a crash cannot double-post), which means the
close can be lost after a human already has the notice. Such a loop must keep
being retried -- silently, the write only -- or it never reaches a terminal
state and `active_for_pr` blocks every future generation for that PR.

*Shadow writes nothing outside the store.* Shadow (and off) is the kill switch
an operator reaches for to stop this service, so every routing branch is gated
on `cfg.mode` -- the pending-escalation intercept included, which a live run is
what creates. A shadow tick records what it would have done and leaves the
escalation debt, the ownership label and the human channel exactly as a later
live tick will find them.

Escalation reasons are free-form strings, not only `EscalationReason` members:
the enum names the loop's *own* dead ends (round cap, no-op lane), while the
driver also stops for reasons the enum does not model -- the PR changed hands,
GitHub is unwritable, the re-review has no thread to land in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig
from pr_remediation.models import EscalationReason, LoopRequest, LoopState
from pr_remediation.store import CorruptLoopRow

# A freshly created lane must not be inspected on the same tick that created it:
# claim_due would find it, see a not-yet-started lane, and escalate it as a no-op.
LANE_POLL_SECONDS = 15

# How many times a single escalation may be posted before the loop closes
# unheard. Chosen high enough to ride out a Slack outage across several ticks,
# low enough that a misconfigured channel cannot hold the PR's loop open (and
# with it every later loop for that PR) indefinitely.
NOTIFY_ATTEMPTS = 5

# Loops the retry pass leases per tick.
DUE_LIMIT = 20

# Stop reasons outside EscalationReason's vocabulary. See the module docstring.
NOT_CHAD_AUTHORED = "not_chad_authored"
GITHUB_UNWRITABLE = "github_unwritable"
REREVIEW_UNPOSTABLE = "rereview_unpostable"

# Why a verdict was seen but deliberately not routed.
LANE_IN_FLIGHT = "lane_in_flight"
LANE_ORPHANED = "lane_orphaned"
ESCALATION_OWED = "escalation_owed"
ESCALATION_ATTEMPT_SPENT = "escalation_attempt_spent"

# Event kinds the driver replays to reconstruct what the row cannot hold: the
# escalation debt, and a lane whose write was lost. Kept apart from the defer
# reasons above -- the two vocabularies read alike but are matched by different
# code (a reason is a field on a `verdict_deferred` event, a kind is what
# `events()` matches on), and one literal serving as both invites a defer note
# being replayed as an escalation attempt.
EVENT_ESCALATION_PENDING = "escalation_pending"
EVENT_ESCALATION_DELIVERED = "escalation_delivered"
EVENT_ESCALATION_UNREACHABLE = "escalation_unreachable"
EVENT_ORPHANED_LANE = "orphaned_lane"
# Written by store.transition() itself, and only when the swap landed.
EVENT_TRANSITION = "transition"

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopDeps:
    source: Any
    github: Any
    slack: Any
    lanes: Any
    store: Any
    cfg: RemediationConfig
    now: Callable[[], float]


@dataclass(frozen=True)
class EscalationDebt:
    """What a stopped loop still owes, and to whom.

    `notified` says a human has the notice (or that we have given up reaching
    one). It does not say the loop is closed -- that is the row reaching
    `escalated`. Keeping them apart is the whole point of this type: a
    delivered escalation whose closing compare-and-swap was lost owes no post
    and every remaining write, and treating it as settled pins its PR open for
    good.
    """
    reason: str
    attempts: int
    notified: bool


def _shadow(deps: LoopDeps, loop_id: str, action: str, detail: dict) -> None:
    deps.store.record_note(loop_id, "shadow_decision",
                           {"would": action, **detail}, deps.now())


def _transition(deps: LoopDeps, loop: LoopRequest, target: LoopState,
                **fields: Any) -> bool:
    """Compare-and-swap the loop forward, recording a lost write.

    A False here means another tick owns the row (or our lease lapsed), so the
    side effects we just performed are no longer ours to claim credit for. It
    is never silently ignored: callers stop, and the event trail carries the
    loss so a human can reconcile whatever we already did.
    """
    ok = deps.store.transition(loop.loop_id, loop.state, target,
                               now=deps.now(), **fields)
    if not ok:
        deps.store.record_note(
            loop.loop_id, "transition_lost",
            {"from": loop.state.value, "to": target.value}, deps.now())
    return ok


def _stand_down(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> None:
    """Hand the PR back: no remediation round is running any more.

    Both acts are best-effort cleanup of signals other agents and humans read
    (the ownership label, the thinkies reaction). Failing to clear either is
    worth recording -- a stuck label makes the PR look owned forever -- but
    must not stop the loop from reaching its terminal state.

    The reaction is anchored on the *loop's* thread, not this verdict's: a
    later verdict can carry a different root, and a reaction added on one
    message can only ever be removed from that same message.
    """
    if loop.root_thread_ts:
        deps.slack.remove_thinkies(deps.cfg.boris_channel, loop.root_thread_ts)
    if not deps.github.release(verdict.owner, verdict.repo, verdict.pr_number):
        deps.store.record_note(loop.loop_id, "release_failed",
                               {"label": deps.cfg.owner_label}, deps.now())


def _defer_verdict(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
                   reason: str) -> None:
    """Record a verdict the driver saw and deliberately did not route.

    `tick` marks a verdict seen before handling it, so it is never replayed:
    this note is the only surviving trace that it existed. It carries enough to
    reconstruct what was refused (which review, what it said, against which
    head) so a human reading the trail can tell a deliberate refusal from a
    verdict the loop simply never received.
    """
    deps.store.record_note(loop.loop_id, "verdict_deferred", {
        "reason": reason,
        "request_id": verdict.request_id,
        "outcome": verdict.outcome,
        "head_sha": verdict.head_sha,
        "lane_id": loop.lane_id,
        "round": loop.round_count,
    }, deps.now())


def _escalation_debt(deps: LoopDeps, loop: LoopRequest) -> EscalationDebt | None:
    """What this loop still owes, replayed from its trail, or None if nothing.

    The event trail is the record, because there is no room for one in the row:
    `transition()` writes a fixed set of columns and the state machine has no
    "escalating" state. Replaying the trail in order also settles the
    orderings for free -- a delivery after the failures means the *notice* is
    no longer outstanding, and the failed attempts before it stop counting.

    Only ever asked of a non-terminal row: the verdict path holds a lease,
    which terminal states refuse, and the due pass claims non-terminal rows
    only. So a debt found here is by definition unfinished -- either nobody has
    been told, or somebody has and the closing write never landed.
    """
    reason: str | None = None
    attempts = 0
    notified = False
    for event in deps.store.events(loop.loop_id):
        kind, detail = event["kind"], event["detail"]
        if kind == EVENT_ESCALATION_PENDING:
            reason = str(detail.get("reason") or "")
            attempts += 1
        elif kind in (EVENT_ESCALATION_DELIVERED, EVENT_ESCALATION_UNREACHABLE):
            reason = str(detail.get("reason") or reason or "")
            attempts, notified = 0, True
    if reason is None:
        return None
    return EscalationDebt(reason=reason, attempts=attempts, notified=notified)


def _settle_escalation(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
                       debt: EscalationDebt) -> bool:
    """Tell the human if nobody has yet, and close the loop either way.

    A failed post leaves the loop non-terminal on purpose. Closing it anyway
    would be the worst outcome this system can produce -- it looks exactly like
    a handled escalation, while the person who was supposed to take over never
    heard anything, and the only trace would be an event nobody watches.

    A delivery is recorded *before* the stand-down and the compare-and-swap, so
    a crash in between cannot make a later tick post the same notice twice.
    The price of that ordering is a close that can be lost after the notice
    landed, which is what `notified` is for: it gates the whole notify-and-
    stand-down half, so a loop that has been heard but not closed comes back
    here and retries the one write still missing -- no post, no label, no
    reaction, nothing a human could see twice.
    """
    if not debt.notified:
        if deps.slack.notify_escalation(verdict, loop.round_count,
                                        debt.reason) is None:
            attempt = debt.attempts + 1
            if attempt < NOTIFY_ATTEMPTS:
                deps.store.record_note(loop.loop_id, EVENT_ESCALATION_PENDING,
                                       {"reason": debt.reason, "attempt": attempt},
                                       deps.now())
                return False
            # Out of attempts. Holding the loop open past this point buys
            # nothing (the channel is not coming back on its own) and costs the
            # PR every future loop, since active_for_pr would keep finding this
            # one.
            _LOG.error("escalation for loop %s (%s) never reached a human in %d attempts",
                       loop.loop_id, debt.reason, attempt)
            deps.store.record_note(loop.loop_id, EVENT_ESCALATION_UNREACHABLE,
                                   {"reason": debt.reason, "attempts": attempt},
                                   deps.now())
        else:
            deps.store.record_note(loop.loop_id, EVENT_ESCALATION_DELIVERED,
                                   {"reason": debt.reason}, deps.now())
        _stand_down(deps, loop, verdict)
    return _transition(deps, loop, LoopState.ESCALATED,
                       escalation_reason=debt.reason)


def _escalate(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
              reason: str) -> bool:
    if deps.cfg.mode != "live":
        _shadow(deps, loop.loop_id, "escalate", {"reason": reason})
        return _transition(deps, loop, LoopState.ESCALATED,
                           escalation_reason=reason)
    # A debt with nothing paid on it yet: a loop that already owes one is
    # intercepted before it can reach here, on the verdict path and in the due
    # pass alike, so this is always the first attempt at this escalation.
    return _settle_escalation(deps, loop, verdict,
                              EscalationDebt(reason=reason, attempts=0,
                                             notified=False))


def _finish_ready(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    if deps.cfg.mode == "live":
        if deps.slack.notify_ready(verdict, loop.round_count) is None:
            deps.store.record_note(loop.loop_id, "notify_failed",
                                   {"kind": "ready"}, deps.now())
        _stand_down(deps, loop, verdict)
    else:
        _shadow(deps, loop.loop_id, "notify_ready", {"rounds": loop.round_count})
    return _transition(deps, loop, LoopState.READY)


def _repost(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """A system-origin incomplete is Boris failing, not the PR failing.

    Retrying the review costs no remediation round: nothing was remediated.
    """
    if deps.cfg.mode != "live":
        _shadow(deps, loop.loop_id, "repost_rereview", {"transient": True})
        return _transition(deps, loop, LoopState.REREVIEW_POSTED)
    if not loop.root_thread_ts:
        # SlackIO refuses to post outside a thread because the intake gate
        # silently drops such a message. No thread means no route back into
        # review, ever -- that is permanent, so escalate rather than retry.
        return _escalate(deps, loop, verdict, REREVIEW_UNPOSTABLE)
    if deps.slack.post_rereview(verdict, loop.root_thread_ts,
                                loop.round_count + 1) is None:
        # A post that failed with a thread present is a Slack blip. Leave the
        # loop in its current state, unspent and immediately due, so the
        # daemon's sweep retries it instead of burning it.
        deps.store.record_note(loop.loop_id, "rereview_post_failed",
                               {"round": loop.round_count + 1}, deps.now())
        return False
    return _transition(deps, loop, LoopState.REREVIEW_POSTED)


def _lane_in_flight(loop: LoopRequest) -> bool:
    """Is a remediation lane already working this branch, by the row's account?

    State alone is the rule -- `remediating` means a round is running -- with
    one qualification: a `remediating` row carrying no lane_id has nothing
    running (a shadow-mode round, or a live flip on top of one). Treating that
    as in-flight would strand it forever, because the lane pass has no lane to
    poll and would never move it on.

    The row is not the whole account. A lane whose recording write was lost
    leaves no trace here at all -- `_orphaned_lane` answers for that one.
    """
    return loop.state is LoopState.REMEDIATING and loop.lane_id is not None


def _orphaned_lane(deps: LoopDeps, loop: LoopRequest) -> dict | None:
    """A lane this loop opened but never managed to record, if one is outstanding.

    The row cannot answer this: the compare-and-swap that would have written
    `lane_id` is precisely the write that was lost, so the row still carries
    its previous state and no lane. The trail can, and the trail is per loop --
    one generation of one PR -- so an orphan can never speak for a later
    generation of the same pull request.

    A transition that did record a lane supersedes the orphan: whatever the row
    points at now is what is running.
    """
    orphan: dict | None = None
    for event in deps.store.events(loop.loop_id):
        kind, detail = event["kind"], event["detail"]
        if kind == EVENT_ORPHANED_LANE and detail.get("lane_id"):
            orphan = detail
        elif kind == EVENT_TRANSITION and detail.get("lane_id"):
            orphan = None
    return orphan


def _adopt_lane(deps: LoopDeps, loop: LoopRequest, orphan: dict) -> bool:
    """Record an orphaned lane instead of dispatching a second one.

    That lane is real and pushing to this branch, so a fresh dispatch is the
    collision this service exists to remove -- and kanban's idempotency key
    cannot stop it, because the key embeds the head sha and the orphan's own
    push is what moves the head.

    Only the lost write is retried: no lane, no round, no label, no post. That
    is also what keeps an orphan from stranding the loop. Once the row carries
    the lane, the one-lane rule sees it and the lane pass has something to
    poll, so the loop goes on from that lane's completion like any other round;
    until the write lands, every verdict and every due sweep tries it again.

    The round the orphan spent is the round recorded. It cannot double-count:
    any later dispatch that did land its write would have superseded the
    orphan, and this path never reaches a row that already carries a lane.
    """
    return _transition(
        deps, loop, LoopState.REMEDIATING,
        round_count=int(orphan.get("round") or loop.round_count + 1),
        lane_id=str(orphan["lane_id"]),
        head_sha=str(orphan.get("head_sha") or loop.head_sha),
        next_attempt_at=deps.now() + LANE_POLL_SECONDS)


def _remediate(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    cfg = deps.cfg
    if _lane_in_flight(loop):
        # A second lane on this branch is the collision this service exists to
        # remove, and here we would be causing it ourselves. The lane already
        # running drives the next step: it finishes, the head moves, the
        # re-review goes out, and *that* verdict starts the next round. Checked
        # before the round cap, because the round this verdict would spend has
        # already been spent by the lane in flight.
        _defer_verdict(deps, loop, verdict, LANE_IN_FLIGHT)
        return False
    orphan = _orphaned_lane(deps, loop)
    if orphan is not None:
        # The row says no lane, the trail says otherwise, and the trail is
        # right: this loop's own dispatch lost its recording write. Adopt that
        # lane rather than open a second one against the same branch. In
        # shadow too, deliberately: adoption dispatches nothing and posts
        # nothing, it only corrects the store's record of a lane that is
        # already running, and the shadow branch below would otherwise write a
        # round with no lane at all -- a worse account of the same PR.
        _defer_verdict(deps, loop, verdict, LANE_ORPHANED)
        return _adopt_lane(deps, loop, orphan)
    if loop.round_count >= cfg.round_cap:
        return _escalate(deps, loop, verdict, EscalationReason.ROUND_CAP.value)
    round_number = loop.round_count + 1

    if cfg.mode != "live":
        _shadow(deps, loop.loop_id, "open_lane", {"round": round_number})
        return _transition(deps, loop, LoopState.REMEDIATING,
                           round_count=round_number, head_sha=verdict.head_sha)

    if not deps.github.claim(verdict.owner, verdict.repo, verdict.pr_number):
        # The ownership label is how every other agent learns this PR is taken.
        # Without it we would push commits to a PR nobody knows we own -- and a
        # failed label write usually means GitHub is unwritable anyway.
        deps.store.record_note(loop.loop_id, "github_claim_failed",
                               {"round": round_number}, deps.now())
        return _escalate(deps, loop, verdict, GITHUB_UNWRITABLE)

    lane_id = deps.lanes.create(verdict, round_number)
    if lane_id is None:
        # Dispatch refused (no worktree anchor, an excluded infra repo, a
        # kanban failure). No lane means no remediation, so the round stays
        # unspent and a human takes over.
        return _escalate(deps, loop, verdict, EscalationReason.LANE_FAILED.value)

    # The lane exists from here on. Record the round before decorating the
    # thread: if the compare-and-swap is lost we are no longer the driver, and
    # a thinkies reaction would claim a round this tick does not own.
    if not _transition(deps, loop, LoopState.REMEDIATING,
                       round_count=round_number, lane_id=lane_id,
                       head_sha=verdict.head_sha,
                       next_attempt_at=deps.now() + LANE_POLL_SECONDS):
        # Everything a later tick needs to adopt this lane rather than open a
        # rival one: the row itself will carry no trace of it.
        deps.store.record_note(loop.loop_id, EVENT_ORPHANED_LANE,
                               {"lane_id": lane_id, "round": round_number,
                                "head_sha": verdict.head_sha}, deps.now())
        return False
    if loop.root_thread_ts:
        deps.slack.add_thinkies(cfg.boris_channel, loop.root_thread_ts)
    return True


def _drive(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
           already_driven: bool) -> bool:
    """Route one verdict against a loop we hold the lease on.

    `already_driven` says this sweep has worked this loop before, for an
    earlier verdict on the same pull request.
    """
    store, cfg = deps.store, deps.cfg

    debt = _escalation_debt(deps, loop)
    if debt is not None:
        # This loop has already given up; all that is left is a human hearing
        # about it and the row being closed. Routing the verdict would restart
        # a loop we stopped, so settle the debt instead: no lane, no round, no
        # round-cap accounting. Ahead of the hold check on purpose -- what
        # follows is a Slack post plus the stand-down of signals this loop
        # itself added, which is what a hold wants to happen anyway -- and
        # ahead of the author check, which would otherwise escalate an
        # escalated loop again.
        if cfg.mode != "live":
            # The one routing branch a shadow run can reach without having
            # created the state itself: a live run left the debt, an operator
            # then flipped the switch to stop the service. Settling here would
            # post to a human channel, hand the ownership label back and drive
            # the loop terminal, from the mode that exists to write nothing.
            # Left untouched, the debt is still there for a later live tick.
            _shadow(deps, loop.loop_id, "settle_escalation",
                    {"reason": debt.reason, "notified": debt.notified})
            _defer_verdict(deps, loop, verdict, ESCALATION_OWED)
            return False
        if already_driven:
            # NOTIFY_ATTEMPTS is a budget for riding out a Slack outage across
            # several ticks. A second verdict for this loop in this sweep would
            # spend a second attempt on the same outage seconds later, and
            # enough of them would close the loop unheard inside one tick.
            _defer_verdict(deps, loop, verdict, ESCALATION_ATTEMPT_SPENT)
            return False
        _defer_verdict(deps, loop, verdict, ESCALATION_OWED)
        return _settle_escalation(deps, loop, verdict, debt)

    if deps.github.has_hold(verdict.owner, verdict.repo, verdict.pr_number):
        # Chad's kill switch, and also the fail-closed answer when GitHub
        # cannot be read at all.
        store.record_note(loop.loop_id, "held", {"label": cfg.hold_label},
                          deps.now())
        return False
    author = deps.github.author(verdict.owner, verdict.repo, verdict.pr_number)
    if author is None:
        store.record_note(loop.loop_id, "author_unreadable", {}, deps.now())
        return False
    if author != cfg.chad_github_login:
        return _escalate(deps, loop, verdict, NOT_CHAD_AUTHORED)

    if verdict.outcome == "READY_FOR_HUMAN_REVIEW":
        return _finish_ready(deps, loop, verdict)
    if verdict.outcome == "REVIEW_INCOMPLETE":
        if verdict.incomplete_origin == "system":
            return _repost(deps, loop, verdict)
        return _escalate(deps, loop, verdict,
                         EscalationReason.MODEL_INCOMPLETE.value)
    if verdict.outcome != "ISSUES_FOUND":
        store.record_note(loop.loop_id, "unknown_outcome",
                          {"outcome": verdict.outcome}, deps.now())
        return False
    return _remediate(deps, loop, verdict)


def _handle(deps: LoopDeps, verdict: Verdict, driven: set[str]) -> bool:
    store = deps.store
    loop = store.active_for_pr(verdict.owner, verdict.repo, verdict.pr_number)
    if loop is None:
        loop = store.open_loop(
            owner=verdict.owner, repo=verdict.repo, pr_number=verdict.pr_number,
            head_sha=verdict.head_sha, boris_request_id=verdict.request_id,
            root_thread_ts=verdict.root_thread_ts, now=deps.now(),
        )
    if loop is None:
        # Another tick opened this PR's loop between our read and our insert.
        # It owns the verdict routing for this PR; we must not double-drive it.
        return False

    # `driven` guards both passes, and is read before it is written. The due
    # pass must not touch a loop this pass has already worked on this tick, and
    # neither must a second verdict for the same pull request: either way a
    # single tick would spend two of one escalation's notification attempts.
    # Recorded whatever happens below, a refused claim included -- that loop is
    # being driven, just not by us.
    already_driven = loop.loop_id in driven
    driven.add(loop.loop_id)
    if not store.claim(loop.loop_id, now=deps.now()):
        # Leased elsewhere, or already terminal. Either way, not ours to drive.
        return False
    try:
        # Re-read under the lease: the snapshot above was taken unleased, so
        # its state and round_count may already be stale, and driving from a
        # stale round_count is how a round gets spent twice.
        leased = store.get(loop.loop_id)
        if leased is None:
            return False
        return _drive(deps, leased, verdict, already_driven)
    finally:
        store.release(loop.loop_id)


def _note_quietly(deps: LoopDeps, loop_id: str, kind: str, detail: dict) -> None:
    """Best-effort forensics from an error path, which must not itself raise."""
    try:
        deps.store.record_note(loop_id, kind, detail, deps.now())
    except Exception:
        _LOG.exception("could not record %s for loop %s", kind, loop_id)


def _verdict_for(loop: LoopRequest) -> Verdict:
    """Enough of a Verdict to address Slack and GitHub for a loop we hold.

    The due pass has no verdict in hand: the one that caused the escalation
    was consumed ticks ago, and a notice only needs the PR's coordinates.
    """
    return Verdict(
        request_id=loop.boris_request_id, owner=loop.owner, repo=loop.repo,
        pr_number=loop.pr_number, head_sha=loop.head_sha,
        root_thread_ts=loop.root_thread_ts, outcome="",
        incomplete_origin=None, blocking_issues=(),
    )


def _settle_due(deps: LoopDeps, driven: set[str]) -> int:
    """Retry the writes that cannot wait for a verdict to carry them.

    Two debts qualify, and neither can count on a verdict arriving. An
    escalated PR gets no further verdicts at all -- that is what escalating
    means -- and an orphaned lane need not produce one either, since a lane
    that changes nothing triggers no re-review.

    The pass leases each loop exactly as the verdict path does, and only ever
    retries a write: it opens no lane, spends no round, reads nothing from
    GitHub, and says nothing to a human who has already been told. A loop can
    therefore pass through it many times without accumulating side effects.
    """
    if deps.cfg.mode != "live":
        return 0
    try:
        due = deps.store.claim_due(now=deps.now(), limit=DUE_LIMIT)
    except Exception:
        _LOG.exception("the due pass could not lease its batch")
        return 0

    advanced = 0
    for loop in due:
        try:
            if loop.loop_id in driven:
                continue
            debt = _escalation_debt(deps, loop)
            if debt is not None:
                # A stopped loop stays stopped: settle it, and never mind the
                # lane, which its escalation has already given up on.
                if _settle_escalation(deps, loop, _verdict_for(loop), debt):
                    advanced += 1
                continue
            orphan = _orphaned_lane(deps, loop)
            if orphan is not None and _adopt_lane(deps, loop, orphan):
                advanced += 1
        except Exception:
            _LOG.exception("loop %s could not be settled", loop.loop_id)
        finally:
            # Guarded: outside a try this is the one statement in the pass that
            # can throw past `tick`, discarding the verdict pass's whole result
            # over a lease we merely failed to drop early. The lease expires on
            # its own.
            try:
                deps.store.release(loop.loop_id)
            except Exception:
                _LOG.exception("loop %s could not be released", loop.loop_id)
    return advanced


def tick(deps: LoopDeps) -> int:
    """Route every unseen verdict. Returns the number of loops advanced.

    Each verdict is isolated: one malformed row, one exploding adapter, one
    corrupt loop must not abort the sweep across every other pull request.

    A verdict is marked seen *before* it is handled, so a crash mid-handle
    consumes it rather than replaying it. That is deliberate: the side effects
    here (a dispatched lane, a Slack post, a label) are not idempotent, and a
    stalled loop the daemon's due-sweep can pick up is a far cheaper failure
    than a duplicate remediation lane.

    The verdict pass is followed by a due pass over leased due loops, which
    exists so that a write nobody will send another verdict for -- an
    escalation nobody received, a lane nobody recorded -- is tried again
    anyway.
    """
    advanced = 0
    driven: set[str] = set()
    for verdict in deps.source.new_verdicts():
        try:
            if not deps.store.mark_verdict_seen(verdict.request_id, deps.now()):
                continue
            if _handle(deps, verdict, driven):
                advanced += 1
        except CorruptLoopRow as exc:
            _LOG.exception("loop %s carries an unreadable state", exc.loop_id)
            _note_quietly(deps, exc.loop_id, "corrupt_row",
                          {"state": str(exc.state)})
        except Exception:
            _LOG.exception("verdict %s could not be routed", verdict.request_id)
    return advanced + _settle_due(deps, driven)
