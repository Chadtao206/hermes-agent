"""The remediation loop tick: verdict routing and round accounting.

Two orderings carry the whole design.

*Reads before writes.* The hold label and the live author are checked before
anything is written, so a PR Chad has taken back is never touched. `has_hold`
fails closed (an unreadable GitHub is a hold), and an unreadable author is
treated as "unknown", not as "reassigned" -- escalating a transient read blip
would burn the loop permanently on a network hiccup. The pull request's own
state is read in that same breath, because a PR merged or closed between the
verdict and the tick is one every act below would aim at a branch nobody can
push to any more: it is closed as `stale` -- silently, since a merge is a
human's own act and not an incident -- with the ownership label and the
reaction handed back all the same.

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
tick the chance to recover it. A third gate then survived in the due pass,
where an ordinary early return was the only thing between an `off` tick and a
Slack post, a released label and a loop driven terminal.

So the mode is not asked inside the routing at all, and no pass asks it for
itself. `_route` (a verdict) and `_route_due` (a debt no verdict will carry)
decide what a live tick would do and perform none of it; `_consumer` then names
the one consumer this mode allows, `_perform` (live) or `_rehearse` (paused,
which writes the decision to the trail and stops). That single line is the
whole of the module's mode-awareness, both passes go through it, and a branch
added to `_perform` cannot leak into a paused tick because a paused tick never
reaches `_perform`. What a shadow soak shows is therefore one recorded decision
per verdict rather than a simulated multi-round progression: shadow exists to
validate routing, not to rehearse state. The due pass's decisions are
`standing` -- retaken every tick until the debt is paid -- so a paused tick
records each of them once instead of appending the same sentence to the trail
forever.

`off` goes one step further than shadow: it is the hard kill switch, so it
stops the work and not merely the writes. A tick in it reads no GitHub at all
(the reads live in `_observe`, which the mode decides whether to call), and
`_route` is handed no facts to route on -- so past the debts the store alone
can answer, it says `unobserved` rather than guessing at a decision it did not
take. Shadow keeps reading, because rehearsing a decision that was taken
without them would not be rehearsing the decision a live tick takes.

Opening the loop row is not a state change but the record itself: without one
there is nowhere to write the decision. It is not a row a later live tick
inherits the verdict for, though -- `tick` marks a verdict seen before the mode
is consulted, so the verdict that opened the row is consumed for good and can
never be routed live. What a paused tick leaves behind is a record of a
decision nobody acted on, which is why the rollout clears the database at the
shadow-to-live cutover rather than expecting live to pick these rows up.

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

Adoption is a correction, never a rewind, and that has to hold for every field
it writes. The rule that a recorded lane supersedes an orphan is
order-dependent, and the ordering is inverted in the only way an orphan arises:
a lapsed lease means the rival records *its* lane first and our orphan note
lands after it, unsuperseded. So what makes a note adoptable is the *row*, not
the trail's order: a row that carries no lane has a lost write to retry, and a
row that already records one has nothing to correct -- adopting there would
point the loop at a dead lane, rewind `head_sha` to that lane's, and drop the
row back into `remediating`, where the one-lane rule then makes every later
pass skip it forever. Such a claim is refused and *deleted* (`_drop_orphan`),
because a note nothing supersedes would otherwise come back the moment the
row's lane is cleared. Adoption is refused outright while a lane is in flight
as well, and can never lower `round_count` -- otherwise a loop that had spent
its whole budget would have rounds handed back to it.

*A round ends when the head moves, not when the lane exits.* The lane in
flight is what drives a `remediating` loop, and the due pass is what watches
it: kanban is polled for that lane's status, and once it is terminal the pull
request itself is read to see whether anything was actually pushed. A moved
head earns a re-review post and, with it, the next verdict; an unmoved one
does not, because re-reviewing an unchanged pull request reproduces the same
verdict and spends another round arriving at it.

"Unmoved" is a judgement about a race, though, so it is not taken on the
first look. Kanban marks a lane terminal when its worker exits, which can be
moments before GitHub serves the pushed head. `awaiting_head_move` is that
one grace window, and the store's transition table gives the state no
self-edge on purpose: a loop enters it at most once per round -- a verdict
arriving inside the window starts the next round fresh, with its own single
entry, so this is not an invariant over the loop's whole life, only over one
round's -- and the next look either finds the push or gives up as
`no_op_lane`. A head GitHub could not read at all is
neither answer -- like an unreadable author it is recorded and retried,
because escalating a network blip would burn the loop permanently. Which
state the loop is in is also what says whether there is a lane to complete at
all: only `remediating` (with a lane) and `awaiting_head_move` are waiting on
one, so a row that still names the lane of a round already closed out is
never read back as a second completion.

All of that presupposes a lane kanban has already called finished, which is a
thing a lane can simply never be. A wedged `running` card, or a card a human
deleted (`lane_status()` then answers None), reaches none of the bounds below
because none of them start counting until the lane is over. LANE_WAIT_SECONDS
is the one bound that does not wait for that, measured from when the loop
entered its current state, and it escalates as `lane_timeout` -- a third dead
end, distinct from the two the next paragraph separates, and pointing a human
at the kanban card rather than at the branch or at GitHub.

That retry is bounded by HEAD_READ_ATTEMPTS, the same shape as NOTIFY_ATTEMPTS
below and for the same reason: a blip is worth riding out, but a PR that was
deleted, a repo that was renamed or transferred, or a token that lost its
scope reads exactly like a blip on every single tick, and nothing else would
ever end the wait -- leaving the loop non-terminal blocks every later
generation of this PR (`active_for_pr`) behind a human who was never told.
At the bound the loop escalates as `head_read_exhausted` rather than
`no_op_lane`: the two are not the same dead end. `no_op_lane` means the read
succeeded and the lane genuinely pushed nothing, which asks a human to accept
the round as spent. `head_read_exhausted` means the read itself never landed,
which asks a human to go look at GitHub rather than at the branch --
conflating the two would send them looking for a push that was never the
problem.

The hold label is read before any of that is acted on, because all of it
writes -- a reaction removed, a re-review posted, an escalation notice, the
ownership label handed back. `has_hold` fails closed, so a GitHub this pass
cannot read is a hold and the pass writes nothing at all. The escalation
retry is the one due-pass route that does not read it, and deliberately: what
it does is notify a human and (`lane_timeout` against a lane not confirmed
finished excepted -- see below) stand down the signals this loop itself
added, which is what a hold wants to happen anyway.

A loop that closes while its lane is still running is the one thing neither
pass can repair. Terminal states are absorbing and unclaimable, so that lane
will finish, push to a pull request this service has just handed back, and be
polled by nobody -- and there is no cancel to call for it from here. Both
shapes of it (the lane the row records, and the lane whose recording write
was lost) are therefore logged and written to the trail as `lane_abandoned`,
so the human who has just been told the PR is ready, or stuck, can find the
lane still moving underneath them.

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

*A live lane keeps its claim.* `lane_timeout` is a bound on how long this loop
will wait, not a bound on how long a lane may run -- kanban's own stale-lane
reclaim (`dispatch_stale_timeout_seconds`) measures silence, a heartbeat gap,
never duration, so a lane that keeps heartbeating can run past
LANE_WAIT_SECONDS while genuinely healthy. Standing down in that case --
releasing the ownership label, clearing the reaction -- would be exactly the
collision this service exists to prevent: a human takes `hermes-owner-andrej`
coming off as permission to touch the branch, while a worker in a worktree
this loop cannot cancel is still able to commit and push to it. So a
`lane_timeout` settle checks the lane fresh right before it would otherwise
stand down (`_lane_still_running`), and a lane not confirmed finished --
running, or a status the read could not resolve -- keeps both signals and
says so in the notice, naming the lane. A status kanban itself calls terminal
still stands down exactly as before; nothing about that path changes. Every
other escalation reason is untouched by this -- the exception is keyed on the
reason string, not on whether a lane happens to still be recorded on the row.

The label a live-lane escalation leaves behind is never cleared by a later
tick: the loop that would have cleared it is terminal, and closing it is the
only lever this service has over a lane it cannot cancel. That is accepted on
purpose -- a stale label a human was explicitly told about is a smaller harm
than a missing one that lets a second agent start working a branch a live
worker is still pushing to -- which is exactly why the notice has to say so.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig
from pr_remediation.lanes import TERMINAL_LANE_STATUSES
from pr_remediation.models import EscalationReason, LoopRequest, LoopState
from pr_remediation.store import CorruptLoopRow

# A freshly created lane must not be inspected on the same tick that created it:
# claim_due would find it, see a not-yet-started lane, and escalate it as a no-op.
LANE_POLL_SECONDS = 15

# The single grace window a finished lane gets for its push to become visible.
# Kanban marks a lane terminal when its worker exits, which can be moments
# before GitHub serves the pushed head, so the first unchanged read is not yet
# evidence that the lane did nothing. Long enough to ride out that gap, short
# enough that a genuinely empty round is not left looking live for minutes --
# and taken exactly once, because `awaiting_head_move` has no self-edge.
HEAD_MOVE_GRACE_SECONDS = 60.0

# How many consecutive due-pass ticks a lane's head may come back unreadable
# before this loop gives up waiting and escalates. A different problem from
# the grace window above: that one assumes the read itself succeeds and only
# the push is late, which its single one-shot wait is built to survive; this
# is for when GitHub cannot even answer the question, which no fixed wait
# survives on its own. Mirrors NOTIFY_ATTEMPTS below for the same reason and
# at the same value: high enough to ride out a GitHub outage across several
# ticks, low enough that a head that is unreadable for a real reason -- the
# PR deleted, the repo renamed or transferred, a token that lost its scope --
# cannot pin this PR's lane loop, and with it every later generation
# (`active_for_pr`), open forever.
HEAD_READ_ATTEMPTS = 5

# How long a lane may sit without reaching a terminal kanban status before this
# loop stops waiting for it. The one bound that does not presuppose a finished
# lane: every other one here (the grace window, the head-read budget) only
# starts counting once kanban has already said the lane is over, so a lane that
# never says so -- a wedged `running` card, or a card a human deleted, which
# `lane_status()` reports as None -- outlived all of them and wedged its pull
# request permanently while this pass polled kanban every DUE_BACKOFF_SECONDS
# forever.
#
# boris_intake bounds exactly this shape with REVIEW_WAIT_SECONDS = 90 minutes
# (reconciler.py's REVIEW_LANE_TIMEOUT), added after a real incident. Three
# hours here, deliberately twice that, for two reasons that both point the same
# way: the sibling bounds a lane that *reads* a diff and returns a structured
# judgement, while this one bounds a lane that writes code, runs a test suite
# and pushes -- strictly more work at the same worker cadence -- and this lane
# is dispatched with `--max-retries 1`, so kanban may legitimately run the work
# twice inside one card's life. A bound tighter than twice the sibling's would
# cut a legitimate second attempt off mid-flight and escalate a lane that was
# about to succeed. It is measured from `updated_at`, which is when the loop
# entered its current state -- the lane's dispatch, or the grace window's
# opening -- and which nothing but a real transition moves (`store.defer`
# deliberately leaves it alone).
LANE_WAIT_SECONDS = 180 * 60

# How long a round may run with no terminal lane status before the due pass
# says, once, that it is still going. Not a bound like LANE_WAIT_SECONDS
# above -- it escalates nothing, closes nothing, and changes no loop state --
# it exists because the loop's own signals while a round runs (the ownership
# label, the thinkies reaction) look identical whether that round is working
# normally or wedged. The first live ISSUES_FOUND run sat silent behind
# exactly those two signals for twenty minutes with nothing to tell them
# apart. Thirty minutes, well under LANE_WAIT_SECONDS (three hours): a human
# who has learned to expect this notice and then hears nothing further still
# has more than two hours of genuine headroom before the loop gives up on its
# own, so the silence past this notice stays informative rather than another
# instance of the same ambiguity this notice exists to remove.
STILL_RUNNING_THRESHOLD_SECONDS = 30 * 60

# How many times a single escalation may be posted before the loop closes
# unheard. Chosen high enough to ride out a Slack outage across several ticks,
# low enough that a misconfigured channel cannot hold the PR's loop open (and
# with it every later loop for that PR) indefinitely.
NOTIFY_ATTEMPTS = 5

# Loops the retry pass leases per tick.
DUE_LIMIT = 20

# How far out the due pass pushes a loop it has just visited and left open.
#
# Matched to the daemon's poll interval, because the fastest thing this pass
# waits on -- a lane in flight -- wants looking at once a tick and no oftener.
# It is deliberately NOT what makes the pass fair: fairness comes from
# `claim_due` ordering on `next_attempt_at`, so a loop just visited sorts behind
# one that has been waiting however small this number is. Tying fairness to a
# backoff longer than the tick interval would be a coincidence between two
# constants nothing enforces, and the pass would starve again the day someone
# tuned `poll_seconds` up.
DUE_BACKOFF_SECONDS = 15.0

# Stop reasons outside EscalationReason's vocabulary. See the module docstring.
NOT_CHAD_AUTHORED = "not_chad_authored"
GITHUB_UNWRITABLE = "github_unwritable"
REREVIEW_UNPOSTABLE = "rereview_unpostable"
# The pull request stopped being open. Serves as both the trail's note kind and
# the reason stamped on the closed row, because here they say the same thing --
# unlike the defer/event vocabularies below, which read alike but are matched by
# different code.
PR_NOT_OPEN = "pr_not_open"
# The one `gh pr view --json state` value that means there is still something to
# remediate. MERGED and CLOSED are both ends of the road.
PR_OPEN = "OPEN"
# The read, not the lane, is what gave up -- distinct from
# EscalationReason.NO_OP_LANE, which means the read succeeded and found no
# push. Conflating them would send whoever is escalated to looking for a push
# that was never the problem.
HEAD_READ_EXHAUSTED = "head_read_exhausted"
# The lane never reached a terminal kanban status at all -- a third dead end,
# and a third thing to tell a human to go and look at. `no_op_lane` says the
# lane finished and pushed nothing (look at the branch); `head_read_exhausted`
# says the lane finished and GitHub would not say what it pushed (look at
# GitHub); this says the lane never finished (look at the kanban card, which may
# be wedged, or may simply be gone).
LANE_TIMEOUT = "lane_timeout"

# Written on a lane_timeout settle that finds the lane not confirmed finished
# by kanban's own account and therefore, deliberately, releases neither the
# ownership label nor the thinkies reaction. See _settle_escalation and
# _lane_still_running.
LANE_TIMEOUT_LANE_ALIVE = "lane_timeout_lane_alive"

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
# The end of an orphan's claim on a loop, written when the row it would correct
# turns out to record a lane of its own. The trail's own superseding rule
# cannot reach that case: the note lands *after* the transition that overtook
# it, so nothing in trail order ever clears it.
EVENT_ORPHAN_SUPERSEDED = "orphan_superseded"
# A lane left running by a loop that has just closed. Nothing polls it, nothing
# can cancel it from here, and it is about to push to a pull request this
# service has handed back -- so the only thing left is to say where it went.
EVENT_LANE_ABANDONED = "lane_abandoned"
# Written by store.transition() itself, and only when the swap landed.
EVENT_TRANSITION = "transition"

# The routes a decision can take, whether a verdict or a due debt prompted it.
# `_route` and `_route_due` name one, `_perform` performs it and `_rehearse`
# records it -- so the name is both the dispatch key and the word a shadow soak
# reads back. Four of them (held, author_unreadable, unknown_outcome, and
# unobserved, which is the absence of an observation rather than one) are not
# acts at all: they are their own note kind, and a live tick does no more with
# them than record it.
ROUTE_SETTLE_ESCALATION = "settle_escalation"
ROUTE_HELD = "held"
ROUTE_AUTHOR_UNREADABLE = "author_unreadable"
ROUTE_HEAD_UNREADABLE = "head_unreadable"
ROUTE_UNKNOWN_OUTCOME = "unknown_outcome"
ROUTE_UNOBSERVED = "unobserved"
ROUTE_ESCALATE = "escalate"
ROUTE_CLOSE_STALE = "close_stale"
ROUTE_NOTIFY_READY = "notify_ready"
ROUTE_REPOST_REREVIEW = "repost_rereview"
ROUTE_LANE_PUSHED = "lane_pushed"
ROUTE_AWAIT_HEAD_MOVE = "await_head_move"
# A round still in flight, well past STILL_RUNNING_THRESHOLD_SECONDS. Not an
# observation (_OBSERVATIONS below): unlike those, a live tick does something
# with it beyond recording -- it posts once to the review thread -- so it
# gets its own branch in `_perform` rather than folding into that tuple.
ROUTE_STILL_RUNNING = "still_running"
ROUTE_ADOPT_LANE = "adopt_lane"
ROUTE_DROP_ORPHAN = "drop_orphan"
ROUTE_OPEN_LANE = "open_lane"
ROUTE_DEFER = "defer_verdict"

_OBSERVATIONS = (ROUTE_HELD, ROUTE_AUTHOR_UNREADABLE, ROUTE_HEAD_UNREADABLE,
                 ROUTE_UNKNOWN_OUTCOME, ROUTE_UNOBSERVED)

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
    """What a live tick would do about one loop, decided before it is done.

    Deciding and doing are split so that the operating mode is asked exactly
    once, outside the routing: a live tick performs the route, a paused one
    records it. `detail` is written to the trail verbatim, so it must stay
    JSON-shaped; `debt` is the replayed trail the escalation actor would
    otherwise have to read a second time, and is deliberately not recorded.

    `defer`, when set, is why this verdict is not itself driving the loop --
    the in-flight lane is, or a debt the loop already owes is. It is recorded
    in both modes, because the verdict is marked seen either way and this note
    is the only surviving trace that it existed.

    `standing` marks a decision that will be taken again, unchanged, on the
    next tick: the due pass's debts, never a verdict's route. One verdict is
    one decision and a shadow soak counts them, but a debt rehearsed once a
    tick for as long as the service stays paused would append the same sentence
    to a trail every later tick reads back in full.
    """
    action: str
    detail: dict = field(default_factory=dict)
    defer: str | None = None
    debt: EscalationDebt | None = None
    standing: bool = False


@dataclass(frozen=True)
class PrFacts:
    """What GitHub says about the pull request, read once before any decision.

    Hoisted out of the routing so that *whether this tick reads GitHub at all*
    is a question about the operating mode, asked where the mode is asked and
    nowhere else. `author` is None both when GitHub could not be read and when
    the PR is held -- a held PR's author is moot and is deliberately not
    fetched -- so `held` must be consulted first.

    `pr_state` is what `gh` calls the pull request -- OPEN, MERGED or CLOSED --
    and None when it could not be read, or was not asked for because the PR is
    held. It is read before any write for the same reason the hold and the
    author are: a pull request merged between a verdict and the tick that acts
    on it is one every act below would aim at a branch nobody can push to any
    more.
    """
    held: bool
    author: str | None
    pr_state: str | None = None


@dataclass(frozen=True)
class LaneFacts:
    """What kanban and GitHub say about the lane a loop is waiting on.

    Hoisted out of the routing for the same reason as `PrFacts`: whether this
    tick looks at the outside world at all is a question about the operating
    mode, and the mode is asked outside the routing.

    `held` and `head` are read only once the lane has finished, because that is
    the only point at which this pass writes anything -- so a hold that was
    never asked about is None here, and never False. A `status` this build does
    not recognise (kanban unreachable, the lane deleted) is not finished: like
    an unreadable author it leaves the loop where it is rather than escalating
    on a blip.
    """
    status: str | None
    held: bool | None = None
    head: str | None = None
    pr_state: str | None = None

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_LANE_STATUSES


def _observe(deps: LoopDeps, verdict: Verdict) -> PrFacts:
    """Read the PR's hold label and author. The only network in a routing pass."""
    if deps.github.has_hold(verdict.owner, verdict.repo, verdict.pr_number):
        return PrFacts(held=True, author=None)
    return PrFacts(held=False,
                   pr_state=deps.github.state(verdict.owner, verdict.repo,
                                              verdict.pr_number),
                   author=deps.github.author(verdict.owner, verdict.repo,
                                             verdict.pr_number))


def _observe_lane(deps: LoopDeps, loop: LoopRequest) -> LaneFacts | None:
    """Read the lane this loop waits on, and the PR once that lane has finished.

    None means the loop is not waiting on a lane at all, which is most of them:
    the due pass revisits every unsettled loop on every tick, so the cheap local
    kanban read is taken only for the loops a lane could still be running for,
    and the two GitHub reads only once one has actually finished. That ordering
    is also what makes the hold label a read-before-write: the lane's status is
    the last question that can be answered without writing anything.
    """
    if not _lane_pending(loop):
        return None
    status = deps.lanes.status(loop.lane_id)
    if status not in TERMINAL_LANE_STATUSES:
        return LaneFacts(status=status)
    if deps.github.has_hold(loop.owner, loop.repo, loop.pr_number):
        return LaneFacts(status=status, held=True)
    return LaneFacts(status=status, held=False,
                     pr_state=deps.github.state(loop.owner, loop.repo,
                                                loop.pr_number),
                     head=deps.github.head_sha(loop.owner, loop.repo,
                                               loop.pr_number))


def _repeats_last_note(deps: LoopDeps, loop: LoopRequest, kind: str,
                       detail: dict) -> bool:
    """Has this exact note just been written, with nothing since?

    A `standing` decision is retaken every tick until whatever prompted it is
    settled, so recording it every time appends the same sentence to a trail
    that every later tick reads back in full. "Just been written" lasts only as
    long as nothing else happens to the loop -- any event appended after the
    note makes the next recording a new fact again, so a pass that failed at
    its one permitted act is never mistaken for one already recorded.
    """
    trail = deps.store.events(loop.loop_id)
    return bool(trail) and trail[-1]["kind"] == kind and trail[-1]["detail"] == detail


def _note_standing(deps: LoopDeps, loop: LoopRequest, kind: str,
                   detail: dict) -> None:
    """Record a note unless it is already the last thing this loop said.

    For the facts the due pass rediscovers on every tick: a hold nobody has
    lifted, a GitHub still unreadable, a Slack still refusing the re-review.
    Recorded once each, because the trail is read back in full on every tick
    and an outage that lasts an hour must not leave 240 identical sentences in
    it.
    """
    if not _repeats_last_note(deps, loop, kind, detail):
        deps.store.record_note(loop.loop_id, kind, detail, deps.now())


def _shadow(deps: LoopDeps, loop: LoopRequest, route: Route) -> None:
    """Write to the trail the decision a live tick would have acted on."""
    decision = {"would": route.action, **route.detail}
    if route.standing and _repeats_last_note(deps, loop, "shadow_decision",
                                             decision):
        return
    deps.store.record_note(loop.loop_id, "shadow_decision", decision, deps.now())


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


def _lane_left_running(deps: LoopDeps, loop: LoopRequest) -> str | None:
    """The lane that would go on running if this loop closed now, if any.

    Both of the module's own accounts of "a lane is really working this branch"
    -- the one the row records while it is `remediating`, and the one whose
    recording write was lost -- and neither one of a lane already completed: a
    row that still names the lane of a finished round is not stranding it.

    `_lane_in_flight`, deliberately, and NOT `_lane_pending`: those answer two
    different questions and only one of them is this one. `_lane_pending` asks
    whether an *outcome* is still to be determined, which is true throughout
    `awaiting_head_move`; this asks whether a *worker* is still going, which in
    `awaiting_head_move` is false by construction -- observing that lane exit is
    what put the loop there. Widening this to `_lane_pending` would report a
    stranded lane on every `no_op_lane` escalation, which is the most ordinary
    way a round fails and is a lane this pass had just watched exit: a leak
    alarm that fires on the routine path stops being read.
    """
    if _lane_in_flight(loop):
        return loop.lane_id
    orphan = _adoptable_orphan(deps, loop)
    return str(orphan["lane_id"]) if orphan else None


def _close(deps: LoopDeps, loop: LoopRequest, target: LoopState,
           **fields: Any) -> bool:
    """Drive the loop terminal, reporting any lane it leaves running.

    Terminal states are absorbing and unclaimable, so a lane still in flight
    when the loop closes is polled by nobody ever again: it finishes, it pushes
    to a pull request this service has just handed back to a human, and no
    re-review follows it. Nothing here can prevent that -- the dispatch surface
    this service holds creates lanes and cannot cancel them, and refusing to
    close the loop instead would pin the pull request open for good (which is
    the failure `active_for_pr` punishes hardest). So it is reported: a
    `lane_abandoned` note beside the notice the human is being sent anyway, and
    a WARNING in the log, both naming the lane to go and look at.

    The lane is identified before the swap, not after: a successful transition
    appends to the trail the orphan replay reads, and only a landed swap is
    worth reporting -- a lost one leaves the loop open and the lane still ours.

    One reason is exempted from the report outright: `head_read_exhausted`
    reaches `_route_due`'s escalate branch only after that pass has already
    read this lane as finished by kanban's own account (`lane.finished` gates
    the branch it comes from) -- the row still says `remediating` because a
    lane whose head never once came back readable never earns the transition
    into `awaiting_head_move` that would say otherwise. `_lane_in_flight`
    cannot see that; it only has the row. Reporting the lane as still running
    here would be the same false alarm on a routine path this module already
    rejected widening this check to `_lane_pending` over -- a GitHub outage
    would then leave one bogus `lane_abandoned` per affected PR, naming a lane
    that is not running, beside the notice a human is already being sent.
    """
    known_finished = fields.get("escalation_reason") == HEAD_READ_EXHAUSTED
    lane_id = None if known_finished else _lane_left_running(deps, loop)
    if not _transition(deps, loop, target, **fields):
        return False
    if lane_id:
        _LOG.warning("loop %s closed as %s with lane %s still running on %s",
                     loop.loop_id, target.value, lane_id, loop.slug)
        deps.store.record_note(loop.loop_id, EVENT_LANE_ABANDONED,
                               {"lane_id": lane_id, "round": loop.round_count,
                                "state": target.value}, deps.now())
    return True


def _clear_running_signal(deps: LoopDeps, loop: LoopRequest) -> None:
    """Take off the reaction that says a round is running, because none is.

    Anchored on Boris's status message (`status_message_ts`), NOT on
    `root_thread_ts`, and the two must never be unified even though both are
    "a message in the same thread". A thread root can carry several reviews
    over time, so a per-review signal on it is ambiguous; worse, it collides
    with `just_do_it_review_reaction_watchdog.py` (live, runs every minute),
    which drives its own reaction state machine on source PR posts and treats
    `thinkies` as the retired predecessor of `rubbing` -- it would strip this
    reaction within a minute of it landing. Boris's status message is not in
    that watchdog's tracked set, so anchoring there sidesteps the collision
    without touching that cron. `post_rereview` deliberately keeps targeting
    `root_thread_ts` -- that is a different requirement (the sibling
    service's authorization gate admits a post only as a reply in an existing
    thread) that this reaction anchor must not be conflated with.

    Read from the *loop's own row*, not any verdict's: a later verdict may
    carry a different status message (or none), and a reaction added on one
    message can only ever be removed from that same message. Idempotent by
    construction -- removing a reaction that is gone is a no-op -- which is
    what lets every path that ends a round run it without checking whether
    another already did.

    `status_message_ts` is nullable (the source column is nullable, and every
    loop opened before this column existed carries none either), and a loop
    with no anchor never had a reaction added for this to be undoing -- so
    there is nothing to remove and nothing is attempted. The reaction is a
    convenience signal, not a correctness mechanism: its absence must never
    stop a round from closing out.
    """
    if loop.status_message_ts:
        deps.slack.remove_thinkies(deps.cfg.boris_channel, loop.status_message_ts)


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
    """
    _clear_running_signal(deps, loop)
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


def _lane_still_running(deps: LoopDeps, lane_id: str) -> bool:
    """Read the lane's status fresh, right here, for one settle and no other.

    Only `_settle_escalation` calls this, and only when `debt.reason` is
    `LANE_TIMEOUT`: it answers a question only that one escalation's
    ownership decision and its wording need, not the routing decision
    `_route_due` already took -- the same reason `_finish_ready` reads
    `unresolved_bot_threads` in its own body rather than folding it into
    `LaneFacts`. The read costs a kanban query only on the tick that actually
    settles a lane_timeout escalation in live mode, since `_settle_escalation`
    is reachable from nowhere else (`_rehearse` never calls it) -- not on
    every due-pass visit the way `_observe_lane`'s read is.

    A read that raises is answered the safe way, not the available way: a
    kanban this call cannot reach and a lane genuinely still going are
    indistinguishable from here, and reading "unreadable" as "finished" would
    release the ownership label and the reaction over a worker that may still
    be pushing to the branch -- the exact collision this service exists to
    prevent. So an exception reads as "still running," the same answer a
    status this build has never catalogued gets: only a status this module
    recognises as terminal (`TERMINAL_LANE_STATUSES`) is ever read as "not
    running" here. That also means this settle can disagree with the due
    pass's own observation moments earlier in the same tick (which only
    required "not yet terminal" to reach here at all) -- a lane that finishes
    in the gap between the two reads is stood down exactly as it always was;
    this function is what lets that still happen instead of freezing the
    lane_timeout observation's answer in place.
    """
    try:
        status = deps.lanes.status(lane_id)
    except Exception:
        return True
    return status not in TERMINAL_LANE_STATUSES


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

    `lane_timeout` carries one more question no other reason asks: whether the
    lane it gave up waiting on is actually done. Kanban's own stale-lane
    reclaim only reaps silence, never duration, so a lane that keeps
    heartbeating can still be genuinely working past LANE_WAIT_SECONDS, and a
    worker in it can still commit and push to this branch. Standing down in
    that case -- releasing `owner_label`, clearing the reaction -- would tell
    a human the PR is free while it is not, which is the exact collision this
    service exists to prevent. So this checks fresh (`_lane_still_running`,
    scoped to this one reason and this one settle) rather than trusting the
    due pass's own, now possibly stale, observation: a lane not confirmed
    finished keeps both signals and is named in the notice; a lane kanban now
    calls terminal stands down exactly as any other escalation does. Nothing
    later ever revisits that "keep" -- the loop closes either way, and closing
    is the only lever this service has over a lane it cannot cancel -- which
    is deliberate: a stale label a human was told about beats a missing one
    that invites a second agent onto a branch a live worker still owns.
    """
    lane_alive = (debt.reason == LANE_TIMEOUT and loop.lane_id is not None
                  and _lane_still_running(deps, loop.lane_id))
    if not debt.notified:
        if deps.slack.notify_escalation(
                verdict, loop.round_count, debt.reason,
                lane_id=loop.lane_id if lane_alive else None) is None:
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
    if lane_alive:
        # Deliberately NOT _stand_down: see the docstring above. The label and
        # the reaction stay, and this note is the only record of why.
        deps.store.record_note(loop.loop_id, LANE_TIMEOUT_LANE_ALIVE,
                               {"lane_id": loop.lane_id}, deps.now())
    else:
        _stand_down(deps, loop, verdict)
    return _close(deps, loop, LoopState.ESCALATED,
                  escalation_reason=debt.reason)


def _escalate(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
              reason: str) -> bool:
    # A debt with nothing paid on it yet: a loop that already owes one is
    # intercepted before it can reach here, on the verdict path and in the due
    # pass alike, so this is always the first attempt at this escalation.
    return _settle_escalation(deps, loop, verdict,
                              EscalationDebt(reason=reason, attempts=0,
                                             notified=False))


def _not_open(pr_state: str | None) -> bool:
    """Is the pull request positively known not to be open?

    None -- unreadable, or never asked for -- is deliberately NOT "not open".
    Closing a loop is irreversible (terminal states are absorbing, and
    `active_for_pr` then refuses every later generation for that PR), so it is
    taken only on a state GitHub actually reported. A blip leaves the loop
    exactly where it was.
    """
    return pr_state is not None and pr_state != PR_OPEN


def _close_stale(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
                 pr_state: str) -> bool:
    """The pull request is not open any more. Close the loop, and tell nobody.

    Silent on purpose. Merging or closing a pull request is a human's own act
    and the ordinary end of one -- of the eleven distinct pull requests today's
    backlog would drive, nine are already MERGED -- so a Slack notice here would
    page the person who merged it about their own merge, on the commonest path
    there is. That is how a notification channel stops being read, and it would
    be bought for nothing: there is no remediation left to do and no decision
    for anyone to take.

    Standing down is *not* skipped, though, and that is the difference between
    silent and absent. The ownership label and the thinkies reaction are signals
    this loop put on a pull request it no longer owns; a label left behind on a
    merged PR is exactly the "looks owned forever" failure this service exists
    to avoid. Both acts are idempotent, so a close lost to a lapsed lease simply
    runs them again.

    The trail carries the record Slack does not: a `pr_not_open` note naming the
    state GitHub reported, and the transition into `stale`. `stale` rather than
    `escalated` because the row's state is read as a claim about what happened,
    and "a human must intervene" would be a false one; rather than `ready`
    because that one notifies and means the opposite thing.
    """
    _note_standing(deps, loop, PR_NOT_OPEN,
                   {"pr_state": pr_state, "round": loop.round_count})
    _stand_down(deps, loop, verdict)
    return _close(deps, loop, LoopState.STALE, escalation_reason=PR_NOT_OPEN)


def _finish_ready(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """Close a loop ready, and tell Slack what GitHub says about open bot
    review threads while doing it.

    The Codex/CodeQL thread check is read *here*, not folded into `_route`'s
    `PrFacts`: it answers a question only the ready notice's wording needs,
    not the routing decision -- a live tick reaches this function only after
    `_route` has already decided READY_FOR_HUMAN_REVIEW, and reading it
    earlier would spend a GraphQL call on every verdict this loop ever sees,
    not only the one that closes it. `unresolved_bot_threads` already turns
    a failed or malformed query into `None` rather than raising or guessing
    (see its docstring), so nothing here needs to guard it a second time --
    and by design that `None` changes only what `notify_ready` says, never
    whether this closes: a stuck GraphQL query must not hold a clean PR's
    loop open, and must not read as an escalation either. Reached only from
    `_perform`, which `_consumer` hands out in live mode alone (see the
    module docstring's `_consumer` paragraph) -- so, like every other GitHub
    write in this module, this call happens only when a live tick performs
    the route a paused one only recorded.
    """
    threads = deps.github.unresolved_bot_threads(verdict.owner, verdict.repo,
                                                  verdict.pr_number)
    if deps.slack.notify_ready(verdict, loop.round_count, threads) is None:
        deps.store.record_note(loop.loop_id, "notify_failed",
                               {"kind": "ready"}, deps.now())
    _stand_down(deps, loop, verdict)
    return _close(deps, loop, LoopState.READY)


def _repost(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """A system-origin incomplete is Boris failing, not the PR failing.

    Retrying the review costs no remediation round: nothing was remediated.
    """
    # root_thread_ts, deliberately -- NOT the reaction anchor
    # (status_message_ts). SlackIO's gate admits a re-review only as a reply
    # in an existing thread; the thinkies reaction anchor is a different
    # concern (see _clear_running_signal) and must stay separate from this.
    #
    # repost_rereview, deliberately -- NOT post_rereview. This path spends no
    # round and pushed nothing, so it must not carry post_rereview's "Round N
    # of M -- remediation pushed" claim; that mismatch (a round number and a
    # push that never happened) is the bug this split exists to prevent. See
    # slack_io.py's docstrings on both methods.
    if deps.slack.repost_rereview(verdict, loop.root_thread_ts) is None:
        # A post that failed with a thread present is a Slack blip. Leave the
        # loop in its current state, unspent and immediately due, so the
        # daemon's sweep retries it instead of burning it.
        deps.store.record_note(loop.loop_id, "rereview_post_failed",
                               {"round": loop.round_count + 1}, deps.now())
        return False
    return _transition(deps, loop, LoopState.REREVIEW_POSTED)


def _lane_pushed(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
                 head: str) -> bool:
    """The lane finished and moved the head: ask Boris to look again.

    The round is not spent here -- it was spent when the lane was dispatched --
    so the post names the round that has just finished, and the row keeps its
    count. What it does record is the head the lane pushed, because that is what
    the next verdict will be about and what the round after this one would be
    dispatched against.

    Posted before the row is moved on, in that order for the same reason
    `_repost` uses it: a post that failed leaves the loop exactly where it was,
    unspent and immediately due, so the next sweep tries again rather than
    parking it in `rereview_posted` to wait for a verdict nobody will send. The
    price is the narrow reverse: a swap lost after the post lands (our lease
    lapsed) leaves a re-review requested that the row does not know about, which
    is recorded as a lost transition and costs one duplicate review at worst.
    """
    _clear_running_signal(deps, loop)
    # root_thread_ts, not status_message_ts -- same split as _repost's, for
    # the same reason.
    if deps.slack.post_rereview(verdict, loop.root_thread_ts,
                                loop.round_count) is None:
        # Unlike `_repost`'s, this failure is retried by the due pass on every
        # tick rather than by the next verdict, so the note is standing.
        _note_standing(deps, loop, "rereview_post_failed",
                       {"round": loop.round_count})
        return False
    return _transition(deps, loop, LoopState.REREVIEW_POSTED, head_sha=head)


def _await_head_move(deps: LoopDeps, loop: LoopRequest) -> bool:
    """The lane exited without a visible push. Give that push one window.

    Not a round and not a verdict: the row is parked in `awaiting_head_move`
    until the grace window elapses, and the next look either finds the push or
    calls the round a no-op. The reaction comes off now, though -- the lane has
    exited, so nothing is running whichever way the next look goes.

    Exactly one such window is possible by construction: the store's transition
    table gives `awaiting_head_move` no self-edge, so a second attempt to enter
    it would raise rather than quietly park the loop again forever.
    """
    _clear_running_signal(deps, loop)
    return _transition(deps, loop, LoopState.AWAITING_HEAD_MOVE,
                       next_attempt_at=deps.now() + HEAD_MOVE_GRACE_SECONDS)


def _notify_still_running(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
                          detail: dict) -> bool:
    """Say once, in the review thread, that this round is still in flight.

    Fires from the due pass once a round has run past
    STILL_RUNNING_THRESHOLD_SECONDS with no terminal lane status -- the gap
    the module constant's comment describes, where the ownership label and
    the thinkies reaction look the same whether the round is working normally
    or wedged.

    Once per round, not once a tick: `_route_due` recomputes this route on
    every due-pass visit for as long as the round keeps running past the
    threshold, and it is the standing-note dedup here (`_repeats_last_note`)
    that stops the repeat -- the same mechanism that already keeps a hold
    nobody has lifted, or a GitHub still unreadable, from growing the trail
    without end. Checked before the post is attempted, and the note that
    prevents the next repeat is written whether or not the post itself
    landed: a Slack outage is not a reason to retry this every tick the way
    `_lane_pushed`'s post is, because nothing downstream depends on this one
    arriving -- the human still gets the escalation at LANE_WAIT_SECONDS if
    the round is genuinely wedged, notice or no notice.

    `elapsed_minutes` is measured from `loop.updated_at`, not from a value
    that moves on every due-pass visit: `store.defer` (what reschedules a
    loop the due pass has just looked at) deliberately leaves `updated_at`
    alone, and only a real transition -- the lane's own dispatch into
    REMEDIATING -- sets it while this round is in flight. That is the same
    basis LANE_WAIT_SECONDS is measured from, so the "will escalate at
    `timeout_minutes`" the notice states stays true against the bound that
    actually fires it.

    Always returns False: nothing here is a round, a state or a verdict --
    only a best-effort post that changes no loop state and consumes nothing.
    """
    if _repeats_last_note(deps, loop, ROUTE_STILL_RUNNING, detail):
        return False
    elapsed_minutes = int((deps.now() - loop.updated_at) // 60)
    timeout_minutes = int(LANE_WAIT_SECONDS // 60)
    deps.slack.notify_still_running(verdict, elapsed_minutes, timeout_minutes,
                                    loop.root_thread_ts)
    deps.store.record_note(loop.loop_id, ROUTE_STILL_RUNNING, detail, deps.now())
    return False


def _lane_pending(loop: LoopRequest) -> bool:
    """Is this loop waiting on the outcome of a lane?

    Only two states are: `remediating` with a lane recorded (it is running) and
    `awaiting_head_move` (it has exited and the push has not shown up yet). The
    state, not the lane_id, is what answers -- `lane_id` outlives the round it
    ran, so a `rereview_posted` row still names the lane whose completion put it
    there, and reading that back as a fresh completion would post the re-review
    a second time and then call the unmoved head a no-op.
    """
    return loop.lane_id is not None and loop.state in (
        LoopState.REMEDIATING, LoopState.AWAITING_HEAD_MOVE)


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
    transition is never superseded by it -- which is what `_adoptable_orphan`
    answers from the row instead, and what `_drop_orphan` writes down here as
    the explicit end of the claim.
    """
    orphan: dict | None = None
    for event in deps.store.events(loop.loop_id):
        kind, detail = event["kind"], event["detail"]
        if kind == EVENT_ORPHANED_LANE and detail.get("lane_id"):
            orphan = detail
        elif kind == EVENT_TRANSITION and detail.get("lane_id"):
            orphan = None
        elif kind == EVENT_ORPHAN_SUPERSEDED:
            orphan = None
    return orphan


def _adoptable_orphan(deps: LoopDeps, loop: LoopRequest) -> dict | None:
    """The orphan this loop may still adopt, which is none once it has a lane.

    Adoption retries a lost write, so the only row it can correct is one that
    still carries no lane. A row recording a lane of its own has nothing to
    correct -- that lane is the loop's, whatever the trail still claims -- and
    an orphan note outlives the round it describes, landing after the rival
    transition that would have superseded it. Adopting it there would point the
    loop at a dead lane and rewind its head to that lane's.

    The due pass deletes such a claim, whether or not a lane is in flight;
    here it is simply not one, and the verdict routes on as though the note
    were already gone. That division is deliberate: a verdict is consumed
    whatever this tick does with it, so spending one on bookkeeping stalls a
    loop that could have been driven.
    """
    if loop.lane_id is not None:
        return None
    return _orphaned_lane(deps, loop)


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

    A correction, never a rewind -- and that has to hold for every field this
    writes, not only for the round. A row that already records a lane is not
    corrected by an orphan but overwritten: pointed at a lane that finished
    rounds ago, rewound to the head that lane was dispatched against, and
    dropped back into `remediating`, where the one-lane rule then makes every
    later pass skip it forever. So such a row is refused here whatever the
    caller decided, and the stale claim is deleted rather than left to be tried
    again on the next tick.

    The round the orphan spent is the round recorded, but never a *lower* one
    than the row already carries, for the same reason: adopting a stale note at
    face value would hand a loop that had exhausted its round cap its whole
    budget back. Callers must also refuse to adopt while a lane is in flight;
    neither guard here is a substitute for that one.
    """
    if loop.lane_id is not None:
        return _drop_orphan(deps, loop, orphan)
    spent = int(orphan.get("round") or loop.round_count + 1)
    return _transition(
        deps, loop, LoopState.REMEDIATING,
        round_count=max(spent, loop.round_count),
        lane_id=str(orphan["lane_id"]),
        head_sha=str(orphan.get("head_sha") or loop.head_sha),
        next_attempt_at=deps.now() + LANE_POLL_SECONDS)


def _drop_orphan(deps: LoopDeps, loop: LoopRequest, orphan: dict) -> bool:
    """Delete an orphan's claim on a loop that records a lane of its own.

    The note is superseded in fact and unsuperseded in the trail -- it was
    written after the transition that overtook it -- so nothing else will ever
    clear it. Left alone it is not merely noise: it would be adopted, and the
    loop rewound to it, the first time the row's lane is cleared, however many
    rounds later. `_orphaned_lane` reads this note as the end of the claim.

    False: a claim was deleted, which is bookkeeping and not a step the loop
    took.
    """
    deps.store.record_note(loop.loop_id, EVENT_ORPHAN_SUPERSEDED, {
        "lane_id": str(orphan.get("lane_id") or ""),
        "round": orphan.get("round"),
        "superseded_by": loop.lane_id,
    }, deps.now())
    return False


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

    # The lane exists from here on. Record the round before decorating Boris's
    # status message: if the compare-and-swap is lost we are no longer the
    # driver, and a thinkies reaction would claim a round this tick does not
    # own.
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
    # Anchored on Boris's status message, not root_thread_ts -- see
    # _clear_running_signal for why the two anchors must never be unified.
    if loop.status_message_ts:
        deps.slack.add_thinkies(deps.cfg.boris_channel, loop.status_message_ts)
    else:
        # status_message_ts is nullable (some rows never got one from
        # boris_intake; every loop opened before this column existed has
        # none either). The reaction is a convenience signal, not a
        # correctness mechanism, so its absence must not fail the round --
        # recorded so a missing thinkies reaction is explainable from the
        # trail rather than mysterious.
        deps.store.record_note(loop.loop_id, "reaction_skipped_no_status_message",
                               {"round": round_number}, deps.now())
    return True


def _route(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
           facts: PrFacts | None) -> Route:
    """Decide what a live tick would do with this verdict, and do none of it.

    The whole routing decision lives here, and nothing here writes loop state,
    posts, labels or dispatches -- nor reads a collaborator. What GitHub says
    arrives as `facts`, already read, because a decision taken without it would
    not be the decision a live tick takes (which is what a shadow soak exists
    to check) while *whether to read at all* is a question about the operating
    mode, and the mode is asked outside the routing.

    `facts` is None when nothing was read: the `off` switch stops the work as
    well as the writes. Past the debts the store alone can answer there is then
    no decision to take, and the route says so rather than inventing one.
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

    if facts is None:
        # Nothing was read, so there is nothing to decide from: recorded as
        # what it is, an unobserved tick, and never as a hold Chad did not put
        # there or an author GitHub failed to give us.
        return Route(ROUTE_UNOBSERVED, {"mode": cfg.mode})
    if facts.held:
        # Chad's kill switch, and also the fail-closed answer when GitHub
        # cannot be read at all.
        return Route(ROUTE_HELD, {"label": cfg.hold_label})
    if _not_open(facts.pr_state):
        # Merged or closed while this verdict was in flight. Everything below
        # writes to the pull request -- the ownership label, a lane told to
        # "fix them at this head and push", a re-review -- and none of it means
        # anything now. Checked ahead of the author, because who wrote a merged
        # pull request is not a question worth escalating over.
        return Route(ROUTE_CLOSE_STALE, {"pr_state": facts.pr_state})
    author = facts.author
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
    orphan = _adoptable_orphan(deps, loop)
    if orphan is not None:
        # The row says no lane, the trail says otherwise, and the trail is
        # right: this loop's own dispatch lost its recording write. Adopt that
        # lane rather than open a second one against the same branch.
        return Route(ROUTE_ADOPT_LANE, dict(orphan), defer=LANE_ORPHANED)
    if loop.round_count >= cfg.round_cap:
        return Route(ROUTE_ESCALATE,
                     {"reason": EscalationReason.ROUND_CAP.value})
    return Route(ROUTE_OPEN_LANE, {"round": loop.round_count + 1})


def _head_read_attempts(deps: LoopDeps, loop: LoopRequest, lane_id: str) -> int:
    """How many times in a row this lane's head has come back unreadable.

    Scoped to `lane_id`, not the loop: kanban hands every dispatch of a lane a
    fresh id, so a loop that rides out a blip in one round and later opens
    another lane starts this count at zero rather than inheriting the last
    round's failures.

    Also scoped to the trailing run, not the lane's whole history: a read that
    lands resets it, because a landed read is what the budget exists to wait
    for. The transition into `awaiting_head_move` is that landing -- it is
    reachable only from a head read that came back unmoved, so it is proof a
    read for this lane succeeded -- and it is the only such proof this budget
    needs, because nothing after it can still be counting the run it ended:
    a moved head or a spent grace window both close the loop out from under
    this lane before another read of it is ever taken. Without the reset the
    grace window's own successful read would not end a run of blips, only
    interrupt it on the way to a cumulative total -- so four blips, a landed
    read that opens the window, and one blip inside it would already look
    like a fifth attempt that never happened.

    Counts the `head_unreadable` notes a live tick actually wrote, rather than
    keeping a ledger of its own -- which is also what makes this budget
    something only a live tick can spend. A paused tick's decisions go to
    `_shadow` under a different kind (`shadow_decision`), so a pause, however
    long, writes none of these and this always reads zero while one is on.
    """
    attempts = 0
    for event in deps.store.events(loop.loop_id):
        kind, detail = event["kind"], event["detail"]
        if kind == ROUTE_HEAD_UNREADABLE and detail.get("lane_id") == lane_id:
            attempts += 1
        elif kind == EVENT_TRANSITION and \
                detail.get("to") == LoopState.AWAITING_HEAD_MOVE.value:
            attempts = 0
    return attempts


def _route_due(deps: LoopDeps, loop: LoopRequest,
               lane: LaneFacts | None) -> Route | None:
    """Decide what a live tick owes a loop no verdict is going to arrive for.

    `_route`'s counterpart for the due pass, under the same discipline: it
    decides and performs none of it. What kanban and GitHub say arrives as
    `lane`, already read, for the reason `_route` takes `facts` that way --
    whether to read at all is a question about the operating mode, and `None`
    is both "this loop waits on no lane" and "this tick looked at nothing".

    None means the loop owes nothing, which is the ordinary case and is
    deliberately silent: this pass revisits every unsettled loop on every tick,
    and a note for a healthy one would grow its trail without end.

    Every route it names is `standing`: a debt is still owed on the next tick,
    unlike a verdict, which arrives once and is consumed.
    """
    debt = _escalation_debt(deps, loop)
    if debt is not None:
        # A stopped loop stays stopped: settle it, and never mind the lane,
        # which its escalation has already given up on.
        return Route(ROUTE_SETTLE_ESCALATION,
                     {"reason": debt.reason, "notified": debt.notified},
                     debt=debt, standing=True)
    orphan = _orphaned_lane(deps, loop)
    if orphan is not None:
        # Settled before the row's own lane is completed, and it costs at most
        # one tick to do so: completing a lane writes the state a stale claim
        # would later be adopted over, so the claim goes first.
        if loop.lane_id is None:
            return Route(ROUTE_ADOPT_LANE, dict(orphan), standing=True)
        # The row records a lane of its own, so this claim can never be adopted
        # -- and nothing in the trail will ever supersede it, so it would come
        # back the moment that lane is cleared. Deleting it is this pass's job
        # precisely because this is the pass with nothing else to spend: a
        # verdict is consumed whatever is done with it, so the verdict path
        # routes past a stale claim rather than stopping to tidy it.
        return Route(ROUTE_DROP_ORPHAN, dict(orphan), standing=True)

    if lane is None:
        # This loop waits on no lane at all, so there is nothing to decide and
        # nothing to write.
        return None
    if not lane.finished:
        # A lane still working -- or one that will never say it has stopped.
        # The bound below is the only one in this module that does not
        # presuppose a finished lane, and without it a wedged `running` card or
        # a card a human deleted (`status` None) held its pull request open for
        # good: `active_for_pr` refuses every later generation while this loop
        # lives, and no other route here can ever fire for it.
        #
        # No hold label is read first, and deliberately: like the escalation
        # retry above, what this does is notify a human and stand down the
        # signals this loop itself added, which is what a hold wants to happen
        # anyway -- and a hold cannot be read on a tick where the lane read is
        # the only thing that answered.
        if deps.now() >= loop.updated_at + LANE_WAIT_SECONDS:
            return Route(ROUTE_ESCALATE,
                         {"reason": LANE_TIMEOUT, "lane_id": loop.lane_id,
                          "status": lane.status},
                         standing=True)
        if deps.now() >= loop.updated_at + STILL_RUNNING_THRESHOLD_SECONDS:
            # Ahead of LANE_WAIT_SECONDS only in the sense that this bound is
            # smaller; the check above already returned for anything that has
            # crossed the timeout, so a round is never told it is "still
            # running" on the same tick it escalates.
            return Route(ROUTE_STILL_RUNNING,
                         {"lane_id": loop.lane_id, "round": loop.round_count},
                         standing=True)
        return None
    if lane.held:
        # Everything below writes -- a reaction, a post, a label -- so Chad's
        # kill switch is answered here, and `has_hold` fails closed so a GitHub
        # this pass could not read lands in this branch too.
        return Route(ROUTE_HELD,
                     {"label": deps.cfg.hold_label, "lane_id": loop.lane_id},
                     standing=True)
    if _not_open(lane.pr_state):
        # The lane ran, and by the time it finished the pull request was merged
        # or closed. Everything below this line writes to it -- a re-review
        # post, an escalation, a released label -- so the same answer the
        # verdict path gives applies here, and ahead of the head comparison: a
        # head that moved into a merged pull request still earns no re-review.
        return Route(ROUTE_CLOSE_STALE,
                     {"pr_state": lane.pr_state, "lane_id": loop.lane_id},
                     standing=True)
    if lane.head is None:
        # Read the hold but not the head: a blip between two calls. Waiting
        # costs a tick; guessing costs the loop, because "no head" read as "no
        # push" escalates a round that may well have landed. But the wait has
        # to end somewhere: a deleted PR, a renamed or transferred repo, or a
        # token that lost its scope reads exactly like a blip on every tick,
        # and nothing else would ever tell this loop the difference.
        attempt = _head_read_attempts(deps, loop, loop.lane_id) + 1
        if attempt >= HEAD_READ_ATTEMPTS:
            return Route(ROUTE_ESCALATE, {"reason": HEAD_READ_EXHAUSTED},
                         standing=True)
        return Route(ROUTE_HEAD_UNREADABLE,
                     {"lane_id": loop.lane_id, "attempt": attempt},
                     standing=True)
    if lane.head != loop.head_sha:
        if not loop.root_thread_ts:
            # SlackIO refuses to post outside a thread because the intake gate
            # silently drops such a message, so the remediation is real and
            # there is no route back into review for it. Permanent: escalate.
            return Route(ROUTE_ESCALATE, {"reason": REREVIEW_UNPOSTABLE},
                         standing=True)
        return Route(ROUTE_LANE_PUSHED,
                     {"round": loop.round_count, "head_sha": lane.head},
                     standing=True)
    if loop.state is LoopState.AWAITING_HEAD_MOVE:
        # The grace window has been spent and the head still has not moved.
        # Re-reviewing an unchanged pull request would reproduce this verdict
        # and spend another round arriving back here.
        return Route(ROUTE_ESCALATE,
                     {"reason": EscalationReason.NO_OP_LANE.value},
                     standing=True)
    return Route(ROUTE_AWAIT_HEAD_MOVE,
                 {"lane_id": loop.lane_id, "status": lane.status},
                 standing=True)


def _perform(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
             route: Route) -> bool:
    """Do what the routing decided. Reached only in live mode."""
    if route.defer is not None:
        _defer_verdict(deps, loop, verdict, route.defer)
    if route.action == ROUTE_SETTLE_ESCALATION:
        if route.debt is None:
            # Unreachable via `_route`, and a silent `return False` here would
            # look exactly like a loop with nothing owing.
            raise ValueError("settle_escalation route carries no debt")
        return _settle_escalation(deps, loop, verdict, route.debt)
    if route.action in _OBSERVATIONS:
        # A standing observation -- a hold that has not been lifted, a GitHub
        # still unreadable -- is retaken every tick, and recording it every
        # time appends the same sentence to a trail every later tick reads back
        # in full. Recorded once, until something else happens to the loop.
        if route.standing:
            _note_standing(deps, loop, route.action, route.detail)
        else:
            deps.store.record_note(loop.loop_id, route.action, route.detail,
                                   deps.now())
        return False
    if route.action == ROUTE_CLOSE_STALE:
        return _close_stale(deps, loop, verdict, str(route.detail["pr_state"]))
    if route.action == ROUTE_ESCALATE:
        return _escalate(deps, loop, verdict, str(route.detail["reason"]))
    if route.action == ROUTE_NOTIFY_READY:
        return _finish_ready(deps, loop, verdict)
    if route.action == ROUTE_REPOST_REREVIEW:
        return _repost(deps, loop, verdict)
    if route.action == ROUTE_LANE_PUSHED:
        return _lane_pushed(deps, loop, verdict, str(route.detail["head_sha"]))
    if route.action == ROUTE_AWAIT_HEAD_MOVE:
        return _await_head_move(deps, loop)
    if route.action == ROUTE_STILL_RUNNING:
        return _notify_still_running(deps, loop, verdict, route.detail)
    if route.action == ROUTE_ADOPT_LANE:
        return _adopt_lane(deps, loop, route.detail)
    if route.action == ROUTE_DROP_ORPHAN:
        return _drop_orphan(deps, loop, route.detail)
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
    refusing it would file a round with no lane instead. Deleting a stale
    orphan's claim is *not* taken here: a paused tick has no reason to tidy the
    trail, and the live tick that finds the claim still there will drop it.
    """
    _shadow(deps, loop, route)
    if route.defer is not None:
        _defer_verdict(deps, loop, verdict, route.defer)
    if route.action == ROUTE_ADOPT_LANE:
        return _adopt_lane(deps, loop, route.detail)
    return False


def _consumer(deps: LoopDeps) -> Callable[
        [LoopDeps, LoopRequest, Verdict, Route], bool]:
    """Which consumer this mode allows: the one place the mode is asked.

    Both passes take their decisions through it, so neither can grow a gate of
    its own -- the shape that has now failed three times, most recently as an
    ordinary early return in the due pass, where deleting one line let an `off`
    tick post to a human channel and close a live-created loop. `_perform`
    acts; `_rehearse` records and stops.
    """
    return _perform if deps.cfg.mode == "live" else _rehearse


def _observes(deps: LoopDeps) -> bool:
    """Whether this tick looks at the outside world at all.

    `_consumer`'s counterpart on the read side, and the only other place the
    mode is asked. `off` is the hard kill switch, so it stops the work and not
    merely the writes: no lane is polled and no GitHub is read. `shadow` keeps
    reading, because rehearsing a decision taken without the facts would not be
    rehearsing the decision a live tick takes.

    Named and shared for the same reason `_consumer` is: both passes ask
    through it, so neither can grow a mode test of its own.
    """
    return deps.cfg.mode != "off"


def _drive(deps: LoopDeps, loop: LoopRequest, verdict: Verdict) -> bool:
    """Route one verdict against a loop we hold the lease on."""
    facts = _observe(deps, verdict) if _observes(deps) else None
    return _consumer(deps)(deps, loop, verdict,
                           _route(deps, loop, verdict, facts))


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
            root_thread_ts=verdict.root_thread_ts,
            status_message_ts=verdict.status_message_ts, now=deps.now(),
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


def _reschedule(deps: LoopDeps, loop: LoopRequest) -> None:
    """Push a loop the due pass has just visited behind the ones it has not.

    The pass claims `next_attempt_at <= now` ordered by that same column, so a
    loop nothing advances is claimed again on the very next tick, ahead of every
    loop that has been waiting longer. Twenty of those and the batch never moves:
    remediation halts for every other pull request, silently. This is the one
    place that guarantees the rotation, because it does not ask what the route
    was -- a route added later cannot forget to call it.

    `store.defer` only ever raises the floor, so a route that chose a longer
    wait (the grace window) keeps it, and a terminal row is refused outright.
    Best-effort: a loop that could not be rescheduled is worth a log line and
    nothing more, and this runs on the path that must not throw past `tick`.

    Asked in both modes, and that is not a mode gate this module has grown a
    fourth of: rescheduling decides nothing and touches no loop state, and a
    paused pass that did not rotate would starve exactly the shadow soak that
    exists to find this class of failure.
    """
    try:
        deps.store.defer(loop.loop_id, now=deps.now(),
                         not_before=deps.now() + DUE_BACKOFF_SECONDS)
    except Exception:
        _LOG.exception("loop %s could not be rescheduled", loop.loop_id)


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
        status_message_ts=loop.status_message_ts,
    )


def _settle_due(deps: LoopDeps, driven: set[str]) -> int:
    """Carry every step no verdict is going to arrive to carry.

    Three things qualify, and none of them can count on a verdict. An escalated
    PR gets no further verdicts at all -- that is what escalating means. An
    orphaned lane need not produce one either, since a lane that changes
    nothing triggers no re-review. And a lane that *has* changed something
    produces one only once this pass has asked for it: the whole round hinges
    on a re-review nobody else will post.

    It is one pass and one batch on purpose. A second `claim_due` would lease
    loops this one is still working, so lane completion joins the retries here
    rather than sweeping for itself.

    The lease is renewed per loop, not taken once for the batch: LEASE_SECONDS
    is short next to DUE_LIMIT loops with a kanban poll and two GitHub reads
    each, and a tick whose lease lapses partway learns it from a `transition()`
    returning False *after* it has already posted. Renewing first turns that
    into a refusal before any side effect, and a renewal we cannot have means
    somebody else owns the loop: leave it entirely, do not merely skip the
    write.

    It decides and acts through the same helpers the verdict path uses --
    `_route_due` names what a live tick owes, `_observes` says whether it looks
    at anything to name it, `_perform` pays it and `_rehearse` records it -- so
    there is no mode gate of its own here to forget, and none that a mutation
    can delete. The one thing it does not share is a verdict: the one that
    stopped this loop was consumed ticks ago, so it addresses Slack and GitHub
    from the row (`_verdict_for`).
    """
    try:
        due = deps.store.claim_due(now=deps.now(), limit=DUE_LIMIT)
    except Exception:
        _LOG.exception("the due pass could not lease its batch")
        return 0

    advanced = 0
    consume = _consumer(deps)
    observes = _observes(deps)
    for loop in due:
        try:
            if loop.loop_id in driven:
                continue
            if not deps.store.renew_claim(loop.loop_id, now=deps.now()):
                _LOG.warning("the lease on loop %s lapsed mid-batch; "
                             "leaving it to whoever holds it now", loop.loop_id)
                continue
            route = _route_due(deps, loop,
                               _observe_lane(deps, loop) if observes else None)
            if route is None:
                continue
            if consume(deps, loop, _verdict_for(loop), route):
                advanced += 1
        except Exception:
            _LOG.exception("loop %s could not be settled", loop.loop_id)
        finally:
            # In the `finally`, so that a loop whose routing threw is
            # rescheduled too. That is not tidiness: an exception is precisely
            # the case that would otherwise leave a permanently-due row, and
            # permanently-due rows are what starve the batch.
            _reschedule(deps, loop)
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
    exists so that a step nobody will send another verdict for -- an escalation
    nobody received, a lane nobody recorded, a finished lane whose re-review
    nobody has asked for -- is taken anyway.
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
