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

Four invariants sit on top of those orderings.

*A paused tick decides; only a live tick acts.* Shadow (and off) is the kill
switch an operator reaches for to stop this service, and gating each routing
branch on `cfg.mode` failed twice: the ready and escalate routes went on
driving loops to a *terminal* state from the mode that exists to write nothing.
A loop a live tick had already claimed the PR for was then closed without
notifying anyone, without releasing the ownership label and without removing
the reaction -- and, being terminal, `active_for_pr` refused every later live
tick the chance to recover it.

So the mode is not asked inside the routing at all. `_route` decides what a
live tick would do and performs none of it; exactly one of two consumers
follows, `_perform` (live) or `_rehearse` (paused, which writes the decision to
the trail and stops). A branch added to `_perform` cannot leak into a paused
tick, because a paused tick never reaches `_perform`. What a shadow soak shows
is therefore one recorded decision per verdict rather than a simulated
multi-round progression: shadow exists to validate routing, not to rehearse
state.

Opening the loop row is not a state change but the record itself: without one
there is nowhere to write the decision, and the `observed` row a paused tick
leaves behind is exactly the row a later live tick would have opened for the
same verdict.

Adopting an orphaned lane is the one deliberate exception, and it is consistent
with the invariant rather than a hole in it: it is a store-only correction that
dispatches nothing, posts nothing and claims nothing, recording a lane that is
already running in the real world. Suppressing it would make a paused tick file
a round with no lane against a PR that has one -- a less truthful record than
the correction itself.

*One verdict per pull request per sweep, and the newest.* `new_verdicts` is
ordered newest-first, so within one sweep the *older* verdict for a PR would be
routed second -- against a loop the newer one may already have closed, which
`active_for_pr` then reports as no loop at all, opening a second generation
that re-claims a PR this service has just handed back. Only the first verdict a
sweep sees for a pull request drives its loop. The rest are marked seen and
written to the trail as `superseded_verdict`, and nothing stalls: the loop's own
`next_attempt_at` is untouched, so the due pass finds it exactly as it would
have.

*One lane per loop.* A round may start only while no lane is in flight -- from
`observed`, `awaiting_head_move` or `rereview_posted`, never from
`remediating` with a lane recorded, and never while a lane this loop opened but
failed to record is still outstanding. Kanban's idempotency key does not cover
the orphaned case, because that key embeds the head sha and the orphan's own
push is what moves the head -- so the trail, not the row, has to answer for it,
and the orphan is *adopted* (the lost write retried, nothing else) rather than
replaced. The in-flight lane, not a new verdict, is what drives the loop's next
step; the refused verdict is written to the trail as `verdict_deferred` rather
than silently dropped.

Adoption is a correction, never a rewind. The rule that a recorded lane
supersedes an orphan is order-dependent, and the ordering is inverted in the
only way an orphan arises: a lapsed lease means the rival records *its* lane
first and our orphan note lands after it, unsuperseded. So adoption is refused
outright while a lane is in flight (on the verdict path and in the due pass
alike), and can never lower `round_count` -- otherwise a loop that had spent
its whole budget would have rounds handed back to it.

*An escalation is not finished until a human has it.* Escalation exists
because a person has to intervene, so a notification that failed does NOT close
the loop. The loop stays non-terminal with an `escalation_pending` note, and a
later tick re-posts -- only the post, never the remediation side effects and
never another round. Retries are bounded by NOTIFY_ATTEMPTS: a permanently
misconfigured channel would otherwise pin the loop open forever, and a loop
that never goes terminal blocks every later loop on that PR
(`active_for_pr`). At the bound the loop closes with `escalation_unreachable`
in the trail and an ERROR in the log. That budget is spread across ticks by
design, so one sweep spends at most one attempt on a loop -- which the
newest-verdict rule above is what guarantees.

"Notified" and "closed" are two debts, not one: the notice is recorded before
the closing compare-and-swap (so a crash cannot double-post), which means the
close can be lost after a human already has the notice. Such a loop must keep
being retried -- the write, and the stand-down, never the post -- or it never
reaches a terminal state and `active_for_pr` blocks every future generation for
that PR. Only the *notification* is gated on `notified`: both stand-down acts
are idempotent by construction, and a settle that died between the delivery
note and the stand-down would otherwise strand the ownership label on the PR
with no tick left that would ever clear it.

Escalation reasons are free-form strings, not only `EscalationReason` members:
the enum names the loop's *own* dead ends (round cap, no-op lane), while the
driver also stops for reasons the enum does not model -- the PR changed hands,
GitHub is unwritable, the re-review has no thread to land in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
SUPERSEDED_VERDICT = "superseded_verdict"

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

# The routes a verdict can take. `_route` names one, `_perform` performs it and
# `_rehearse` records it -- so the name is both the dispatch key and the word a
# shadow soak reads back. Three of them (held, author_unreadable,
# unknown_outcome) are observations rather than acts: they are their own note
# kind, and a live tick does no more with them than record it.
ROUTE_SETTLE_ESCALATION = "settle_escalation"
ROUTE_HELD = "held"
ROUTE_AUTHOR_UNREADABLE = "author_unreadable"
ROUTE_UNKNOWN_OUTCOME = "unknown_outcome"
ROUTE_ESCALATE = "escalate"
ROUTE_NOTIFY_READY = "notify_ready"
ROUTE_REPOST_REREVIEW = "repost_rereview"
ROUTE_ADOPT_LANE = "adopt_lane"
ROUTE_OPEN_LANE = "open_lane"
ROUTE_DEFER = "defer_verdict"

_OBSERVATIONS = (ROUTE_HELD, ROUTE_AUTHOR_UNREADABLE, ROUTE_UNKNOWN_OUTCOME)

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


@dataclass(frozen=True)
class Route:
    """What a live tick would do with one verdict, decided before it is done.

    Deciding and doing are split so that the operating mode is asked exactly
    once, outside the routing: a live tick performs the route, a paused one
    records it. `detail` is written to the trail verbatim, so it must stay
    JSON-shaped; `debt` is the replayed trail the escalation actor would
    otherwise have to read a second time, and is deliberately not recorded.

    `defer`, when set, is why this verdict is not itself driving the loop --
    the in-flight lane is, or a debt the loop already owes is. It is recorded
    in both modes, because the verdict is marked seen either way and this note
    is the only surviving trace that it existed.
    """
    action: str
    detail: dict = field(default_factory=dict)
    defer: str | None = None
    debt: EscalationDebt | None = None


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

    Both are also idempotent by construction, which is what lets every settle
    run them: removing a reaction that is gone and releasing a label that is
    already released are no-ops, and a settle interrupted midway would
    otherwise leave the PR looking owned with nothing left to clear it.

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
    The price of that ordering is two writes that can be lost after the notice
    landed -- the stand-down and the close -- which is what `notified` is for:
    it gates the *post* only, so a loop that has been heard but not closed
    comes back here and retries both of those, and neither is anything a human
    could see twice.
    """
    if not debt.notified:
        if deps.slack.notify_escalation(verdict, loop.round_count,
                                        debt.reason) is None:
            attempt = debt.attempts + 1
            if attempt < NOTIFY_ATTEMPTS:
                deps.store.record_note(loop.loop_id, EVENT_ESCALATION_PENDING,
                                       {"reason": debt.reason, "attempt": attempt},
                                       deps.now())
                # Nobody has the notice, so the PR stays owned and the reaction
                # stays on: the loop is still open and will be retried.
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
    # A debt with nothing paid on it yet: a loop that already owes one is
    # intercepted before it can reach here, on the verdict path and in the due
    # pass alike, so this is always the first attempt at this escalation.
    return _settle_escalation(deps, loop, verdict,
                              EscalationDebt(reason=reason, attempts=0,
                                             notified=False))


def _finish_ready(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    if deps.slack.notify_ready(verdict, loop.round_count) is None:
        deps.store.record_note(loop.loop_id, "notify_failed",
                               {"kind": "ready"}, deps.now())
    _stand_down(deps, loop, verdict)
    return _transition(deps, loop, LoopState.READY)


def _repost(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """A system-origin incomplete is Boris failing, not the PR failing.

    Retrying the review costs no remediation round: nothing was remediated.
    """
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
    running (a hand-edited row, or one an older build's shadow round left
    behind). Treating that as in-flight would strand it forever, because the
    lane pass has no lane to poll and would never move it on.

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
    points at now is what is running. That rule reads the trail in order and is
    therefore only half the answer -- an orphan written *after* a rival's
    transition is never superseded by it -- so callers must additionally refuse
    to adopt while a lane is in flight, and `_adopt_lane` refuses to rewind.
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

    The round the orphan spent is the round recorded, but never a *lower* one
    than the row already carries. An orphan note can outlive the round it
    describes (it is written after the rival transition that supersedes it in
    trail order, so nothing ever clears it), and adopting it at face value
    would hand a loop that had exhausted its round cap its whole budget back.
    Callers must also refuse to adopt while a lane is in flight; this floor is
    the second half of that guard, not a substitute for it.
    """
    spent = int(orphan.get("round") or loop.round_count + 1)
    return _transition(
        deps, loop, LoopState.REMEDIATING,
        round_count=max(spent, loop.round_count),
        lane_id=str(orphan["lane_id"]),
        head_sha=str(orphan.get("head_sha") or loop.head_sha),
        next_attempt_at=deps.now() + LANE_POLL_SECONDS)


def _open_lane(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
               round_number: int) -> bool:
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
        deps.slack.add_thinkies(deps.cfg.boris_channel, loop.root_thread_ts)
    return True


def _route(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> Route:
    """Decide what a live tick would do with this verdict, and do none of it.

    The whole routing decision lives here, and nothing here writes loop state,
    posts, labels or dispatches. Only GitHub *reads* happen -- the hold label
    and the author -- because a decision taken without them would not be the
    decision a live tick takes, and a shadow soak exists to check exactly that.
    """
    cfg = deps.cfg

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
        return Route(ROUTE_SETTLE_ESCALATION,
                     {"reason": debt.reason, "notified": debt.notified},
                     defer=ESCALATION_OWED, debt=debt)

    if deps.github.has_hold(verdict.owner, verdict.repo, verdict.pr_number):
        # Chad's kill switch, and also the fail-closed answer when GitHub
        # cannot be read at all.
        return Route(ROUTE_HELD, {"label": cfg.hold_label})
    author = deps.github.author(verdict.owner, verdict.repo, verdict.pr_number)
    if author is None:
        return Route(ROUTE_AUTHOR_UNREADABLE, {})
    if author != cfg.chad_github_login:
        return Route(ROUTE_ESCALATE, {"reason": NOT_CHAD_AUTHORED})

    if verdict.outcome == "READY_FOR_HUMAN_REVIEW":
        return Route(ROUTE_NOTIFY_READY, {"rounds": loop.round_count})
    if verdict.outcome == "REVIEW_INCOMPLETE":
        if verdict.incomplete_origin != "system":
            return Route(ROUTE_ESCALATE,
                         {"reason": EscalationReason.MODEL_INCOMPLETE.value})
        if not loop.root_thread_ts:
            # SlackIO refuses to post outside a thread because the intake gate
            # silently drops such a message. No thread means no route back into
            # review, ever -- that is permanent, so escalate rather than retry.
            return Route(ROUTE_ESCALATE, {"reason": REREVIEW_UNPOSTABLE})
        return Route(ROUTE_REPOST_REREVIEW, {"round": loop.round_count + 1})
    if verdict.outcome != "ISSUES_FOUND":
        return Route(ROUTE_UNKNOWN_OUTCOME, {"outcome": verdict.outcome})

    if _lane_in_flight(loop):
        # A second lane on this branch is the collision this service exists to
        # remove, and here we would be causing it ourselves. The lane already
        # running drives the next step: it finishes, the head moves, the
        # re-review goes out, and *that* verdict starts the next round. Checked
        # before the round cap, because the round this verdict would spend has
        # already been spent by the lane in flight.
        return Route(ROUTE_DEFER, {"lane_id": loop.lane_id},
                     defer=LANE_IN_FLIGHT)
    orphan = _orphaned_lane(deps, loop)
    if orphan is not None:
        # The row says no lane, the trail says otherwise, and the trail is
        # right: this loop's own dispatch lost its recording write. Adopt that
        # lane rather than open a second one against the same branch.
        return Route(ROUTE_ADOPT_LANE, dict(orphan), defer=LANE_ORPHANED)
    if loop.round_count >= cfg.round_cap:
        return Route(ROUTE_ESCALATE,
                     {"reason": EscalationReason.ROUND_CAP.value})
    return Route(ROUTE_OPEN_LANE, {"round": loop.round_count + 1})


def _perform(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
             route: Route) -> bool:
    """Do what `_route` decided. Reached only in live mode."""
    if route.defer is not None:
        _defer_verdict(deps, loop, verdict, route.defer)
    if route.action == ROUTE_SETTLE_ESCALATION:
        if route.debt is None:
            # Unreachable via `_route`, and a silent `return False` here would
            # look exactly like a loop with nothing owing.
            raise ValueError("settle_escalation route carries no debt")
        return _settle_escalation(deps, loop, verdict, route.debt)
    if route.action in _OBSERVATIONS:
        deps.store.record_note(loop.loop_id, route.action, route.detail,
                               deps.now())
        return False
    if route.action == ROUTE_ESCALATE:
        return _escalate(deps, loop, verdict, str(route.detail["reason"]))
    if route.action == ROUTE_NOTIFY_READY:
        return _finish_ready(deps, loop, verdict)
    if route.action == ROUTE_REPOST_REREVIEW:
        return _repost(deps, loop, verdict)
    if route.action == ROUTE_ADOPT_LANE:
        return _adopt_lane(deps, loop, route.detail)
    if route.action == ROUTE_OPEN_LANE:
        return _open_lane(deps, loop, verdict, int(route.detail["round"]))
    # ROUTE_DEFER: the in-flight lane drives the loop, not this verdict, and
    # the note above is the whole of what we owe it.
    return False


def _rehearse(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
              route: Route) -> bool:
    """Record what a live tick would have done, and change no loop state.

    The kill switch, honoured structurally: a paused tick decides and stops,
    so no routing branch can leak a post, a label or a terminal state past it.
    Adoption is the one exception, for the reason the module docstring gives --
    it is a store-only correction recording a lane that is already running, and
    refusing it would file a round with no lane instead.
    """
    _shadow(deps, loop.loop_id, route.action, route.detail)
    if route.defer is not None:
        _defer_verdict(deps, loop, verdict, route.defer)
    if route.action == ROUTE_ADOPT_LANE:
        return _adopt_lane(deps, loop, route.detail)
    return False


def _drive(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """Route one verdict against a loop we hold the lease on.

    The operating mode is asked here and nowhere below: one of two consumers
    takes the decision, and only one of them can act.
    """
    route = _route(deps, loop, verdict)
    if deps.cfg.mode != "live":
        return _rehearse(deps, loop, verdict, route)
    return _perform(deps, loop, verdict, route)


def _handle(deps: LoopDeps, verdict: Verdict,
            driven: set[str]) -> tuple[bool, str | None]:
    """Drive one verdict, returning whether it advanced its loop and which one.

    The loop id goes back to `tick` so that an older verdict for the same pull
    request has somewhere to be recorded even after the newest one has closed
    the loop -- by which point `active_for_pr` can no longer find it.
    """
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
        return False, None

    # The due pass must not touch a loop this pass has already worked on this
    # tick. Recorded whatever happens below, a refused claim included -- that
    # loop is being driven, just not by us.
    driven.add(loop.loop_id)
    if not store.claim(loop.loop_id, now=deps.now()):
        # Leased elsewhere, or already terminal. Either way, not ours to drive.
        return False, loop.loop_id
    try:
        # Re-read under the lease: the snapshot above was taken unleased, so
        # its state and round_count may already be stale, and driving from a
        # stale round_count is how a round gets spent twice.
        leased = store.get(loop.loop_id)
        if leased is None:
            return False, loop.loop_id
        return _drive(deps, leased, verdict), loop.loop_id
    finally:
        store.release(loop.loop_id)


def _record_superseded(deps: LoopDeps, verdict: Verdict,
                       loop_id: str | None) -> None:
    """Note an older verdict for a pull request this sweep has already driven.

    It is marked seen like any other, so this note is the only trace it leaves;
    it drives nothing, and it stalls nothing either, because the loop's own
    `next_attempt_at` is untouched by a note.
    """
    loop = deps.store.get(loop_id) if loop_id else None
    if loop is None:
        # The driving verdict never got as far as a loop (a lost open_loop
        # race, or a throw before it had one). Whatever row it would have found
        # is still the right place for this, if there is one at all.
        loop = deps.store.active_for_pr(verdict.owner, verdict.repo,
                                        verdict.pr_number)
    if loop is None:
        _LOG.warning("superseded verdict %s has no loop to record it against",
                     verdict.request_id)
        return
    _defer_verdict(deps, loop, verdict, SUPERSEDED_VERDICT)


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
            if _lane_in_flight(loop):
                # The same guard the verdict path applies, and for the same
                # reason: an orphan note that trails a rival's transition is
                # never superseded, so without this the pass would rewrite a
                # live lane (and its round) with a stale one. Deliberately
                # silent -- this pass runs every tick, and a note here would
                # grow the trail forever for a loop that is perfectly healthy.
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

    At most one verdict per pull request is routed, the newest -- `sweep`
    remembers which pull requests this tick has driven, and which loop each of
    them belongs to. See the module docstring: an older verdict routed second
    would drive a loop the newer one has already closed, and open a second
    generation once `active_for_pr` stops finding it.

    The verdict pass is followed by a due pass over leased due loops, which
    exists so that a write nobody will send another verdict for -- an
    escalation nobody received, a lane nobody recorded -- is tried again
    anyway.
    """
    advanced = 0
    driven: set[str] = set()
    sweep: dict[tuple[str, str, int], str | None] = {}
    for verdict in deps.source.new_verdicts():
        pr = (verdict.owner, verdict.repo, verdict.pr_number)
        try:
            if not deps.store.mark_verdict_seen(verdict.request_id, deps.now()):
                continue
            if pr in sweep:
                _record_superseded(deps, verdict, sweep[pr])
                continue
            # Claimed before it is driven, so a verdict that throws mid-handle
            # still bars an older one for the same PR from driving the loop.
            sweep[pr] = None
            handled, loop_id = _handle(deps, verdict, driven)
            sweep[pr] = loop_id
            if handled:
                advanced += 1
        except CorruptLoopRow as exc:
            _LOG.exception("loop %s carries an unreadable state", exc.loop_id)
            _note_quietly(deps, exc.loop_id, "corrupt_row",
                          {"state": str(exc.state)})
        except Exception:
            _LOG.exception("verdict %s could not be routed", verdict.request_id)
    return advanced + _settle_due(deps, driven)
