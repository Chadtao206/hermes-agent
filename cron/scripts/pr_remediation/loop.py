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
failed to record is still outstanding. Stated that way it was enforced in the
one branch that dispatches a lane, and that was not enough: a route that merely
moves the row *off* `remediating` produces the same two lanes one verdict
later, because the row is the whole of `_lane_in_flight`'s account. So the rule
a verdict is actually held to is the stronger one -- while a lane is in flight
no verdict moves the loop at all, whatever its outcome -- and it is asked once,
above the outcome dispatch, where a branch added later cannot fail to inherit
it. That subsumes the closing outcomes too: standing the pull request down over
a live worker is the same collision read from the other end (see `_route`).
Kanban's idempotency key does not cover
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

That retry is bounded by HEAD_READ_ATTEMPTS *spaced by*
HEAD_READ_RETRY_SECONDS -- the same shape as NOTIFY_ATTEMPTS below and for the
same reason, including the spacing, without which neither count was a
tolerance at all: a blip is worth riding out, but a PR that was deleted, a repo
that was renamed or transferred, or a token that lost its scope reads exactly
like a blip on every single tick, and nothing else would ever end the wait -- leaving the loop non-terminal blocks every later
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
cannot read is a hold and the pass writes nothing at all. Four due-pass routes
write without it, all deliberately and three for one reason: what the
escalation retry, the `lane_timeout` escalation and the acknowledgement
guard's deadline escalation do is notify a human and (`lane_timeout` against a
lane not confirmed finished excepted -- see below) stand down the signals this
loop itself added, which is what a hold wants to happen anyway. Two of them
could not read it in any case -- a `lane_timeout` fires on the branch where
the kanban poll was the only read taken, and the deadline fires on the branch
where the gate says a review is already live. The fourth is `still_running`,
which posts one progress line into the review thread and touches neither the
pull request nor the loop's state. The re-review re-post is *not* among them
any more: it writes to the pull request through boris_intake's gate, which has
no hold check of its own, so it reads the label itself (`_route_rereview_ack`).

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
newest-verdict rule above is what guarantees -- and, because the due pass's own
cadence is fifteen seconds, spread across NOTIFY_RETRY_SECONDS as well: a count
alone gave up on Slack after a minute, which is a bound on nothing anybody
would have chosen.

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

from pr_remediation import jev_gate
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
# at the same value, and neither number means anything on its own: what rides
# out an outage is this count times HEAD_READ_RETRY_SECONDS below, and it was
# the spacing rather than the count that this budget was missing. Low enough,
# at that spacing, that a head unreadable for a real reason -- the PR deleted,
# the repo renamed or transferred, a token that lost its scope -- cannot pin
# this PR's lane loop, and with it every later generation (`active_for_pr`),
# open forever.
HEAD_READ_ATTEMPTS = 5

# How long the due pass waits before spending the next of those attempts.
#
# The count is not the bound; the count times this is. Spent one per due-pass
# visit and revisited every DUE_BACKOFF_SECONDS, the five attempts above bought
# sixty seconds of tolerance, measured by driving the real tick at the real
# cadence -- so a GitHub incident of the kind we watched last for hours ended
# the wait as `head_read_exhausted` inside the first minute of it, stranding a
# pull request that would have recovered on its own. Raising the count would
# not have fixed that: at fifteen seconds a visit, any count anyone would write
# down still only buys minutes.
#
# Half an hour, which is what REREVIEW_REPOST_ATTEMPTS' own spacing is for
# (REREVIEW_ACK_GRACE_SECONDS, twelve attempts one window apart, 130 minutes)
# arrived at from the other direction: four spacings is two hours, the same
# order as that budget's span and comfortably inside LANE_WAIT_SECONDS. It
# costs nothing on the path that matters -- a read that lands, lands on the
# first attempt -- and what it lengthens is only how long this loop waits
# before deciding a transient is permanent. Escalation is absorbing, so that
# asymmetry is the whole argument: a needless wait costs two hours, a needless
# escalation costs the pull request.
HEAD_READ_RETRY_SECONDS = 30 * 60

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
# unheard. Like HEAD_READ_ATTEMPTS above, this number is only half of a bound:
# what rides out a Slack outage is this count times NOTIFY_RETRY_SECONDS below,
# and the count alone bought a minute. Low enough, at that spacing, that a
# misconfigured channel cannot hold the PR's loop open (and with it every later
# loop for that PR) indefinitely.
NOTIFY_ATTEMPTS = 5

# How long the due pass waits before spending the next of those attempts, for
# the reason HEAD_READ_RETRY_SECONDS gives and at the same value -- and the
# consequence here is worse. One per visit at DUE_BACKOFF_SECONDS bought sixty
# seconds, measured at the real cadence: a minute of Slack being down closed
# the loop `escalation_unreachable`, released the ownership label and told
# nobody, which this module's own `_settle_escalation` calls the worst outcome
# this system can produce. Meanwhile the re-review budget waits ten minutes
# between attempts for a strictly smaller consequence.
#
# It delays no escalation that is going to land: a reachable Slack takes the
# post on the first attempt, and only a failed one is spaced. What it delays is
# giving up. The one gap it does not cover is a verdict arriving for a loop
# that owes an escalation -- `_handle` claims by pull request and never asks
# `next_attempt_at` -- so a burst of verdicts can still spend attempts faster
# than this; the newest-verdict rule bounds that at one per sweep, which is the
# same guarantee the docstring already claims for this budget.
NOTIFY_RETRY_SECONDS = 30 * 60

# How long a `rereview_posted` loop waits for its request to show up in
# boris_intake before assuming the post was rejected and sending it again.
# The gate admits synchronously, so a successful request appears within
# seconds; this window is sized for `catch_up`, which replays a missed
# message on the next daemon start and can legitimately admit minutes late.
REREVIEW_ACK_GRACE_SECONDS = 10 * 60

# How many times the re-review is re-posted before a human is told. Twelve
# at the grace above is roughly two hours, which is deliberately longer than
# a typical GitHub incident: escalation is absorbing, so escalating early
# permanently strands a pull request that would have recovered on its own.
REREVIEW_REPOST_ATTEMPTS = 12

# How long a loop may sit in `rereview_posted` waiting on a request the intake
# gate DID take, measured from when the gate took it, before a human is told
# regardless of what boris_intake's own tables say. The backstop the retry
# budget above cannot be: that budget is only ever spent when the intake read
# comes back NOT_ADMITTED, and the read coming back ADMITTED is not a promise of
# a verdict. `slack_unavailable` is a second terminal state in boris_intake (its
# store.py gives it an empty transition set), and a request that ends there is
# never `complete` -- so `new_verdicts` never returns it, `request_exists` goes
# on answering ADMITTED at this loop's head, and the loop parks exactly as it did
# before this guard existed. A request wedged in `reviewing` does the same. No
# re-post is owed on that path, which is why nothing but elapsed time can bound
# it -- and why this is deliberately not applied to the path where a re-post IS
# owed (see `_route_rereview_ack`): there the budget is the bound, and this one,
# measured from a wait that may have begun long before this code first ran,
# would escalate a whole backlog with no retry at all.
#
# From the admission and not from the wait, because the wait is not evidence
# about the review. An admission can land at any point inside an episode -- it is
# the thing the loop is waiting for -- so a re-review admitted at 3h50m of a
# four-hour wait got ten minutes before this fired, on a review that had barely
# started. Anchoring here is also what makes the comparison below with
# boris_intake's own REVIEW_WAIT_SECONDS an honest one: that bound runs from the
# admission too, so the two are finally measured from the same instant.
#
# Renewable, and that is a real property of this bound, not a slip: the read
# behind it is `MAX(created_at)` with no requester or author filter, so ANY
# review request admitted at this head -- by this loop's own post, by a human,
# by anything else that can write that table -- becomes the new anchor the next
# time this route runs. A wedged loop sitting behind a head that keeps getting
# re-requested faster than every four hours never reaches this bound at all.
# That is accepted: a genuinely newer review in flight is real evidence the
# loop is not stuck, which is the entire reason `MAX` and not `MIN` is correct
# above. But say what it means plainly, because "absolute" in this constant's
# own name invites the opposite reading: the bound is four hours since the
# LAST admission recorded at this head, not four hours since the wait began,
# and not a backstop nothing can push back out.
#
# Four hours, because it must comfortably exceed boris_intake's
# REVIEW_WAIT_SECONDS (90 minutes, in its reconciler.py), which is that
# service's own bound on how long an admitted review may legitimately take --
# so any wait this one ends is a wait the review path had already given up on.
# It also exceeds the re-post budget's own span (twelve attempts one grace
# window apart, plus the first window, is 130 minutes), which used to be the only
# thing standing between a wait that spent its whole budget before the gate
# relented and an escalation the moment it did. The anchor above is what
# guarantees that now -- a late admission gets four hours of its own, however
# long the budget took -- and this margin is no longer load-bearing for it.
REREVIEW_ABSOLUTE_DEADLINE_SECONDS = 4 * 60 * 60

# How many times a system-origin REVIEW_INCOMPLETE may send the review back to
# boris_intake before a human is told instead.
#
# The one budget here counted over the loop's whole trail rather than over the
# wait it belongs to, and that is the point of it. `_repost` spends no round,
# so `round_cap` cannot bound it; it opens a fresh episode by design (a
# genuinely new request deserves a fresh wait, which is what
# EVENT_REPOST_REREVIEW_SENT exists to say), so REREVIEW_ABSOLUTE_DEADLINE_
# SECONDS restarts on every one and can never fire on this path -- boris_intake
# gives a review lane ninety minutes and this deadline is four hours. Nothing
# else looked at the pair at all: forty cycles over sixty simulated hours
# dispatched forty full Boris review lanes, spent no round, escalated nothing
# and told nobody, with the trail growing the whole time.
#
# Three, in the spirit of `round_cap` rather than of REREVIEW_REPOST_ATTEMPTS,
# because of what is being counted. That budget's twelve are retries of a
# message the gate never took -- cheap, and a lost message deserves patience.
# These are *completed Boris reviews that came back incomplete*: each one is a
# review lane that ran for up to ninety minutes and produced nothing, so the
# third one arriving is already strong evidence that re-asking is not what is
# missing. It is also the reason the escalation carries REVIEW_NEVER_COMPLETES
# rather than either re-review reason: the place to go and look is the review
# lane, not the gate's admission log and not that request's own row.
SYSTEM_REPOST_ATTEMPTS = 3

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
# The re-review was re-posted REREVIEW_REPOST_ATTEMPTS times and boris_intake
# never showed a request for it. A fourth dead end, and a fourth place to send
# a human: not the branch, not GitHub, not the kanban card, but the intake
# gate's own admission log -- whatever it keeps rejecting the post for has
# outlasted every retry this loop is willing to spend.
REREVIEW_UNACKNOWLEDGED = "rereview_unacknowledged"
# The gate DID take the request (or would not say), and no verdict arrived
# inside REREVIEW_ABSOLUTE_DEADLINE_SECONDS of it doing so -- of the wait
# beginning, in the one case where it would not say. A fifth dead end, and it
# must not share the fourth's reason even though both end the same wait: they mean
# opposite things and send a human to opposite places. `rereview_unacknowledged`
# says twelve posts were refused -- go and read why the gate keeps rejecting
# them. This says one was accepted and then nothing came of it -- go and read
# that request's own row, which is either still `reviewing` or has ended in
# `slack_unavailable`, boris_intake's second terminal state, from which no
# verdict is ever produced. The reason is what the notice names, so one literal
# for both would send every one of these to the wrong place.
REREVIEW_VERDICT_OVERDUE = "rereview_verdict_overdue"
# SYSTEM_REPOST_ATTEMPTS completed Boris reviews came back system-incomplete on
# this one loop. A sixth dead end, and a sixth place to send a human: not the
# branch, not GitHub, not the kanban card, not the intake gate's admission log
# and not one request's own row -- the review lane itself, which keeps being
# dispatched, keeps running, and keeps failing to produce a judgement. Its own
# reason for the reason every other one here has its own: the reason is what
# the notice names, and the two re-review reasons above would send whoever
# reads it to a gate that took every one of these requests without complaint.
REVIEW_NEVER_COMPLETES = "review_never_completes"

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
# One re-review request re-sent because boris_intake never showed a request for
# the one already posted. The trail is the ledger here for the same reason it
# is for the escalation debt and the head-read budget: the row has no column
# for it, and counting these is what bounds the retry -- so only a live tick
# writes one (a paused tick's decisions go to `_shadow` under a different kind,
# so a pause spends none of this budget).
EVENT_REREVIEW_REPOSTED = "rereview_reposted"
# One such re-send Slack refused. Counted against the same budget as a landed
# one, because a budget that only advances on success is no budget at all: a
# Slack that refuses forever left the route deciding and performing nothing,
# once every fifteen seconds, indefinitely -- the exact silent stall this
# route exists to end, rebuilt inside it.
#
# Its own kind, deliberately, and NOT `_lane_pushed`'s and `_repost`'s
# `rereview_post_failed`. Those two read alike but belong to other budgets --
# `_repost`'s can even be written inside one of these episodes, by a
# system-origin verdict arriving mid-wait -- and one literal serving as both
# would let an unrelated failure spend this loop's retries. Written directly
# rather than through `_note_standing`, because each attempt is a fact of its
# own and must stay individually countable. The dedup would not in fact swallow
# these today -- the detail carries a rising `attempt`, so `_repeats_last_note`
# never matches -- but that is a coincidence of the payload, not a property of
# the ledger: the day this detail stops varying, the dedup would silently keep
# the first refusal and drop the rest, and the unbounded retry this kind exists
# to bound would be back with nothing in the trail to show it. It cannot grow
# without end: REREVIEW_REPOST_ATTEMPTS bounds it at twelve.
EVENT_REREVIEW_REPOST_FAILED = "rereview_repost_failed"
# One re-review request sent by `_repost` -- the system-origin REVIEW_INCOMPLETE
# route, which spends no round and moves no head. Written when that send lands,
# and it is what tells `_rereview_episode` that a *new* request is outstanding.
#
# It exists because that route can reach `rereview_posted` from
# `rereview_posted`: a system-origin incomplete arriving at a loop already
# waiting takes the self-edge, and the transition it writes is byte-identical to
# the one the acknowledgement guard's own re-send writes ({'from':
# 'rereview_posted', 'to': 'rereview_posted'}). Told apart by the transitions
# alone they cannot be, and reading this one as a continuation is what left the
# guard inert on a fifth of live reviews: the episode stayed pinned to the
# previous request, so `request_exists` answered "already admitted" from the very
# request whose verdict this re-send is reacting to, and the guard returned None
# forever. A marker rather than trail adjacency (a self-edge with no
# `rereview_reposted` immediately before it) because adjacency is an accident of
# what nothing else happens to write in between; this says the thing directly and
# survives anything later inserted into the trail.
EVENT_REPOST_REREVIEW_SENT = "repost_rereview_sent"
# Written by store.transition() itself, and only when the swap landed.
EVENT_TRANSITION = "transition"

# The routes a decision can take, whether a verdict or a due debt prompted it.
# `_route` and `_route_due` name one, `_perform` performs it and `_rehearse`
# records it -- so the name is both the dispatch key and the word a shadow soak
# reads back. Six of them -- `_OBSERVATIONS` below: held, author_unreadable,
# head_unreadable, intake_unreadable, unknown_outcome, and unobserved, which is
# the absence of an observation rather than one -- are not acts at all: they are
# their own note kind, and a live tick does no more with them than record it
# (head_unreadable also waits out HEAD_READ_RETRY_SECONDS before the next one,
# which changes when the note is next written and nothing else).
ROUTE_SETTLE_ESCALATION = "settle_escalation"
ROUTE_HELD = "held"
ROUTE_AUTHOR_UNREADABLE = "author_unreadable"
ROUTE_HEAD_UNREADABLE = "head_unreadable"
# boris_intake's database could not be read, so whether the re-review request
# ever arrived is unknown. An observation, like `head_unreadable` above and for
# the same reason -- a live tick can only record that it asked and got no
# answer -- but with no attempt bound of its own: unlike a head read, nothing
# here can be retried into an answer, and REREVIEW_ABSOLUTE_DEADLINE_SECONDS
# already ends this wait whatever the read is doing.
ROUTE_INTAKE_UNREADABLE = "intake_unreadable"
ROUTE_UNKNOWN_OUTCOME = "unknown_outcome"
ROUTE_UNOBSERVED = "unobserved"
ROUTE_ESCALATE = "escalate"
ROUTE_CLOSE_STALE = "close_stale"
ROUTE_NOTIFY_READY = "notify_ready"
ROUTE_REPOST_REREVIEW = "repost_rereview"
ROUTE_LANE_PUSHED = "lane_pushed"
# A re-review request boris_intake never admitted, re-sent. An action, exactly
# like ROUTE_LANE_PUSHED and deliberately NOT one of the _OBSERVATIONS below:
# routing it there would hand it to `_note_standing`, whose dedup would record
# the first attempt and then perform nothing on every later tick -- reproducing
# the exact silent stall this route exists to end.
ROUTE_REREVIEW_UNACKNOWLEDGED = "rereview_unacknowledged"
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
                 ROUTE_INTAKE_UNREADABLE, ROUTE_UNKNOWN_OUTCOME,
                 ROUTE_UNOBSERVED)

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

    `retry_after`, when set, is how long the loop waits before this decision is
    retaken -- for a route that has just spent one attempt out of a budget and
    whose next attempt is worth nothing sooner. Honoured by `_perform` alone: a
    paused tick spends none of these budgets, so it has none to space out, and
    slowing the rotation a shadow soak depends on would buy nothing.

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
    retry_after: float | None = None


@dataclass(frozen=True)
class RereviewEpisode:
    """One stretch of waiting for a single outstanding re-review request.

    An episode opens when a request goes out with nothing outstanding behind it
    -- the loop entering `rereview_posted` from somewhere else, or `_repost`
    sending a fresh request into a loop already parked here -- and ends when the
    loop leaves for somewhere else: a verdict arriving, a round opening, an
    escalation. The self-edge the acknowledgement guard's own re-send takes
    stays *inside* it: the same request is still outstanding, merely sent again,
    and the retry budget is what bounds that sending.

    Named because both questions the acknowledgement guard asks are questions
    about an episode, and answering either one about the whole loop is wrong in
    a different direction. "How many re-posts has this loop made" charges a
    later round for an earlier round's retries; "does a request exist at this
    head" is answered `True` forever by a request the `_repost` path never
    moved off. See `_rereview_episode`.

    `origin` is the route that opened it -- ROUTE_LANE_PUSHED or
    ROUTE_REPOST_REREVIEW -- and it is what decides which of `slack_io`'s two
    deliberately-distinct messages a re-send may truthfully use.
    """
    started_at: float
    origin: str
    attempts: int


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


def _observe_pr(deps: LoopDeps, loop: LoopRequest) -> PrFacts:
    """Read the hold label and the pull request's state for a loop we hold.

    `_observe`'s counterpart for a pass with no verdict in hand, and one field
    shorter on purpose: the author is a question only the verdict path asks (a
    pull request that changed hands between two rounds is escalated there, once)
    and every accessor on `GitHubIO` costs its own `gh pr view` subprocess. So
    `author` stays None here, which `PrFacts` already documents as "not asked
    for", and no caller of this reads it.

    Taken only on the tick that is about to write, never on the ordinary tick
    that finds nothing owed -- see `_route_rereview_ack`, its one caller.
    """
    if deps.github.has_hold(loop.owner, loop.repo, loop.pr_number):
        return PrFacts(held=True, author=None)
    return PrFacts(held=False, author=None,
                   pr_state=deps.github.state(loop.owner, loop.repo,
                                              loop.pr_number))


def _observe_lane(deps: LoopDeps, loop: LoopRequest) -> LaneFacts | None:
    """Read the lane this loop waits on, and the PR once that lane has finished.

    None means the loop is not waiting on a lane at all, which is most of them:
    the due pass revisits every unsettled loop on every tick, so the cheap local
    kanban read is taken only for the loops a lane could still be running for,
    and the two GitHub reads only once one has actually finished. That ordering
    is also what makes the hold label a read-before-write: the lane's status is
    the last question that can be answered without writing anything.

    A kanban read that raises is answered the same way a status this build does
    not recognise is: unreadable is not finished, so the loop stays where it is
    rather than escalating on a blip -- the answer `LaneFacts` already
    documents, and the one `_lane_still_running` already gives at the sibling
    call site. Guarded rather than left to propagate, because propagating is
    not "leave the loop where it is": `KanbanIO.lane_status` raises
    `RuntimeError` on any `sqlite3.Error` and a busy or locked `kanban.db` is a
    known transient here, so the throw landed in `_settle_due`'s per-loop
    `except`, `_route_due` never ran at all, and LANE_WAIT_SECONDS -- the one
    bound that does not wait for a finished lane, and so the only one that
    could ever end this -- could never fire. Driven at the real cadence that
    left a loop `remediating` for thirty simulated hours, ten times its own
    bound, with the ownership label still held and `active_for_pr` refusing
    every later generation of that pull request.
    """
    if not _lane_pending(loop):
        return None
    try:
        status = deps.lanes.status(loop.lane_id)
    except Exception:
        _LOG.warning("lane %s could not be read for loop %s; "
                     "treating it as unfinished", loop.lane_id, loop.loop_id)
        status = None
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
    message can only ever be removed from that same message. The row's anchor
    is not frozen for the loop's life -- `_open_lane` moves it to the verdict
    that opens each round, on that round's own compare-and-swap -- but it
    always names the round that is running, which is exactly what makes
    reading it here the right pairing. Idempotent by construction -- removing
    a reaction that is gone is a no-op -- which is what lets every path that
    ends a round run it without checking whether another already did.

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


def _space_attempt(deps: LoopDeps, loop: LoopRequest, seconds: float) -> None:
    """Wait `seconds` before this loop's next attempt at whatever just failed.

    The due pass revisits a loop it leaves open every DUE_BACKOFF_SECONDS,
    which is the cadence a lane in flight wants and is far too fast to be a
    retry interval: a budget of five attempts spent one per visit gave up on an
    outage after sixty seconds, on both of the paths that keep one. Spacing is
    what turns a count into a tolerance, and it is the mechanism
    REREVIEW_REPOST_ATTEMPTS already used (`_repost_unacknowledged` defers a
    window between its own attempts) for the same reason.

    `store.defer` and deliberately not a transition: nothing here is a step the
    loop took, `updated_at` must not move (LANE_WAIT_SECONDS and the
    acknowledgement window are both measured from it), and `defer` is monotone,
    so the pass's own DUE_BACKOFF_SECONDS floor afterwards cannot cut this
    short. Unguarded, like `_repost_unacknowledged`'s: the attempt is already on
    the trail by the time this runs, so a defer that fails costs this wait and
    nothing else -- the bound still closes.
    """
    deps.store.defer(loop.loop_id, now=deps.now(),
                     not_before=deps.now() + seconds)


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
                # And waited out before the next one. The budget is spread
                # across ticks by design, but the due pass's own cadence is
                # DUE_BACKOFF_SECONDS -- so spread across *those* ticks the five
                # attempts gave up on Slack after sixty seconds and closed the
                # loop with nobody told. See NOTIFY_RETRY_SECONDS.
                _space_attempt(deps, loop, NOTIFY_RETRY_SECONDS)
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

    The send is marked on the trail (EVENT_REPOST_REREVIEW_SENT) because this
    route can be taken by a loop already sitting in `rereview_posted`, and then
    the transition below is a self-edge indistinguishable from the one the
    acknowledgement guard's own re-send takes. The marker is what makes the
    request this route just sent a new wait rather than a continuation of the
    one it interrupted -- see `_rereview_episode`. Written before the swap and
    not after it, because the fact it records is the *post*, which has already
    landed by this line: a swap lost to a lapsed lease leaves the row where it
    was, but the request is out there either way and the guard must be waiting
    on that one, not on its predecessor.
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
    deps.store.record_note(loop.loop_id, EVENT_REPOST_REREVIEW_SENT,
                           {"round": loop.round_count}, deps.now())
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


def _repost_unacknowledged(deps: LoopDeps, loop: LoopRequest, verdict: Verdict,
                           attempt: int, origin: str) -> bool:
    """Send the re-review request again, because the gate never took the first.

    Which of `slack_io`'s two messages goes out is decided by how the episode
    began, not by this route, because this route reaches `rereview_posted` from
    both of the ways in and the two messages make opposite claims. An episode
    `_lane_pushed` opened re-sends with `post_rereview`: a remediation lane
    genuinely ran and genuinely pushed, so "Round N of M -- remediation pushed"
    is true and there is a round to name. An episode `_repost` opened re-sends
    with `repost_rereview`: nothing was pushed and no round was spent, so
    `post_rereview` there would announce a push that never happened under a
    "Round 0 of 3" that means nothing -- the exact conflation slack_io.py keeps
    two methods to prevent, and a human scanning the thread would have no way
    to tell which of the two actually occurred.

    The round is never spent either way: this re-sends a message that never
    arrived, it does not start a new round. `round_count` is unchanged.

    Posted before the row is moved on, the ordering `_lane_pushed` uses and for
    the same reason: a row moved ahead of a post that never landed would say a
    request is outstanding when none is, and re-arm a wait for it. So a refused
    post leaves the loop exactly where it was -- but it does spend an attempt,
    and it does wait out a window before the next one, both below.

    The self-edge into REREVIEW_POSTED is what re-arms that window -- the wait
    is measured from `updated_at`, and only a real transition moves it (see
    `_route_rereview_ack`). It is already in the store's transition table; this
    adds no edge. It stays inside the episode, so the retry budget goes on
    counting across it (see `_rereview_episode`).

    Nothing guards against the request having been admitted after all between
    the routing read and this post, and the gate does not fully close that race
    either. Admission dedups on `review_identity` AND `gate_fingerprint`
    (boris_intake/store.py) -- the first keyed on the head sha, the second a
    hash of the PR's CI check states at capture time (its result.py) -- so a
    re-post landing on top of an admitted request is rejected as a duplicate
    only while the checks have not moved. On a pull request whose head was just
    pushed, CI is still running and those states move constantly, so a `catch_up`
    that admits the original moments after our routing read False can leave this
    post admitted as a second full review. That is a bounded, narrow chance of
    one duplicate review, and it is accepted deliberately: a needless re-post
    costs at worst a second review, a needless escalation costs the pull request
    permanently. That asymmetry is the whole reason this route re-posts rather
    than escalating.
    """
    # root_thread_ts, not status_message_ts -- the same split `_repost` and
    # `_lane_pushed` observe, for the same reason.
    landed = (deps.slack.post_rereview(verdict, loop.root_thread_ts,
                                       loop.round_count)
              if origin == ROUTE_LANE_PUSHED
              else deps.slack.repost_rereview(verdict, loop.root_thread_ts))
    if landed is None:
        # Recorded directly and NOT through `_note_standing`, which is what
        # `_lane_pushed`'s sibling failure uses: each of these is one of the
        # twelve and has to stay individually countable. As the detail stands
        # the dedup would not in fact swallow any of them -- `attempt` rises on
        # every one, so `_repeats_last_note` can never match -- and that is
        # precisely the reason not to route them through it: it would read as a
        # safeguard while doing nothing, and this budget's countability would
        # rest on a field of the payload rather than on the call. It rested on
        # nothing at all before that field existed, and the dedup then kept the
        # first refusal and swallowed every later one, which is how a Slack that
        # refused forever produced one sentence and an unbounded retry.
        deps.store.record_note(loop.loop_id, EVENT_REREVIEW_REPOST_FAILED,
                               {"round": loop.round_count, "attempt": attempt},
                               deps.now())
        # And waited on, rather than retried on the next tick. The loop stays
        # where it is -- the row must not claim a request is outstanding when
        # none is -- so `updated_at` does not move and the routing window
        # cannot do this waiting; the due time is what is pushed out instead.
        # `defer` is monotone, so the pass's own backoff cannot cut it short.
        # A defer that fails costs this wait and nothing more: the attempt is
        # already on record above, so the bound still closes.
        deps.store.defer(loop.loop_id, now=deps.now(),
                         not_before=deps.now() + REREVIEW_ACK_GRACE_SECONDS)
        return False
    deps.store.record_note(loop.loop_id, EVENT_REREVIEW_REPOSTED,
                           {"attempt": attempt}, deps.now())
    return _transition(deps, loop, LoopState.REREVIEW_POSTED)


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
        elif kind == EVENT_TRANSITION and detail.get("lane_id") or kind == EVENT_ORPHAN_SUPERSEDED:
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

    # One more read before the write, same as the claim above: the brief
    # needs the PR's base and head branch names (lanes.build_brief), and
    # GitHubIO.branches() answers both from the single `_view()` call every
    # other accessor on that adapter already makes -- not a second round
    # trip. A GitHub that cannot answer this must not block the round: None
    # degrades to the brief saying so (see build_brief), never to a raise or
    # a refusal here -- the round this verdict earned is not forfeit over a
    # brief that can only be slightly less specific.
    base_branch, head_branch = deps.github.branches(
        verdict.owner, verdict.repo, verdict.pr_number)

    # Same pass, same reasoning: the brief also carries an inventory of
    # unresolved Codex/CodeQL review threads (lanes.build_brief), which is
    # coverage pr-review-hourly still tracked and this loop, until now, did
    # not. Unlike branches() above, this does NOT share `_view()` -- it is
    # its own `gh api graphql` call, a second round trip, not a second field
    # off the first. `unresolved_bot_threads` already turns a failed or
    # truncated read into None rather than raising (see its docstring), and
    # that None must not block, escalate, or hold the lane open here either:
    # it only changes what the brief says, degrading to its own uncertain
    # sentence, the same way an unreadable branches() does above.
    bot_threads = deps.github.unresolved_bot_threads(
        verdict.owner, verdict.repo, verdict.pr_number)

    lane_id = deps.lanes.create(verdict, round_number,
                                base_branch=base_branch, head_branch=head_branch,
                                bot_threads=bot_threads)
    if lane_id is None:
        # Dispatch refused (no worktree anchor, an excluded infra repo, a
        # kanban failure). No lane means no remediation, so the round stays
        # unspent and a human takes over.
        return _escalate(deps, loop, verdict, EscalationReason.LANE_FAILED.value)

    # Where this round's reaction belongs. A loop outlives the verdict that
    # opened it and every Boris verdict posts its own status message, so an
    # anchor written once at `open_loop` and never refreshed pins every later
    # round's reaction to round 1's message -- several messages up the thread
    # from the round that is actually running, which then shows no signal at
    # all. A verdict that carries none keeps the anchor the row already has,
    # deliberately: a null says nothing about where the previous round's
    # reaction is, and blanking the row on its account would strand that
    # reaction for good, since `_clear_running_signal` only ever reads the row.
    anchor = verdict.status_message_ts or loop.status_message_ts

    # The lane exists from here on. Record the round before decorating Boris's
    # status message: if the compare-and-swap is lost we are no longer the
    # driver, and a thinkies reaction would claim a round this tick does not
    # own. The anchor moves on that same swap rather than in a write of its
    # own, for the same reason and in the same breath: a lost swap must leave
    # the previous round's anchor exactly where it was.
    if not _transition(deps, loop, LoopState.REMEDIATING,
                       round_count=round_number, lane_id=lane_id,
                       head_sha=verdict.head_sha, status_message_ts=anchor,
                       next_attempt_at=deps.now() + LANE_POLL_SECONDS):
        # Everything a later tick needs to adopt this lane rather than open a
        # rival one: the row itself will carry no trace of it.
        deps.store.record_note(loop.loop_id, EVENT_ORPHANED_LANE,
                               {"lane_id": lane_id, "round": round_number,
                                "head_sha": verdict.head_sha}, deps.now())
        return False
    if loop.status_message_ts != anchor:
        # The message this round is leaving behind. Its reaction is normally
        # already gone -- every route out of a running round clears it before
        # a later verdict can reach here (`_lane_pushed`, `_await_head_move`,
        # and the stand-downs, which close the loop instead) -- but every one
        # of those removals is best-effort: SlackIO swallows the failure and
        # records nothing. While the anchor never moved, such a removal was
        # retried by the loop's next one; now that it moves, no later
        # `_clear_running_signal` can ever name that message again, so the one
        # dropped removal would be stuck on it forever. Removing a reaction
        # that is gone is a no-op, so this costs one idempotent call on the
        # ordinary path and closes the only way moving the anchor can strand
        # one.
        _clear_running_signal(deps, loop)
    # Anchored on Boris's status message, not root_thread_ts -- see
    # _clear_running_signal for why the two anchors must never be unified.
    if anchor:
        deps.slack.add_thinkies(deps.cfg.boris_channel, anchor)
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

    if _lane_in_flight(loop):
        # A second lane on this branch is the collision this service exists to
        # remove, and here we would be causing it ourselves. Asked once, ahead
        # of the whole outcome dispatch, rather than inside the one branch that
        # dispatches a lane -- because opening a second lane is not the only way
        # to end up with two. A system-origin REVIEW_INCOMPLETE used to swap
        # `remediating -> rereview_posted` with the lane still running, after
        # which `_lane_in_flight` answered False and the very next ISSUES_FOUND
        # opened a rival lane against the same branch; kanban's idempotency key
        # does not cover that pair either, because it embeds the round. Checked
        # here, no outcome added later can forget it -- the same argument
        # `_consumer` wins for the mode gate, and for the same reason: a rule
        # spelled out once per branch is a rule one branch will eventually miss.
        #
        # It covers the two outcomes that would *close* the loop as well, and
        # that widening is deliberate. Both of them stand the pull request down
        # -- `hermes-owner-andrej` released, the reaction cleared -- which is
        # exactly what `_settle_escalation` refuses to do over a lane kanban has
        # not confirmed finished, for exactly the reason given there: a human
        # reads that label coming off as permission to touch the branch, while a
        # worker in a worktree this loop cannot cancel is still able to push to
        # it. Closing also abandons that lane for good (`_close` can report it
        # and nothing more), on a verdict computed against a head the lane is in
        # the middle of replacing -- so a READY closed here tells a human the
        # pull request is finished while unreviewed commits are still landing on
        # it.
        #
        # Deferring costs none of what closing would have bought. The lane in
        # flight drives the next step: it finishes, the head moves, the
        # re-review goes out, and *that* verdict decides the loop -- and the
        # whole wait is bounded by LANE_WAIT_SECONDS, so this can never become
        # the "pin the pull request open for good" failure that makes abandoning
        # a lane the lesser evil inside `_close`. Ahead of the round cap too,
        # because the round this verdict would spend has already been spent by
        # the lane in flight.
        return Route(ROUTE_DEFER, {"lane_id": loop.lane_id},
                     defer=LANE_IN_FLIGHT)

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
        sent = _system_reposts(deps, loop)
        if sent >= SYSTEM_REPOST_ATTEMPTS:
            # Read off the loop's whole trail, deliberately, and not off the
            # episode this verdict is about to end: `_repost` opens a new
            # episode every time it sends, which is correct (a new request
            # deserves a fresh wait) and is exactly why the episode's own
            # budget and deadline can bound nothing here. This is the only
            # bound on how many times that may happen at all -- no round is
            # spent, so `round_cap` never sees it either.
            return Route(ROUTE_ESCALATE,
                         {"reason": REVIEW_NEVER_COMPLETES, "sent": sent})
        return Route(ROUTE_REPOST_REREVIEW, {"round": loop.round_count + 1,
                                             "sent": sent + 1})
    if verdict.outcome != "ISSUES_FOUND":
        return Route(ROUTE_UNKNOWN_OUTCOME, {"outcome": verdict.outcome})

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


def _system_reposts(deps: LoopDeps, loop: LoopRequest) -> int:
    """How many times this loop has already sent the review back to Boris.

    Counted over the whole trail and never reset, unlike every other budget
    replayed here. `_head_read_attempts` resets on a landed read and
    `_rereview_episode` resets on a new request, both because what those budgets
    bound is a *retry* and a retry that finally succeeded is not evidence about
    the next one. This bounds something else: the number of completed Boris
    reviews that came back with no judgement at all. Each one is its own
    success -- the gate took the request, a review lane ran, it finished -- so
    there is no event here that could honestly reset it, and any reset would be
    the very thing that left this unbounded, since the marker that would do the
    resetting is the marker being counted.

    Counts the sends a live tick actually made (EVENT_REPOST_REREVIEW_SENT is
    written only by `_repost`, and only once the post has landed), so a paused
    tick spends none of this budget and a Slack that refused the send costs
    none of it either -- the same property `_head_read_attempts` and
    `_rereview_episode` have, for the same reason.
    """
    return sum(1 for event in deps.store.events(loop.loop_id)
               if event["kind"] == EVENT_REPOST_REREVIEW_SENT)


def _rereview_episode(deps: LoopDeps, loop: LoopRequest) -> RereviewEpisode:
    """Replay the trail for the wait this loop is currently in.

    Counted off the trail rather than kept in a column, the same way the
    escalation debt and the head-read budget are, and for the same reason:
    `transition()` writes a fixed set of columns and there is no room in the row
    for any of this. Like `_head_read_attempts`, it counts what a live tick
    actually wrote, so a pause spends none of the budget -- a paused tick's
    decisions are recorded by `_shadow` under `shadow_decision` and never as
    these kinds.

    An attempt is an attempt, landed or refused: a budget that only advanced on
    a landed post never advanced at all while Slack was refusing, so the one
    failure mode with no way out -- a post that can never land -- was the one
    the bound did not cover.

    Scoped to the episode, exactly as `_head_read_attempts` is scoped to the
    lane's trailing run, and for the same reason: a budget that is never reset
    is spent for good. A head that moves ends this *wait* -- moving it takes a
    lane, a lane takes a round, and a round leaves `rereview_posted` -- but it
    does not clear the *count*. So a loop that spent eleven re-posts riding out
    a GitHub incident in round one escalates in round two on its second blip,
    and one that spent twelve escalates on its first, with no retry at all.
    Escalation is absorbing, so that permanently strands a pull request over a
    transient `gate_snapshot_unavailable` -- precisely what
    REREVIEW_REPOST_ATTEMPTS exists to prevent. Nor is the number of episodes
    bounded by `round_cap`: `_repost` re-enters this state spending no round.

    The guard's *own* self-edge deliberately does NOT reset it. A re-post
    re-arms the wait without ending it -- the same request is still the one
    outstanding -- and resetting there would zero the budget on every attempt,
    making escalation unreachable. That is the same permanent silence read from
    the other end.

    `_repost`'s self-edge is the other thing entirely, and telling the two apart
    is what EVENT_REPOST_REREVIEW_SENT is for. A system-origin incomplete
    arriving at a loop already parked here sends a *new* request and leaves the
    old one moot -- so that is a new wait, with its own budget, its own deadline
    and its own message, and the transition it writes is byte-identical to the
    guard's. Read as a continuation instead, the episode stayed pinned to the
    previous request and `request_exists` answered "already admitted" from the
    very request whose verdict prompted the re-send: the guard then never fired
    once, on the route that produces a fifth of live verdicts. This is not the
    self-edge reset above wearing a different hat -- a marker is written only
    where a genuinely new request went out, and only a completed review can
    prompt one, so no amount of re-sending by the guard itself can refresh a
    budget it is spending.

    `origin` otherwise comes off the opening transition itself rather than a new
    column: `_lane_pushed` is the only route that records the head its lane
    pushed on this edge, and `_repost`, which moves no head, writes none. A row
    with no such transition on record at all -- hand-edited, or older than this
    trail -- falls back to `updated_at` for when the wait began and to the
    message that claims the less of the two, since nothing here can evidence a
    push.
    """
    posted = LoopState.REREVIEW_POSTED.value
    started_at, origin, attempts = loop.updated_at, ROUTE_REPOST_REREVIEW, 0
    for event in deps.store.events(loop.loop_id):
        kind, detail = event["kind"], event["detail"]
        if kind in (EVENT_REREVIEW_REPOSTED, EVENT_REREVIEW_REPOST_FAILED):
            attempts += 1
        elif kind == EVENT_REPOST_REREVIEW_SENT:
            # A new request, whether or not the swap that follows it is a
            # self-edge. On the way in from anywhere else the transition below
            # says the same thing at the same instant, so this is simply the
            # opener that also works when the loop was already here.
            started_at, origin, attempts = event["at"], ROUTE_REPOST_REREVIEW, 0
        elif kind == EVENT_TRANSITION and detail.get("to") == posted \
                and detail.get("from") != posted:
            started_at, attempts = event["at"], 0
            origin = (ROUTE_LANE_PUSHED if "head_sha" in detail
                      else ROUTE_REPOST_REREVIEW)
    return RereviewEpisode(started_at, origin, attempts)


def _route_rereview_ack(deps: LoopDeps, loop: LoopRequest,
                        observes: bool) -> Route | None:
    """Did the re-review request this loop posted ever reach boris_intake?

    `rereview_posted` is left only by an arriving verdict, and the verdict comes
    only if the intake gate admitted the post. That gate reads GitHub
    synchronously while admitting, and a failed read is indistinguishable from a
    missing pull request -- so a GitHub outage rejects the post outright
    (`pr_not_found`, `gate_snapshot_unavailable`). Three loops parked that way in
    one afternoon: nothing recorded, no alarm, and no rescue from pr_discovery
    either, which treats this loop's own ownership label as a kill switch.

    The admission check is a local sqlite read, like the trail replays above it
    in `_route_due` and unlike the GitHub read below -- which is why it sits
    where it does and needs no `_observes` gate of its own. It decides and
    performs nothing; `_perform` re-posts and `_rehearse` records.

    This is the one routing function that reads GitHub itself rather than being
    handed facts, and that is not a hole in the discipline the rest of the
    module keeps -- it is what that discipline costs here. Everywhere else,
    *whether* to read is a question about the operating mode alone, so the read
    hoists cleanly out to `_observe`/`_observe_lane`. Here it is a question
    about the two answers below as well: on the ordinary tick this route owes
    nothing, and a read hoisted above them would spend a `gh pr view` per open
    `rereview_posted` loop per tick to learn that. So the read stays where the
    write is, `observes` carries the mode's answer in from `_settle_due` (the
    routing still never asks the mode itself), and an `off` tick reads nothing
    from GitHub exactly as it reads no lane.

    The question asked is "has a request been admitted since this episode
    began", not "does a request exist at this head" -- see `_rereview_episode`
    for what an episode is. The narrower question is not a refinement, it is the
    only version that works on both routes into this state. `_repost` transitions
    without touching `head_sha`, so the request that produced the very verdict
    which opened the loop is still sitting at the loop's head: the broader
    question answers True there unconditionally, this guard returns None
    forever, and a rejected `repost_rereview` strands the loop exactly as
    production does today. On `_lane_pushed`'s route it changes nothing
    observable, the head being new.

    The three reads are ordered by what they cost and by how often they can
    change the answer. The grace window first: two numbers already in hand, and
    inside it nothing else matters. Then the admission read, a database this
    process does not own but a local one. Then -- only on the branch that is
    about to post -- GitHub, the read that costs a subprocess and can only ever
    be spent on the write path. So the ordinary case, a loop waiting out its
    window or admitted and simply waiting for its verdict, costs nothing but a
    comparison and at most one sqlite query.

    What the read comes back with then decides which bound applies, because the
    two bounds here answer different questions and each is wrong for the other's
    case. The read answers with an `AdmissionLookup` rather than a `bool | None`
    -- three states still, and the same three, but ADMITTED now carries the epoch
    the gate took the request at, which is what the deadline below is anchored
    on:

    NOT_ADMITTED -- the gate never admitted this request, so nobody is reviewing
    anything and the re-post budget governs. This is the branch that writes, so
    it is the branch that reads GitHub: a hold stops it (Chad has taken the pull
    request back, and boris_intake's own gate has no hold check to stop the
    review this post would start), and a pull request that is no longer open
    closes the loop `stale` instead of spending twelve rejected retries and
    escalating over somebody's own merge. Twelve attempts a window apart
    already bounds this at 130 minutes, counted from the first tick that finds
    it, so the absolute deadline is deliberately NOT consulted: measured from
    when the wait began, it would escalate every loop already older than four
    hours on the first tick that ever looked at it, without one retry. That is
    the entire existing backlog -- the loops this guard was written to rescue
    are hours old before it first runs -- and it would make each one's fate
    depend on when this service happened to be deployed.

    UNREADABLE -- the intake database could not be read. Not NOT_ADMITTED:
    waiting costs a tick, guessing costs a duplicate review on a PR whose review
    may well be live. Unlike the other do-nothings it is *recorded*, because it
    is the one that can repeat indefinitely on an external fault, and a branch
    that repeats indefinitely writing nothing is the shape this whole route
    exists to remove. Standing, so an outage lasting a day leaves one sentence.
    The deadline does apply here: nothing about an unreadable intake can be
    retried into an answer, so there is no budget for it to have and no bound
    but time. It carries no admission time -- there is nothing to carry -- so the
    deadline falls back to the episode start and this branch keeps exactly the
    bound it has always had.

    ADMITTED -- a request for this episode is on record, whatever workflow_state
    it is in, so re-posting would be noise on top of a review that already
    exists. It does NOT promise a verdict is coming, and this is the case the
    deadline exists for: a request that ends in `slack_unavailable`, or wedges in
    `reviewing`, holds this wait open for good. No re-post is owed on this path,
    so the budget can bound nothing here and only elapsed time can -- elapsed
    since the *admission*, which is the one thing on this path that says how long
    the review has actually had.

    Three anchors, then, and they are three on purpose; unifying any two of them
    is a regression each way. The grace window is measured from `updated_at`,
    which is when the loop entered `rereview_posted` or last re-sent (the
    self-edge is what re-arms it), and which `store.defer` deliberately leaves
    alone, so a due-pass visit does not push it out. Deliberately NOT measured
    from the episode start, which does not move on a re-send and would fire this
    route on every tick after the first. A *refused* re-send moves neither, and
    waits the same interval out through the due time instead -- see
    `_repost_unacknowledged`. The admission question is asked from the episode
    start, because that is what makes it this wait's own request and not a
    predecessor's. The deadline is anchored later still, at the admission itself
    -- see the comment on `anchor` below.

    At the attempt bound -- escalate rather than go on re-posting forever. The
    reason is its own (REREVIEW_UNACKNOWLEDGED) because the place to go and look
    is its own: not the branch, not GitHub, not the kanban card, but whatever
    the gate keeps rejecting the post for. The deadline's escalation carries a
    different reason again (REREVIEW_VERDICT_OVERDUE) for exactly the same
    reason: it means a request was accepted and then had four full hours from
    its own admission and produced nothing, which is a different thing to go and
    read -- that request's own row -- and the reason is what the notice names.
    Anchored at the wait instead, that notice could name a row whose review had
    barely begun, which is the same lie in the opposite direction.
    """
    episode = _rereview_episode(deps, loop)
    if deps.now() < loop.updated_at + REREVIEW_ACK_GRACE_SECONDS:
        return None
    admission = deps.source.request_exists(loop.owner, loop.repo, loop.pr_number,
                                           loop.head_sha, episode.started_at)
    if admission.not_admitted:
        # The gate never took it, so this pass is about to write -- and every
        # other writing route in this module reads GitHub first. This one is
        # where that read has to live rather than in `_observe_lane` with the
        # others, because *whether* it is owed is a question only the two
        # answers above can settle, and taking it up there would spend a `gh pr
        # view` per open loop per tick on the loops that owe nothing at all.
        # None means the tick looked at nothing (`off`), which is not a hold
        # Chad did not put there and not a pull request GitHub called merged.
        facts = _observe_pr(deps, loop) if observes else None
        if facts is None:
            return Route(ROUTE_UNOBSERVED, {"mode": deps.cfg.mode}, standing=True)
        if facts.held:
            # Chad's kill switch, and the fail-closed answer when GitHub cannot
            # be read at all. Without it a held pull request still got its
            # re-review re-posted, boris_intake's own gate has no hold check,
            # and a full review then published a GitHub review on a pull request
            # Chad had explicitly taken back.
            return Route(ROUTE_HELD, {"label": deps.cfg.hold_label},
                         standing=True)
        if _not_open(facts.pr_state):
            # Chad merging while Boris re-reviews is the ordinary ending, and
            # the gate rejects every re-post for a pull request that is no
            # longer open -- so without this the loop spent all twelve retries
            # over two hours and escalated as `rereview_unacknowledged`, paging
            # the person who merged it about their own merge and sending them
            # to the intake gate's admission log to find out why.
            return Route(ROUTE_CLOSE_STALE, {"pr_state": facts.pr_state},
                         standing=True)
        # The budget governs this path, and it returns ahead of the deadline
        # below rather than being bounded by it -- see
        # REREVIEW_ABSOLUTE_DEADLINE_SECONDS. There is nothing to anchor a
        # deadline to here in any case: the gate took no request, so the only
        # timestamp on offer is the wait's own, which is what made that
        # deadline the wrong bound for this branch.
        if episode.attempts >= REREVIEW_REPOST_ATTEMPTS:
            return Route(ROUTE_ESCALATE, {"reason": REREVIEW_UNACKNOWLEDGED},
                         standing=True)
        # `origin` rides along so the act can pick the message this episode can
        # truthfully carry without replaying the trail a second time -- see
        # `_repost_unacknowledged`.
        return Route(ROUTE_REREVIEW_UNACKNOWLEDGED,
                     {"attempt": episode.attempts + 1, "origin": episode.origin},
                     standing=True)
    # The deadline's anchor, and the whole reason this read answers with a time.
    #
    # `episode.started_at` is the fallback, not the anchor: it is what an
    # UNREADABLE intake gets, because with no admission time available there is
    # nothing else to anchor to and this branch must stay bounded -- it is the
    # one that can repeat indefinitely on an external fault, and it has no
    # retry budget of its own. It is also what a caller with no `admitted_at`
    # at all would get, so this cannot become unbounded by omission.
    #
    # An ADMITTED request is anchored at its own admission instead. Anchored at
    # the wait, a request admitted at 3h50m of a four-hour wait got ten minutes
    # before this escalated it, carrying REREVIEW_VERDICT_OVERDUE -- which sends
    # a human to read that request's own row and find a review that started
    # moments ago and is running perfectly. The deadline is meant to catch "a
    # request was accepted and produced nothing", never "was accepted and has
    # barely begun". A manual re-kick of a long-stranded pull request lands
    # exactly there, which is how this was found.
    #
    # `max` is defence in depth and cannot fire today. The single call site
    # below passes `since = episode.started_at`, and the query above filters
    # `created_at >= since` -- so every admission it can return already
    # satisfies `admitted_at >= episode.started_at`, whatever `started_at` is.
    # That is the whole reason, and it is a property of the query, not of how
    # `_rereview_episode` arrived at `started_at`: that trail is not even
    # forward-moving in general (a `repost_rereview_sent` marker overwrites it
    # unconditionally from that event's own `at`, which can precede an earlier
    # transition), and the floor still cannot fire, because the same episode
    # that windows the query is the one the anchor is computed from, in the
    # same tick. It stands against a future caller that windows on something
    # else, and is deliberately not tested -- there is no live path that
    # reaches it.
    #
    # `<= deps.now()` is the clamp, and it is load-bearing rather than
    # defensive: `created_at` is another process's epoch column, and a row from
    # the future -- clock skew, a manual repair, a bad migration -- would push
    # this bound out by exactly that skew, so one bad row would disable the
    # backstop for the life of the loop. A timestamp that has not happened yet
    # evidences nothing about how long a review has been running, so it buys no
    # window at all and the wait keeps the bound it would have had anyway.
    #
    # A guard and NOT `min(deps.now(), max(...))`, which is the tidier-looking
    # form and does nothing whatever: `deps.now() >= min(deps.now(), X) + D` is
    # the same predicate as `deps.now() >= X + D` for every positive D -- below
    # the clock the `min` picks X, and above it both sides are false -- so that
    # form leaves the bound disabled for exactly as long as the bad row is
    # dated into the future, which is the failure it appears to prevent. It is
    # also untestable, being a no-op. Do not tidy this into it.
    anchor = episode.started_at
    if admission.admitted_at is not None and admission.admitted_at <= deps.now():
        anchor = max(anchor, admission.admitted_at)
    if deps.now() >= anchor + REREVIEW_ABSOLUTE_DEADLINE_SECONDS:
        return Route(ROUTE_ESCALATE, {"reason": REREVIEW_VERDICT_OVERDUE},
                     standing=True)
    if admission.unreadable:
        return Route(ROUTE_INTAKE_UNREADABLE, {"head_sha": loop.head_sha},
                     standing=True)
    return None


def _route_due(deps: LoopDeps, loop: LoopRequest, lane: LaneFacts | None,
               observes: bool) -> Route | None:
    """Decide what a live tick owes a loop no verdict is going to arrive for.

    `_route`'s counterpart for the due pass, under the same discipline: it
    decides and performs none of it. What kanban and GitHub say arrives as
    `lane`, already read, for the reason `_route` takes `facts` that way --
    whether to read at all is a question about the operating mode, and `None`
    is both "this loop waits on no lane" and "this tick looked at nothing".

    `observes` carries the same mode answer for the one read this pass cannot
    hoist, the acknowledgement guard's -- see `_route_rereview_ack`. It is the
    answer `_observes` already gave in `_settle_due`, passed down rather than
    asked again, so the routing still never consults `cfg.mode` itself.

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

    if loop.state is LoopState.REREVIEW_POSTED:
        # Ahead of the `lane is None` return below, because that return is
        # exactly what strands these loops: `_lane_pending` excludes
        # `rereview_posted` (correctly -- `lane_id` outlives the round that ran,
        # and reading it back as a fresh completion would post the re-review
        # twice and then call the unmoved head a no-op), so `_observe_lane`
        # answers None here and every tick fell straight through it.
        #
        # Behind the two above, because both concern writes already owed. A
        # stopped loop's escalation debt outranks a request there is no longer
        # anyone waiting on, and a lost lane-recording write must be settled
        # before a route that moves the row.
        return _route_rereview_ack(deps, loop, observes)

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
                     standing=True, retry_after=HEAD_READ_RETRY_SECONDS)
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
        if route.retry_after is not None:
            # An observation that is also one attempt out of a budget: the note
            # above is what spends it, and this is what stops the next one being
            # spent fifteen seconds later. Read off the route rather than off the
            # action, so a second such observation cannot arrive here and be
            # silently retried at the pass's own cadence.
            _space_attempt(deps, loop, route.retry_after)
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
    if route.action == ROUTE_REREVIEW_UNACKNOWLEDGED:
        return _repost_unacknowledged(deps, loop, verdict,
                                      int(route.detail["attempt"]),
                                      str(route.detail["origin"]))
    if route.action == ROUTE_AWAIT_HEAD_MOVE:
        return _await_head_move(deps, loop)
    if route.action == ROUTE_STILL_RUNNING:
        return _notify_still_running(deps, loop, verdict, route.detail)
    if route.action == ROUTE_ADOPT_LANE:
        return _adopt_lane(deps, loop, route.detail)
    if route.action == ROUTE_DROP_ORPHAN:
        return _drop_orphan(deps, loop, route.detail)
    if route.action == ROUTE_OPEN_LANE:
        opened = _open_lane(deps, loop, verdict, int(route.detail["round"]))
        jev_gate.shadow(deps, loop, verdict)
        return opened
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
    if route.action == ROUTE_OPEN_LANE:
        jev_gate.shadow(deps, loop, verdict)
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
                               _observe_lane(deps, loop) if observes else None,
                               observes)
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
