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

# Stop reasons outside EscalationReason's vocabulary. See the module docstring.
NOT_CHAD_AUTHORED = "not_chad_authored"
GITHUB_UNWRITABLE = "github_unwritable"
REREVIEW_UNPOSTABLE = "rereview_unpostable"

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


def _escalate(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
              reason: str) -> bool:
    if deps.cfg.mode == "live":
        if deps.slack.notify_escalation(verdict, loop.round_count, reason) is None:
            # The human was not told. Still close the loop -- leaving it open
            # would re-run every side effect on the next sweep -- but leave the
            # silence in the trail, because nothing else will report it.
            deps.store.record_note(loop.loop_id, "notify_failed",
                                   {"kind": "escalation", "reason": reason},
                                   deps.now())
        _stand_down(deps, loop, verdict)
    else:
        _shadow(deps, loop.loop_id, "escalate", {"reason": reason})
    return _transition(deps, loop, LoopState.ESCALATED, escalation_reason=reason)


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


def _remediate(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    cfg = deps.cfg
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
        deps.store.record_note(loop.loop_id, "orphaned_lane",
                               {"lane_id": lane_id, "round": round_number},
                               deps.now())
        return False
    if loop.root_thread_ts:
        deps.slack.add_thinkies(cfg.boris_channel, loop.root_thread_ts)
    return True


def _drive(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """Route one verdict against a loop we hold the lease on."""
    store, cfg = deps.store, deps.cfg

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


def _handle(deps: LoopDeps, verdict: Verdict) -> bool:
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
        return _drive(deps, leased, verdict)
    finally:
        store.release(loop.loop_id)


def _note_quietly(deps: LoopDeps, loop_id: str, kind: str, detail: dict) -> None:
    """Best-effort forensics from an error path, which must not itself raise."""
    try:
        deps.store.record_note(loop_id, kind, detail, deps.now())
    except Exception:
        _LOG.exception("could not record %s for loop %s", kind, loop_id)


def tick(deps: LoopDeps) -> int:
    """Route every unseen verdict. Returns the number of loops advanced.

    Each verdict is isolated: one malformed row, one exploding adapter, one
    corrupt loop must not abort the sweep across every other pull request.

    A verdict is marked seen *before* it is handled, so a crash mid-handle
    consumes it rather than replaying it. That is deliberate: the side effects
    here (a dispatched lane, a Slack post, a label) are not idempotent, and a
    stalled loop the daemon's due-sweep can pick up is a far cheaper failure
    than a duplicate remediation lane.
    """
    advanced = 0
    for verdict in deps.source.new_verdicts():
        try:
            if not deps.store.mark_verdict_seen(verdict.request_id, deps.now()):
                continue
            if _handle(deps, verdict):
                advanced += 1
        except CorruptLoopRow as exc:
            _LOG.exception("loop %s carries an unreadable state", exc.loop_id)
            _note_quietly(deps, exc.loop_id, "corrupt_row",
                          {"state": str(exc.state)})
        except Exception:
            _LOG.exception("verdict %s could not be routed", verdict.request_id)
    return advanced
