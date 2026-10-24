"""The driver: verdict routing, round accounting, and the claim discipline.

Every fake here is a fake -- no GitHub, no Slack, no kanban, no network. The
store is the real one against a tmp_path database, because the lease and
compare-and-swap semantics the driver depends on are exactly what these tests
are checking.
"""
import pytest

from pr_remediation.boris_source import (AdmissionLookup, AdmissionStatus,
                                         Issue, Verdict)
from pr_remediation.config import RemediationConfig
from pr_remediation.models import LoopState
from pr_remediation.store import Store


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=30.0):
        self.value += seconds


class SourceFake:
    def __init__(self, verdicts=(), admitted=True, admitted_at=None):
        self.verdicts = list(verdicts)
        # What request_exists() answers -- boris_intake's own account of
        # whether a re-review request exists at the head asked about. True is
        # the default because it is the ordinary case (the gate admitted the
        # post) and the one every test that is not about the acknowledgement
        # check assumes: an admitted request has a verdict coming, so the loop
        # owes nothing. False is a rejected post; None is an intake database
        # this process could not read. The three map onto the three
        # `AdmissionStatus` values the real read answers with -- kept as a
        # tri-state flag here because that is how every existing test spells the
        # case it is about.
        self.admitted = admitted
        # When the one request in this fake's world was admitted, for the tests
        # that care *when* rather than only whether. Set it and the answer is
        # computed the way the real query computes it (`created_at >= since`),
        # which is the only way to model the `_repost` path at all: there the
        # stale request sits at exactly the head being asked about, so nothing
        # but the timestamp tells it apart. It is also the anchor the absolute
        # deadline is measured from, so a test that parks a wait and then has
        # the gate admit late sets this and nothing else.
        #
        # Leaving it unset with `admitted=True` means "admitted at the instant
        # the wait began", which is both the ordinary live case (the gate admits
        # synchronously, seconds after the post) and the earliest thing the real
        # query can return -- it windows on `created_at >= since`, so an
        # admission that predates the episode is invisible to it. This fake
        # cannot return one either, deliberately: no test should be able to
        # assert a branch production cannot reach.
        self.admitted_at = admitted_at
        # Every (owner, repo, pr_number, head_sha, since) the loop asked
        # about, so a test can pin that it asks at its *own* head rather than
        # at whatever head the round started from.
        self.exists_calls = []

    def new_verdicts(self, limit=50):
        return tuple(self.verdicts)

    def request_exists(self, owner, repo, pr_number, head_sha, since):
        self.exists_calls.append((owner, repo, pr_number, head_sha, since))
        if self.admitted_at is not None:
            if self.admitted_at < since:
                return AdmissionLookup(AdmissionStatus.NOT_ADMITTED)
            return AdmissionLookup(AdmissionStatus.ADMITTED,
                                   admitted_at=self.admitted_at)
        if self.admitted is None:
            return AdmissionLookup(AdmissionStatus.UNREADABLE)
        if self.admitted:
            return AdmissionLookup(AdmissionStatus.ADMITTED, admitted_at=since)
        return AdmissionLookup(AdmissionStatus.NOT_ADMITTED)


_READABLE_EMPTY = object()


class GitHubFake:
    """claim/release return bool, as GitHubIO now does. ``bot_threads=None`` is
    an unreadable GitHub; the default is a readable PR with no bot threads."""

    def __init__(self, head="a" * 40, author="Chadtao206", hold=False,
                 claim_ok=True, release_ok=True, hold_raises=(),
                 pr_state="OPEN", bot_threads=_READABLE_EMPTY,
                 base_branch="main", head_branch="chad/example-branch"):
        if bot_threads is _READABLE_EMPTY:
            bot_threads = []
        self.head, self.author_login, self.hold = head, author, hold
        self.claim_ok, self.release_ok = claim_ok, release_ok
        self.hold_raises = set(hold_raises)
        self.claimed, self.released = [], []
        # What branches() reports -- the (base, head) branch names a
        # remediation brief needs (see lanes.build_brief). Defaulted to
        # ordinary-looking names so the many tests that never mention
        # branches at all still exercise a lane dispatch that has them.
        self.base_branch, self.head_branch = base_branch, head_branch
        self.branch_calls = []
        # What `gh pr view --json state` reports: OPEN / MERGED / CLOSED, or
        # None when GitHub could not be read.
        self.pr_state = pr_state
        # What unresolved_bot_threads() returns: [] (the default) means "read
        # succeeded, nothing outstanding" -- the ordinary, clean case every
        # existing test exercises without knowing this parameter exists.
        # None means the read itself failed. A non-empty list of dicts
        # ({"path": ..., "outdated": ...}) means real bot threads are open.
        #
        # A LIST, not the tuple this used to default to. The real adapter's
        # contract is `list | None` and never a tuple, and lanes.py once read
        # "clean" two different ways -- `not bot_threads` in the section,
        # `== []` in the acceptance bullet -- which agreed for `[]` and
        # disagreed for `()`. Every "clean PR" test here ran on the one value
        # that told them apart, so a fake that is not shaped like the thing it
        # stands in for is how that survived. A mutable default is safe here:
        # nothing on this class or in the code under test mutates it.
        self.bot_threads = bot_threads
        self.bot_thread_calls = []

    def head_sha(self, o, r, p):
        return self.head

    def author(self, o, r, p):
        return self.author_login

    def state(self, o, r, p):
        return self.pr_state

    def branches(self, o, r, p):
        self.branch_calls.append((o, r, p))
        return self.base_branch, self.head_branch

    def has_hold(self, o, r, p):
        if p in self.hold_raises:
            raise RuntimeError(f"github exploded reading {o}/{r}#{p}")
        return self.hold

    def claim(self, o, r, p):
        self.claimed.append((o, r, p))
        return self.claim_ok

    def release(self, o, r, p):
        self.released.append((o, r, p))
        return self.release_ok

    def unresolved_bot_threads(self, o, r, p):
        self.bot_thread_calls.append((o, r, p))
        return self.bot_threads


class SlackFake:
    def __init__(self, repost_ts="9.0", ready_ts="9.1", escalation_ts="9.2"):
        self.reposts, self.ready, self.escalations = [], [], []
        self.thinkies_added, self.thinkies_removed = [], []
        # The thread_ts each post_rereview call actually targeted -- kept
        # apart from `reposts` (which every existing assertion already reads
        # as (slug, round) tuples) so a test can pin the target without
        # touching every one of those.
        self.repost_targets = []
        # `_lane_pushed` calls post_rereview; `_repost` (the system-origin
        # incomplete path) calls repost_rereview. Tracked in separate lists,
        # deliberately, so a test can tell which of loop.py's two call sites
        # actually fired without parsing message text -- and so that if
        # `_repost` ever regresses back to calling post_rereview (the bug
        # this fix closes), the call lands in the wrong list and the test
        # reading repost_retries sees nothing.
        self.repost_retries = []
        self.repost_retry_targets = []
        # What notify_ready was actually handed for unresolved_threads on
        # each call -- kept apart from `ready` (every existing assertion
        # already reads that as (slug, rounds) 2-tuples) so a test can pin
        # what the loop passed through without touching those.
        self.ready_threads = []
        # Calls to notify_still_running, as (slug, elapsed_minutes,
        # timeout_minutes, thread_ts) -- kept apart from the other lists for
        # the same reason: a test should be able to pin what fired without
        # parsing message text or touching unrelated assertions.
        self.still_running = []
        # The `lane_id` each notify_escalation call was actually handed --
        # kept apart from `escalations` (every existing assertion already
        # reads that as (slug, rounds, reason) 3-tuples) for the same reason
        # `ready_threads` is kept apart from `ready`: a test should be able to
        # pin what loop.py passed through without touching those.
        self.escalation_lane_ids = []
        self._repost_ts = repost_ts
        self._ready_ts = ready_ts
        self._escalation_ts = escalation_ts
        # None (the default) means "post succeeds"; set to True to make
        # notify_still_running behave like a failed Slack call, mirroring
        # the real SlackIO's contract of returning None on failure.
        self.still_running_fails = False
        # The same, for post_rereview -- and settable *after* construction on
        # purpose, unlike `repost_ts`: the re-post tests need the loop's first
        # post (the one `_lane_pushed` makes, which is what puts the row in
        # `rereview_posted` at all) to succeed and only a later one to fail.
        self.post_rereview_fails = False

    def post_rereview(self, verdict, thread_ts, round_number):
        self.reposts.append((verdict.slug, round_number))
        self.repost_targets.append(thread_ts)
        return None if self.post_rereview_fails else self._repost_ts

    def repost_rereview(self, verdict, thread_ts):
        self.repost_retries.append(verdict.slug)
        self.repost_retry_targets.append(thread_ts)
        return self._repost_ts

    def add_thinkies(self, channel, ts):
        self.thinkies_added.append(ts)

    def remove_thinkies(self, channel, ts):
        self.thinkies_removed.append(ts)

    def notify_ready(self, verdict, rounds, unresolved_threads):
        self.ready.append((verdict.slug, rounds))
        self.ready_threads.append(unresolved_threads)
        return self._ready_ts

    def notify_escalation(self, verdict, rounds, reason, lane_id=None):
        self.escalations.append((verdict.slug, rounds, reason))
        self.escalation_lane_ids.append(lane_id)
        return self._escalation_ts

    def notify_still_running(self, verdict, elapsed_minutes, timeout_minutes,
                             thread_ts):
        self.still_running.append(
            (verdict.slug, elapsed_minutes, timeout_minutes, thread_ts))
        return None if self.still_running_fails else "9.3"


class LanesFake:
    def __init__(self, status="in_progress", lane_id=True):
        self.created, self._status, self._lane_id = [], status, lane_id
        # What each create() call was handed for base_branch/head_branch --
        # kept apart from `created` (every existing assertion already reads
        # that as (slug, round) 2-tuples) so a test can pin what _open_lane
        # passed through without touching those.
        self.branches_seen = []
        # What each create() call was handed for bot_threads -- kept apart
        # from `branches_seen` for the same reason `branches_seen` is kept
        # apart from `created`: a test should be able to pin what _open_lane
        # passed through without touching the others.
        self.bot_threads_seen = []

    def create(self, verdict, round_number, *, base_branch=None,
               head_branch=None, bot_threads=None):
        self.created.append((verdict.slug, round_number))
        self.branches_seen.append((base_branch, head_branch))
        self.bot_threads_seen.append(bot_threads)
        if self._lane_id is None:
            return None
        return f"t_{len(self.created)}"

    def status(self, lane_id):
        return self._status


class SequencedLanesFake(LanesFake):
    """A lane whose status() answers a different value on each successive
    call, so a test can put a different answer in front of the due pass's own
    observation (`_observe_lane`'s read) than in front of the fresh re-check a
    lane_timeout settle takes moments later in the same tick
    (`_lane_still_running`'s read) -- simulating kanban's own account of the
    lane changing in the gap between the two.
    """

    def __init__(self, statuses, lane_id=True):
        super().__init__(status=statuses[0], lane_id=lane_id)
        self._statuses = list(statuses)
        self._calls = 0

    def status(self, lane_id):
        index = min(self._calls, len(self._statuses) - 1)
        self._calls += 1
        return self._statuses[index]


class RaisesOnSecondReadLanesFake(LanesFake):
    """status() answers normally the first time -- what the due pass's own
    observation reads, and what must stay non-terminal for a lane_timeout
    escalation to fire at all -- and raises every time after, simulating
    kanban going unreachable exactly between that observation and the fresh
    re-check `_lane_still_running` takes at settle time.
    """

    def __init__(self, first="running", lane_id=True):
        super().__init__(status=first, lane_id=lane_id)
        self._calls = 0

    def status(self, lane_id):
        self._calls += 1
        if self._calls > 1:
            raise RuntimeError("kanban unreachable")
        return self._status


class LosingStore:
    """A real store whose transition() always reports the write was lost.

    Stands in for the race the lease exists to prevent: another tick took the
    row over, so our compare-and-swap no longer matches.
    """

    def __init__(self, inner):
        self._inner = inner
        self.attempts = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def transition(self, loop_id, expected, target, *, now, **fields):
        self.attempts.append((expected, target, fields))
        return False


class UnclaimableStore:
    """A real store that refuses the per-loop lease, as if a rival tick held it."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def claim(self, loop_id, *, now):
        return False


class LosesTheRepostSelfEdge:
    """A real store whose `rereview_posted` -> `rereview_posted` swap is lost.

    The lapsed-lease race, aimed at the one edge where losing it used to hide a
    request that had already gone out: `_repost` sends a fresh re-review into a
    loop already waiting, and the swap that records it does not land.
    """

    def __init__(self, inner):
        self._inner = inner
        self.lost = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def transition(self, loop_id, expected, target, *, now, **fields):
        if not self.lost and expected is LoopState.REREVIEW_POSTED and \
                target is LoopState.REREVIEW_POSTED:
            self.lost += 1
            return False
        return self._inner.transition(loop_id, expected, target, now=now, **fields)


class LosesTheClosingWrite:
    """A real store whose *escalating* compare-and-swap is lost exactly once.

    The narrow race the escalation ordering cannot avoid: the notice reaches
    the human, and the write that closes the loop does not.
    """

    def __init__(self, inner):
        self._inner = inner
        self.lost = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def transition(self, loop_id, expected, target, *, now, **fields):
        if target is LoopState.ESCALATED and not self.lost:
            self.lost += 1
            return False
        return self._inner.transition(loop_id, expected, target, now=now, **fields)


class UnreleasableStore:
    """A real store whose release() explodes for whichever loops are nominated."""

    def __init__(self, inner):
        self._inner = inner
        self.explode = set()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def release(self, loop_id):
        if loop_id in self.explode:
            raise RuntimeError(f"the lease on {loop_id} could not be dropped")
        return self._inner.release(loop_id)


def verdict(outcome="ISSUES_FOUND", origin=None, head="a" * 40, rid="br_1",
            pr=42, thread="1.0", status="2.0"):
    """`thread` (root_thread_ts) and `status` (status_message_ts) default to
    different values on purpose: the reaction anchor is status_message_ts, the
    re-review target stays root_thread_ts, and a test that let the two default
    to the same literal could not tell them apart -- including from the one
    regression this module exists to prevent (the anchor reverting to
    root_thread_ts). See pr_remediation/loop.py's _clear_running_signal.
    """
    return Verdict(rid, "Ylopo", "example", pr, head, thread, outcome, origin,
                   (Issue(title="Null deref"),), status)


def build(tmp_path, source, github=None, slack=None, lanes=None, clock=None,
          store=None, cfg=None):
    from pr_remediation.loop import LoopDeps

    return LoopDeps(
        source=source, github=github or GitHubFake(), slack=slack or SlackFake(),
        lanes=lanes or LanesFake(), store=store or Store(tmp_path / "r.db"),
        cfg=cfg or RemediationConfig(mode="live"), now=clock or Clock(),
    )


def kinds(deps, loop_id):
    return [event["kind"] for event in deps.store.events(loop_id)]


def only_loop(deps, pr=42):
    return deps.store.active_for_pr("Ylopo", "example", pr)


def deferrals(deps, loop_id):
    return [event["detail"] for event in deps.store.events(loop_id)
            if event["kind"] == "verdict_deferred"]


def in_mode(deps, mode):
    """The same tick, the same store, a different operating mode."""
    from pr_remediation.loop import LoopDeps

    return LoopDeps(**{**deps.__dict__, "cfg": RemediationConfig(mode=mode)})


# --- the brief's behaviour -------------------------------------------------


def test_issues_found_opens_a_lane_and_reacts_with_thinkies(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    assert deps.slack.thinkies_added, "no immediate visual feedback was given"
    assert deps.github.claimed == [("Ylopo", "example", 42)]


def test_open_lane_reads_branches_once_and_threads_them_into_the_brief(tmp_path):
    """GitHubIO.branches() answers both names from the one _view() call
    (see github_io.py) -- _open_lane must call it exactly once per round and
    hand both straight to lanes.create(), which is what lets build_brief say
    where to push instead of leaving a real ambiguity open (see the module
    docstring)."""
    from pr_remediation.loop import tick

    github = GitHubFake(base_branch="main", head_branch="chad/fix-things")
    deps = build(tmp_path, SourceFake([verdict()]), github=github)
    assert tick(deps) == 1
    assert github.branch_calls == [("Ylopo", "example", 42)], \
        "exactly one branches() read for the round, not one per field"
    assert deps.lanes.branches_seen == [("main", "chad/fix-things")]


def test_open_lane_still_dispatches_when_branches_cannot_be_read(tmp_path):
    """None/None (an unreadable GitHub) must degrade the brief, not block
    the round -- the round this verdict earned is not forfeit over a brief
    that can only be slightly less specific."""
    from pr_remediation.loop import tick

    github = GitHubFake(base_branch=None, head_branch=None)
    deps = build(tmp_path, SourceFake([verdict()]), github=github)
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    assert deps.lanes.branches_seen == [(None, None)]


def test_open_lane_reads_bot_threads_on_the_same_pass_and_threads_them_into_the_brief(tmp_path):
    """The blind spot pr-review-hourly still tracked and this loop did not:
    unresolved Codex/CodeQL threads. `_open_lane` reads them the same tick
    it reads branches(), before dispatching, and hands them straight to
    lanes.create() -- which is what lets build_brief's inventory section say
    what a lane is walking into instead of the agent discovering it cold."""
    from pr_remediation.loop import tick

    found = [{"path": "src/webhook.py", "outdated": False}]
    github = GitHubFake(bot_threads=found)
    deps = build(tmp_path, SourceFake([verdict()]), github=github)
    assert tick(deps) == 1
    assert github.bot_thread_calls == [("Ylopo", "example", 42)]
    assert deps.lanes.bot_threads_seen == [found]


def test_open_lane_still_dispatches_when_bot_threads_cannot_be_read(tmp_path):
    """None (an unreadable GitHub, or a truncated read) must degrade the
    brief -- via build_brief's own uncertain sentence -- and must never
    block, escalate, or hold the lane open. The round still opens."""
    from pr_remediation.loop import tick

    github = GitHubFake(bot_threads=None)
    deps = build(tmp_path, SourceFake([verdict()]), github=github)
    assert tick(deps) == 1
    assert github.bot_thread_calls == [("Ylopo", "example", 42)], \
        "the read must actually be attempted, not skipped because it can fail"
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    assert deps.lanes.bot_threads_seen == [None]
    assert deps.slack.escalations == [], \
        "a failed bot-thread read must not become an escalation"


def test_ready_notifies_and_closes_without_a_lane(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]))
    tick(deps)
    assert deps.slack.ready == [("Ylopo/example#42", 0)]
    assert deps.lanes.created == []
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_ready_clears_the_thinkies_reaction(tmp_path):
    """thinkies means "a round is running"; a finished loop has none."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]))
    tick(deps)
    assert deps.slack.thinkies_removed == ["2.0"]


# --- the ready notice reports unresolved bot review threads ----------------
#
# The blind spot the first live run exposed: the loop declared a PR ready
# while three Codex threads sat open on the file it had just remediated.
# `_finish_ready` now asks GitHubIO for open Codex/CodeQL threads before
# telling Slack the loop is closing ready, and hands whatever it found
# straight through to `notify_ready`'s new third parameter.

def test_ready_asks_github_for_unresolved_bot_threads_on_the_right_pr(tmp_path):
    from pr_remediation.loop import tick

    github = GitHubFake(bot_threads=[
        {"path": "scripts/lib/tag-vendors.ts", "outdated": False}])
    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 github=github)
    tick(deps)
    assert github.bot_thread_calls == [("Ylopo", "example", 42)]


def test_ready_passes_unresolved_bot_threads_through_to_the_notice(tmp_path):
    from pr_remediation.loop import tick

    found = [{"path": "scripts/lib/tag-vendors.ts", "outdated": False}]
    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 github=GitHubFake(bot_threads=found))
    tick(deps)
    assert deps.slack.ready_threads == [found]


def test_ready_with_no_unresolved_threads_passes_through_the_clean_answer(tmp_path):
    """The ordinary case: GitHub was asked, nothing is outstanding. Not None
    -- that would claim the check never ran."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 github=GitHubFake(bot_threads=[]))
    tick(deps)
    assert deps.slack.ready_threads == [[]]


def test_a_failed_thread_check_still_closes_the_loop_ready(tmp_path):
    """The failure mode that matters most: a query that could not be
    answered must not hold the loop open and must not turn into an
    escalation either. It changes only what the notice says."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 github=GitHubFake(bot_threads=None))
    assert tick(deps) == 1
    assert deps.slack.ready_threads == [None]
    assert deps.store.get(_loop_id(deps)).state is LoopState.READY
    assert deps.slack.escalations == [], \
        "a failed thread check must not become an escalation"


def test_off_mode_never_calls_unresolved_bot_threads(tmp_path):
    """The kill switch stops the work, not only the writes: an off tick
    never reaches `_finish_ready` at all (verdict.outcome is never even
    consulted -- see test_the_off_switch_reads_no_github_at_all), so the new
    GraphQL read must never fire either."""
    from pr_remediation.loop import tick

    github = GitHubFake(bot_threads=[{"path": "a.ts", "outdated": False}])
    deps = in_mode(
        build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
              github=github),
        "off")
    assert tick(deps) == 0
    assert github.bot_thread_calls == []
    assert deps.slack.ready == []


def test_thinkies_is_anchored_on_boris_status_message_not_root_thread_ts(tmp_path):
    """The regression this module exists to prevent: the reaction must land on
    (and come off) Boris's status message, never the thread root -- even
    though `post_rereview` targets the thread root on purpose (see
    `test_rereview_still_posts_to_the_thread_not_the_status_message` below)."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(thread="1.0", status="2.0")]))
    tick(deps)
    assert deps.slack.thinkies_added == ["2.0"], \
        "the reaction must anchor on status_message_ts, not root_thread_ts"


def test_thinkies_is_anchored_on_the_loops_own_status_message_not_the_verdict(tmp_path):
    """A reaction can only be removed from the message it was added to: the
    loop's own persisted anchor, not whatever a later verdict happens to
    carry. Only `_open_lane`'s own compare-and-swap ever moves that anchor,
    and it moves the row -- so the closing verdict below, which opens no
    round, must be removed from where the round that ran put it.

    Closed by a merge rather than a READY verdict, because a merge is the one
    ending that still closes a loop whose lane is in flight: everything past
    the pull request's own state defers to the running lane (see
    `test_a_ready_verdict_never_hands_back_a_pr_a_lane_is_pushing_to`), while a
    branch nobody can push to any more is not a branch this loop is protecting.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(status="2.0")]))
    tick(deps)
    assert deps.slack.thinkies_added == ["2.0"]

    deps.source.verdicts = [verdict(rid="br_2", status="9.5")]
    deps.github.pr_state = "MERGED"
    tick(deps)
    assert deps.slack.thinkies_removed == ["2.0"], \
        "removed from the loop's own anchor, not the closing verdict's status message"


def test_rereview_still_posts_to_the_thread_not_the_status_message(tmp_path):
    """Only the reaction anchor moved. `post_rereview` must keep targeting
    root_thread_ts: boris_intake's gate (gate.py::_is_loop_repost) admits a
    re-review only as a reply in an existing thread, which is root_thread_ts,
    not Boris's status message -- unifying the two anchors would silently drop
    every re-review the moment they diverge."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(outcome="REVIEW_INCOMPLETE", origin="system",
                 thread="1.0", status="2.0")]))
    tick(deps)
    assert deps.slack.repost_retry_targets == ["1.0"], \
        "the system-incomplete repost must target root_thread_ts, not the anchor"


def test_the_due_pass_rereview_also_targets_the_thread(tmp_path):
    """The other post_rereview call site (a lane that finished and pushed),
    pinned the same way."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                thread="1.0", status="2.0")
    deps.github.head = "b" * 40
    assert tick(deps) == 1
    assert deps.slack.repost_targets == ["1.0"]


def test_a_verdict_with_no_status_message_reacts_not_at_all_but_still_opens_a_lane(
        tmp_path):
    """status_message_ts is nullable. Its absence must not fail the round --
    the reaction is a convenience signal, not a correctness mechanism -- and it
    must NOT fall back to root_thread_ts, which would reintroduce the
    watchdog collision this fix exists to end."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(thread="1.0", status=None)]))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)], "the round still ran"
    assert deps.github.claimed == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_added == [], "no anchor, so no reaction at all"
    loop = only_loop(deps)
    assert loop.state is LoopState.REMEDIATING
    assert "reaction_skipped_no_status_message" in kinds(deps, loop.loop_id), \
        "the omission must be explainable from the trail, not silent"


def test_a_loop_with_no_status_message_anchor_still_closes_out_normally(tmp_path):
    """The missing anchor is a missing convenience signal, nothing more: the
    round it never decorated still completes and the loop still reaches its
    terminal state."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(status=None, outcome="READY_FOR_HUMAN_REVIEW")]))
    assert tick(deps) == 1
    assert deps.slack.ready == [("Ylopo/example#42", 0)]
    assert deps.slack.thinkies_removed == [], "nothing to remove; no anchor was ever set"
    assert only_loop(deps) is None, "the loop still reaches its terminal state"


class ReactionRemovalSilentlyFails(SlackFake):
    """A Slack whose first reaction removal quietly does nothing.

    The real `SlackIO.remove_thinkies` swallows every exception and never
    inspects `reactions.remove`'s own `ok` field, so a removal that did not
    land leaves the reaction sitting on the message with no exception raised
    and no record written anywhere. This reproduces exactly that: the call is
    counted in `dropped` rather than `thinkies_removed`, so a test can tell
    "asked, and it did not land" apart from "never asked".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.dropped = []
        self._swallow = True

    def remove_thinkies(self, channel, ts):
        if self._swallow:
            self._swallow = False
            self.dropped.append(ts)
            return
        return super().remove_thinkies(channel, ts)


def rereviewed(tmp_path, slack=None, status="2.0"):
    """A loop whose round 1 lane finished, pushed, and earned its re-review.

    Where the next verdict finds it: `rereview_posted`, one round spent, and
    round 1's reaction already taken off `status`.
    """
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                slack=slack, status=status)
    deps.github.head = "b" * 40
    assert tick(deps) == 1
    assert deps.store.get(_loop_id(deps)).state is LoopState.REREVIEW_POSTED
    deps.now.advance()
    return deps


def test_a_new_round_moves_the_anchor_to_the_verdict_that_opened_it(tmp_path):
    """Every Boris verdict posts its own status message, so a loop that keeps
    the anchor it opened with reacts on round 1's message for the rest of its
    life -- several messages up the thread -- and the round actually running
    shows no signal at all. Live evidence: the loop for Ylopo/million-stars#199
    held 1786904768.085119 across three verdicts that each had their own."""
    from pr_remediation.loop import tick

    deps = rereviewed(tmp_path)
    loop_id = _loop_id(deps)
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40, status="7.0")]
    assert tick(deps) == 1
    assert deps.store.get(loop_id).status_message_ts == "7.0", \
        "the row must follow the verdict whose round is now running"
    assert deps.slack.thinkies_added == ["2.0", "7.0"], \
        "round 2's signal belongs on round 2's message"


def test_the_new_rounds_reaction_comes_off_the_message_it_went_on(tmp_path):
    """The add and the remove are one pair. Moving the anchor is only correct
    if the removal follows it, so a round opened against a later verdict is
    cleared from that verdict's message and not from the one before it.

    Closed by a merge for the reason the anchor test above gives: it is the one
    ending a verdict can still bring to a loop whose round is in flight."""
    from pr_remediation.loop import tick

    deps = rereviewed(tmp_path)
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40, status="7.0")]
    assert tick(deps) == 1
    deps.source.verdicts = [verdict(rid="br_3", head="b" * 40, status="9.5")]
    deps.github.pr_state = "MERGED"
    deps.now.advance()
    assert tick(deps) == 1
    # Round 1's clear, round 2's idempotent retry of it as the anchor moves
    # (see test_a_round_that_moves_the_anchor_retries_the_old_ones_removal),
    # and then the close -- which is the one that has to name 7.0.
    assert deps.slack.thinkies_removed == ["2.0", "2.0", "7.0"], \
        "the running round's own message is what gets cleared"
    assert only_loop(deps) is None


def test_a_later_verdict_with_no_status_message_keeps_the_anchor_it_has(tmp_path):
    """The anchor is nullable at both ends, and the two nulls do not mean the
    same thing. A verdict that carries none says nothing about where the
    previous round's reaction is; blanking the row on its account would strand
    that reaction for good, because `_clear_running_signal` only ever reads the
    row. So the anchor already on the row is kept, and the round is decorated
    there rather than not at all."""
    from pr_remediation.loop import tick

    deps = rereviewed(tmp_path)
    loop_id = _loop_id(deps)
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40, status=None)]
    assert tick(deps) == 1
    assert deps.store.get(loop_id).status_message_ts == "2.0", \
        "a verdict with no anchor must not blank the one the row already has"
    assert deps.slack.thinkies_added == ["2.0", "2.0"]
    assert "reaction_skipped_no_status_message" not in kinds(deps, loop_id), \
        "there is an anchor; nothing was skipped"


def test_the_anchor_moves_inside_the_rounds_own_compare_and_swap(tmp_path):
    """The swap is where "we own this round" becomes true. A lost one means
    another tick owns the row, so the anchor must not have moved -- which is
    only guaranteed if it rides that swap rather than a write of its own."""
    from pr_remediation.loop import LoopDeps, tick

    deps = rereviewed(tmp_path)
    loop_id = _loop_id(deps)
    losing = LosingStore(deps.store)
    stalled = LoopDeps(**{**deps.__dict__, "store": losing})
    stalled.source.verdicts = [verdict(rid="br_2", head="b" * 40, status="7.0")]
    assert tick(stalled) == 0
    assert losing.attempts[0][2]["status_message_ts"] == "7.0", \
        "the anchor rides the round's own compare-and-swap"
    assert deps.store.get(loop_id).status_message_ts == "2.0", \
        "a swap we lost is not our round: the anchor stays where it was"
    assert deps.slack.thinkies_added == ["2.0"], \
        "and no reaction claims a round this tick does not own"


def test_a_round_that_moves_the_anchor_retries_the_old_ones_removal(tmp_path):
    """Every removal in this module is best-effort: SlackIO swallows the
    failure and nothing records it. While the anchor never moved, a removal
    that did not land was retried by the loop's next one; once it moves, no
    later `_clear_running_signal` can ever target the old message again and a
    dropped removal is stuck there forever. So the round that moves the anchor
    clears the message it is leaving behind -- idempotent by construction, and
    on the ordinary path a no-op."""
    from pr_remediation.loop import tick

    slack = ReactionRemovalSilentlyFails()
    deps = rereviewed(tmp_path, slack=slack)
    assert slack.dropped == ["2.0"] and slack.thinkies_removed == [], \
        "round 1's clear was asked for and quietly did not land"

    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40, status="7.0")]
    assert tick(deps) == 1
    assert slack.thinkies_removed == ["2.0"], \
        "nothing else would ever have targeted 2.0 again"
    assert slack.thinkies_added == ["2.0", "7.0"]


def test_a_round_that_keeps_its_anchor_does_not_clear_it_first(tmp_path):
    """The retry above is scoped to an anchor that is actually being left
    behind. A round opening on the same message must not remove the reaction
    it is about to add, which would flicker the signal off and on for every
    verdict that carries no anchor of its own."""
    from pr_remediation.loop import tick

    deps = rereviewed(tmp_path)
    deps.slack.thinkies_removed.clear()
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40, status=None)]
    assert tick(deps) == 1
    assert deps.slack.thinkies_removed == []


def test_a_hold_label_stops_the_loop_before_any_write(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]), github=GitHubFake(hold=True))
    assert tick(deps) == 0
    assert deps.lanes.created == []
    assert deps.github.claimed == []
    assert "held" in kinds(deps, only_loop(deps).loop_id)


def test_a_pr_chad_no_longer_authors_is_never_written_to(tmp_path):
    """Re-verified at lane creation, not just at admission."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 github=GitHubFake(author="someone-else"))
    tick(deps)
    assert deps.lanes.created == []
    assert deps.slack.escalations[0][2] == "not_chad_authored"


def test_an_unreadable_author_waits_instead_of_escalating(tmp_path):
    """None is "GitHub could not be read", not "Chad handed the PR over"."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]), github=GitHubFake(author=None))
    assert tick(deps) == 0
    assert deps.slack.escalations == []
    assert deps.lanes.created == []
    loop = only_loop(deps)
    assert loop.state is LoopState.OBSERVED, "a read blip must stay retriable"


def test_system_incomplete_reposts_without_burning_a_round(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(outcome="REVIEW_INCOMPLETE", origin="system")]))
    tick(deps)
    assert deps.slack.repost_retries, "a transient incomplete should be retried"
    assert deps.slack.reposts == [], \
        "must call repost_rereview, not post_rereview -- see slack_io.py"
    assert deps.lanes.created == []
    loop = only_loop(deps)
    assert loop.round_count == 0
    assert loop.state is LoopState.REREVIEW_POSTED


def system_incomplete(deps, rid, head="a" * 40):
    """One completed Boris review that came back with no judgement.

    boris_intake gives a review lane REVIEW_WAIT_SECONDS (90 minutes) and emits
    a system-origin REVIEW_INCOMPLETE when it gives up, so this is what a cycle
    of the re-post ping-pong actually costs in wall-clock terms.
    """
    from pr_remediation.loop import tick

    deps.now.advance(90 * 60)
    deps.source.verdicts = [verdict(rid=rid, head=head,
                                    outcome="REVIEW_INCOMPLETE", origin="system")]
    result = tick(deps)
    deps.source.verdicts = []
    return result


def test_a_review_that_never_completes_stops_being_asked_again(tmp_path):
    """`_repost` spends no round, so `round_cap` cannot bound it, and it opens
    a fresh episode every time it sends -- correctly, a new request deserves a
    new wait -- so the episode's own budget and four-hour deadline both restart
    on every cycle and neither can ever fire. Nothing else looked at the pair:
    forty cycles over sixty simulated hours dispatched forty full Boris review
    lanes, spent no round, escalated nothing and told nobody.

    A review that keeps failing to complete is a real thing a human should see,
    and it points somewhere none of the other dead ends do -- at the review lane
    itself, which keeps being dispatched and keeps producing nothing.
    """
    from pr_remediation.loop import (REVIEW_NEVER_COMPLETES,
                                     SYSTEM_REPOST_ATTEMPTS)

    deps = build(tmp_path, SourceFake([]))
    for n in range(SYSTEM_REPOST_ATTEMPTS):
        assert system_incomplete(deps, f"sys_{n}") == 1
    assert len(deps.slack.repost_retries) == SYSTEM_REPOST_ATTEMPTS
    loop = only_loop(deps)
    assert loop.state is LoopState.REREVIEW_POSTED
    assert loop.round_count == 0, "none of this ever spent a round"

    loop_id = loop.loop_id
    assert system_incomplete(deps, "sys_last") == 1
    assert len(deps.slack.repost_retries) == SYSTEM_REPOST_ATTEMPTS, \
        "the review is not asked for a fourth time"
    assert deps.slack.escalations == \
        [("Ylopo/example#42", 0, REVIEW_NEVER_COMPLETES)]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_the_system_repost_budget_survives_a_round_that_really_ran(tmp_path):
    """Counted over the loop's whole trail, and never reset -- which is the
    whole of what makes it a bound at all.

    Every other budget replayed off the trail here resets on the event that
    proves the thing it was waiting for arrived: a landed head read, a new
    request. This one cannot, because each of these sends *is* such an event --
    the gate took the request, a review lane ran, it finished -- and the marker
    that would do the resetting is the marker being counted. Counting it inside
    the episode instead puts the count back to zero on every cycle and makes the
    cap unreachable, which is the state this fix found the loop in.
    """
    from pr_remediation.loop import REVIEW_NEVER_COMPLETES, tick

    deps = build(tmp_path, SourceFake([]))
    assert system_incomplete(deps, "sys_0") == 1
    loop_id = only_loop(deps).loop_id

    # A real round in between: a verdict with issues, a lane, a push, and the
    # re-review that earns the next verdict. It leaves `rereview_posted` and
    # comes back to it, which is what ends an episode.
    deps.lanes._status = "done"
    deps.source.verdicts = [verdict(rid="issues", head="a" * 40)]
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    deps.source.verdicts = []
    deps.github.head = "b" * 40
    deps.now.advance(60)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED

    assert system_incomplete(deps, "sys_1", head="b" * 40) == 1
    assert system_incomplete(deps, "sys_2", head="b" * 40) == 1
    assert len(deps.slack.repost_retries) == 3
    assert system_incomplete(deps, "sys_3", head="b" * 40) == 1
    assert len(deps.slack.repost_retries) == 3, \
        "the round in between is not a reason to ask three more times"
    assert deps.slack.escalations[-1][2] == REVIEW_NEVER_COMPLETES


def test_a_paused_tick_spends_none_of_the_system_repost_budget(tmp_path):
    """The marker is written by `_repost`, which only a live tick reaches, so
    a pause of any length leaves this budget exactly where it was -- the same
    property the head-read budget and the re-post budget have, and for the same
    reason: what is counted is what a live tick actually did."""
    from pr_remediation.loop import SYSTEM_REPOST_ATTEMPTS

    deps = build(tmp_path, SourceFake([]))
    assert system_incomplete(deps, "sys_0") == 1

    paused = in_mode(deps, "shadow")
    for n in range(SYSTEM_REPOST_ATTEMPTS + 2):
        assert system_incomplete(paused, f"paused_{n}") == 0
    assert len(deps.slack.repost_retries) == 1, "a pause sends nothing"

    for n in range(SYSTEM_REPOST_ATTEMPTS - 1):
        assert system_incomplete(deps, f"live_{n}") == 1
    assert len(deps.slack.repost_retries) == SYSTEM_REPOST_ATTEMPTS, \
        "the pause spent none of the budget the live ticks are still holding"
    assert deps.slack.escalations == []


def test_model_incomplete_escalates(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(outcome="REVIEW_INCOMPLETE", origin="model")]))
    tick(deps)
    assert deps.slack.escalations
    assert deps.slack.escalations[0][2] == "model_incomplete"


def test_shadow_mode_decides_but_never_writes(tmp_path):
    from pr_remediation.loop import LoopDeps, tick

    deps = build(tmp_path, SourceFake([verdict()]))
    deps = LoopDeps(**{**deps.__dict__, "cfg": RemediationConfig(mode="shadow")})
    tick(deps)
    assert deps.lanes.created == []
    assert deps.slack.reposts == []
    assert deps.github.claimed == []
    loop = only_loop(deps)
    assert "shadow_decision" in kinds(deps, loop.loop_id), \
        "shadow must still record what it would do"


def test_shadow_mode_never_posts_or_labels_on_any_route(tmp_path):
    from pr_remediation.loop import LoopDeps, tick

    outcomes = [
        verdict(rid="br_1", pr=1),
        verdict(rid="br_2", pr=2, outcome="READY_FOR_HUMAN_REVIEW"),
        verdict(rid="br_3", pr=3, outcome="REVIEW_INCOMPLETE", origin="system"),
        verdict(rid="br_4", pr=4, outcome="REVIEW_INCOMPLETE", origin="model"),
    ]
    deps = build(tmp_path, SourceFake(outcomes))
    deps = LoopDeps(**{**deps.__dict__, "cfg": RemediationConfig(mode="shadow")})
    # Zero, not four: a paused tick advances no loop at all. It decides, and
    # every decision is recorded -- which is what the four routes below check.
    assert tick(deps) == 0
    assert deps.lanes.created == []
    assert deps.github.claimed == deps.github.released == []
    assert deps.slack.reposts == deps.slack.ready == deps.slack.escalations == []
    assert deps.slack.thinkies_added == deps.slack.thinkies_removed == []

    decided = []
    for pr in (1, 2, 3, 4):
        loop = only_loop(deps, pr)
        assert loop.state is LoopState.OBSERVED, "a paused tick moved a loop"
        decided += [event["detail"]["would"]
                    for event in deps.store.events(loop.loop_id)
                    if event["kind"] == "shadow_decision"]
    assert decided == ["open_lane", "notify_ready", "repost_rereview", "escalate"]


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_run_never_settles_an_escalation_a_live_run_left_pending(tmp_path, mode):
    """The one branch a pure shadow run cannot reach on its own, which is why
    walking all four routes in shadow never caught it: only a live run leaves a
    pending escalation, and the pause procedure is exactly what arrives next.
    """
    from pr_remediation.loop import NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1")]), slack=slack)
    tick(deps)
    loop_id = _loop_id(deps)
    assert kinds(deps, loop_id).count("escalation_pending") == 1

    slack._escalation_ts = "9.2"  # Slack is back. The service is not.
    paused = in_mode(deps, mode)
    paused.source.verdicts = [verdict(rid="br_2")]
    paused.now.advance(NOTIFY_RETRY_SECONDS)
    assert tick(paused) == 0

    assert len(slack.escalations) == 1, "a paused run posted to a human channel"
    assert deps.github.released == [], "a paused run handed the label back"
    assert deps.slack.thinkies_removed == []
    loop = deps.store.get(loop_id)
    assert loop.state is not LoopState.ESCALATED, "a paused run drove the loop terminal"
    assert kinds(deps, loop_id).count("escalation_pending") == 1, \
        "the debt must survive untouched, and unspent"
    assert "shadow_decision" in kinds(deps, loop_id)
    assert deferrals(deps, loop_id)[-1]["reason"] == "escalation_owed"

    # ...and the debt is still there for whoever turns the service back on.
    live = in_mode(deps, "live")
    live.source.verdicts = []
    live.now.advance(NOTIFY_RETRY_SECONDS)
    assert tick(live) == 1
    assert len(slack.escalations) == 2
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


@pytest.mark.parametrize("mode", ["shadow", "off"])
@pytest.mark.parametrize("outcome,origin,target", [
    ("READY_FOR_HUMAN_REVIEW", None, LoopState.READY),
    ("REVIEW_INCOMPLETE", "model", LoopState.ESCALATED),
])
def test_a_paused_run_never_closes_a_loop_a_live_run_created(
        tmp_path, mode, outcome, origin, target):
    """Gating single branches on the mode missed the two that go *terminal*.

    A pure paused run cannot create the precondition, which is why walking
    every route in shadow never caught it: a live tick is what claims the PR,
    dispatches the lane and adds the reaction, and the pause procedure is
    exactly what arrives next. Closing such a loop is unrecoverable -- terminal
    means `active_for_pr` returns None, so no later live tick can pick it up,
    the ownership label other agents read stays on the PR forever, and nobody
    is ever told the PR is ready.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]))
    assert tick(deps) == 1
    loop_id = _loop_id(deps)
    assert deps.github.claimed == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_added == ["2.0"]
    rereview_posted(deps)  # the lane finished, the head moved, the re-review went

    paused = in_mode(deps, mode)
    paused.source.verdicts = [verdict(rid="br_2", outcome=outcome, origin=origin)]
    paused.now.advance()
    assert tick(paused) == 0

    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED, \
        "a paused run drove the loop terminal"
    assert only_loop(deps) is not None, "no later live tick could ever recover it"
    assert deps.slack.ready == deps.slack.escalations == []
    assert deps.github.released == [], "a paused run handed the label back"
    assert deps.slack.thinkies_removed == []
    assert "shadow_decision" in kinds(deps, loop_id), \
        "a paused run must still record the route it would have taken"

    # ...and everything is exactly as a later live tick expects to find it.
    live = in_mode(deps, "live")
    live.source.verdicts = [verdict(rid="br_3", outcome=outcome, origin=origin)]
    live.now.advance()
    assert tick(live) == 1
    assert deps.store.get(loop_id).state is target
    assert deps.github.released == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_removed == ["2.0"]


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_due_pass_settles_nothing_a_live_run_left_owing(tmp_path, mode):
    """The third mode gate, in the pass no verdict ever reaches.

    `_settle_due` called the acting code directly, behind an ordinary
    early-return on the mode -- the per-branch shape that had already failed
    twice. Deleting that one line let an `off` tick post to the human channel,
    release the ownership label, remove the reaction and drive a live-created
    loop to `escalated`, and the whole suite stayed green: every paused test
    drove a verdict, and this pass runs when there is no verdict at all.
    """
    from pr_remediation.loop import NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1")]), slack=slack)
    assert tick(deps) == 0, "a live tick that owes a human a notice"
    loop_id = _loop_id(deps)
    assert kinds(deps, loop_id).count("escalation_pending") == 1

    slack._escalation_ts = "9.2"  # Slack is back. The service is not.
    paused = in_mode(deps, mode)
    paused.source.verdicts = []  # nothing to route: the due pass is the tick
    paused.now.advance(NOTIFY_RETRY_SECONDS)
    assert tick(paused) == 0

    assert len(slack.escalations) == 1, "a paused due pass posted to a human"
    assert deps.github.released == [], "a paused due pass handed the label back"
    assert deps.slack.thinkies_removed == []
    loop = deps.store.get(loop_id)
    assert loop.state is not LoopState.ESCALATED, \
        "a paused due pass drove a live-created loop terminal"
    assert kinds(deps, loop_id).count("escalation_pending") == 1, \
        "the debt must survive untouched, and unspent"
    assert [event["detail"]["would"] for event in deps.store.events(loop_id)
            if event["kind"] == "shadow_decision"] == ["settle_escalation"], \
        "the due pass must still record the decision it would have taken"

    # A standing decision is recorded once however long the pause lasts: this
    # pass retakes it every tick, and every tick reads the whole trail back.
    for _ in range(3):
        paused.now.advance()
        assert tick(paused) == 0
    assert kinds(deps, loop_id).count("shadow_decision") == 1

    # ...and the debt is still there for whoever turns the service back on.
    live = in_mode(deps, "live")
    live.now.advance()
    assert tick(live) == 1
    assert len(slack.escalations) == 2
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)]


class ReadRecordingGitHub(GitHubFake):
    """A GitHub that remembers being asked. `off` must ask it nothing.

    Every accessor the loop calls is recorded, `state` included: each one is
    its own `gh pr view` subprocess (GitHubIO._view is not memoised, on
    purpose), so a read this fake forgot to record is a read no test can see
    arriving on a path that must not take it.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reads = []

    def head_sha(self, o, r, p):
        self.reads.append(("head_sha", p))
        return super().head_sha(o, r, p)

    def author(self, o, r, p):
        self.reads.append(("author", p))
        return super().author(o, r, p)

    def state(self, o, r, p):
        self.reads.append(("state", p))
        return super().state(o, r, p)

    def has_hold(self, o, r, p):
        self.reads.append(("has_hold", p))
        return super().has_hold(o, r, p)


def test_the_off_switch_reads_no_github_at_all(tmp_path):
    """`off` is the hard kill switch, not a quieter shadow: it stops the work,
    not only the writes. Two API reads per verdict from a service an operator
    has switched off is rate budget spent on decisions nobody will act on.

    `shadow` still reads, and must: its whole job is rehearsing the decision a
    live tick would take, and a decision taken without the hold label and the
    author is a different decision.
    """
    from pr_remediation.loop import tick

    github = ReadRecordingGitHub()
    deps = in_mode(build(tmp_path, SourceFake([verdict()]), github=github), "off")
    assert tick(deps) == 0
    assert github.reads == [], "the kill switch still called GitHub"
    assert deps.lanes.created == deps.github.claimed == deps.slack.reposts == []

    loop = only_loop(deps)
    assert loop.state is LoopState.OBSERVED
    assert [event["detail"]["would"] for event in deps.store.events(loop.loop_id)
            if event["kind"] == "shadow_decision"] == ["unobserved"], \
        "a tick that looked at nothing must not claim a decision it never took"

    shadow = in_mode(deps, "shadow")
    shadow.source.verdicts = [verdict(rid="br_2")]
    shadow.now.advance()
    assert tick(shadow) == 0
    assert github.reads == [("has_hold", 42), ("state", 42), ("author", 42)], \
        "shadow rehearses the live decision, so shadow reads"


def test_a_paused_tick_consumes_the_verdict_it_opened_the_row_for(tmp_path):
    """A verdict is marked seen before the mode is consulted, so the row a
    paused tick leaves behind is *not* one a later live tick takes over: that
    verdict can never be routed live. The module docstring used to say the
    opposite, which reads as though pausing costs nothing. It costs every
    verdict that arrives while paused -- which is why the rollout clears the
    database at the shadow-to-live cutover rather than inheriting these rows.
    """
    from pr_remediation.loop import tick

    deps = in_mode(build(tmp_path, SourceFake([verdict(rid="br_1")])), "shadow")
    assert tick(deps) == 0
    loop_id = _loop_id(deps)
    assert deps.store.get(loop_id).state is LoopState.OBSERVED

    live = in_mode(deps, "live")  # the operator turns the service on
    live.now.advance()
    assert tick(live) == 0, "the paused tick consumed the verdict for good"
    assert live.lanes.created == [], "no lane: there is no verdict left to route"
    assert deps.store.get(loop_id).state is LoopState.OBSERVED
    assert deps.store.mark_verdict_seen("br_1", deps.now()) is False


def test_the_newest_verdict_wins_and_a_stale_one_never_reopens_the_pr(tmp_path):
    """`new_verdicts` is newest-first, so within one sweep the *older* verdict
    is routed second. Once the newer one has closed the loop, `active_for_pr`
    reports no loop at all and a second generation opens: in a single tick the
    service announced the PR ready, handed the ownership label back, then
    re-claimed it and dispatched a lane -- from a stale verdict.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([
        verdict(rid="br_new", outcome="READY_FOR_HUMAN_REVIEW"),
        verdict(rid="br_old"),
    ]))
    assert tick(deps) == 1
    assert deps.slack.ready == [("Ylopo/example#42", 0)]
    assert deps.github.released == [("Ylopo", "example", 42)]
    assert deps.github.claimed == [], "the PR was re-claimed after being handed back"
    assert deps.lanes.created == []
    assert only_loop(deps) is None
    rows = deps.store._db.execute("SELECT COUNT(*) AS c FROM loops").fetchone()
    assert rows["c"] == 1, "a stale verdict opened a second generation"

    loop_id = _loop_id(deps)
    deferred = deferrals(deps, loop_id)
    assert [d["request_id"] for d in deferred] == ["br_old"], \
        "the verdict we skipped must be in the trail, not silently dropped"
    assert deferred[0]["reason"] == "superseded_verdict"
    assert deps.store.mark_verdict_seen("br_old", deps.now()) is False, \
        "a skipped verdict is consumed, not left to be replayed"


def test_the_same_verdict_is_never_consumed_twice(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]))
    tick(deps)
    tick(deps)
    assert len(deps.lanes.created) == 1


# --- round accounting ------------------------------------------------------


def rereview_posted(deps):
    """Force the loop past its in-flight lane, the way the lane pass does.

    A verdict is refused while a lane is still in flight (that lane, not a
    second one, drives the next step), so driving round accounting from the
    verdict path alone means standing in for the due pass's lane completion:
    the lane finished, the head moved, the re-review went out. The lane pass
    itself is exercised further down, from the due pass it really runs in.
    """
    deps.store._db.execute(
        "UPDATE loops SET state=? WHERE loop_id=?",
        (LoopState.REREVIEW_POSTED.value, _loop_id(deps)))
    deps.store._db.commit()


def test_the_round_cap_escalates_on_the_fourth_verdict(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([]))
    for n in range(1, 4):
        deps.source.verdicts = [verdict(rid=f"br_{n}")]
        tick(deps)
        rereview_posted(deps)
    assert len(deps.lanes.created) == 3
    assert only_loop(deps).round_count == 3

    deps.source.verdicts = [verdict(rid="br_4")]
    tick(deps)
    assert len(deps.lanes.created) == 3, "the cap must stop a fourth lane"
    assert deps.slack.escalations[0][2] == "round_cap"
    assert only_loop(deps) is None, "an escalated loop is finished"


def test_a_new_round_records_the_head_it_dispatched_against(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]))
    tick(deps)
    rereview_posted(deps)
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    tick(deps)
    assert only_loop(deps).head_sha == "b" * 40


# --- one lane per loop -----------------------------------------------------


def test_two_verdicts_for_one_pr_in_one_sweep_open_exactly_one_lane(tmp_path):
    """A re-review landing just behind its review is still one branch."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(rid="br_1"), verdict(rid="br_2")]))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    assert deps.github.claimed == [("Ylopo", "example", 42)]
    loop = only_loop(deps)
    assert loop.round_count == 1

    deferred = [event["detail"] for event in deps.store.events(loop.loop_id)
                if event["kind"] == "verdict_deferred"]
    assert [d["request_id"] for d in deferred] == ["br_2"], \
        "the verdict we refused must be in the trail, not silently dropped"
    # br_2 is the *older* of the two (new_verdicts is newest-first), so it is
    # now refused a step earlier than it used to be -- before it is routed at
    # all, rather than by the in-flight lane br_1 opened.
    assert deferred[0]["reason"] == "superseded_verdict"


def test_a_verdict_while_a_lane_is_in_flight_starts_no_second_round(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]))
    tick(deps)
    deps.now.advance()
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    assert tick(deps) == 0
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    loop = only_loop(deps)
    assert loop.round_count == 1
    assert loop.state is LoopState.REMEDIATING
    assert loop.head_sha == "a" * 40, "the head the lane runs against must stand"
    assert deferrals(deps, loop.loop_id)[-1]["reason"] == "lane_in_flight"


def test_a_deferred_verdict_leaves_the_loop_due_for_the_lane_sweep(tmp_path):
    """Refusing a verdict must not park the loop out of anyone's reach."""
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    deps = build(tmp_path, SourceFake([verdict(rid="br_1"), verdict(rid="br_2")]))
    tick(deps)
    due = deps.store.claim_due(now=deps.now() + LANE_POLL_SECONDS + 1, limit=5)
    assert [loop.loop_id for loop in due] == [only_loop(deps).loop_id]


def test_a_lane_whose_write_was_lost_is_not_dispatched_a_second_time(tmp_path):
    """The row carries no lane, so only the trail can refuse this -- and
    kanban's idempotency key cannot: it embeds the head sha, and the orphan's
    own push is what moves the head."""
    from pr_remediation.loop import tick

    store = LosingStore(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]), store=store)
    assert tick(deps) == 0
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    loop_id = _loop_id(deps)
    assert "orphaned_lane" in kinds(deps, loop_id)

    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    deps.now.advance()
    assert tick(deps) == 0
    assert deps.lanes.created == [("Ylopo/example#42", 1)], \
        "a second live lane on one branch"
    assert deps.github.claimed == [("Ylopo", "example", 42)]
    assert deferrals(deps, loop_id)[-1]["reason"] == "lane_orphaned"


def test_an_orphaned_lane_is_adopted_by_the_next_verdict(tmp_path):
    """Refusing the second dispatch must not strand the loop: the lane that is
    really running becomes the loop's own, and drives it from there."""
    from pr_remediation.loop import LoopDeps, tick

    store = LosingStore(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]), store=store)
    tick(deps)

    healed = LoopDeps(**{**deps.__dict__, "store": store._inner})
    healed.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    healed.now.advance()
    assert tick(healed) == 1
    assert healed.lanes.created == [("Ylopo/example#42", 1)]
    loop = only_loop(healed)
    assert loop.state is LoopState.REMEDIATING
    assert loop.lane_id == "t_1", "the lane already running is now the loop's"
    assert loop.round_count == 1, "the orphan's round, not a second one"
    assert loop.head_sha == "a" * 40, "the head that lane was dispatched against"


def test_an_orphaned_lane_is_adopted_with_no_verdict_at_all(tmp_path):
    """A lane that changes nothing triggers no re-review, so waiting for a
    verdict to settle the orphan is waiting for something that may never come."""
    from pr_remediation.loop import LANE_POLL_SECONDS, LoopDeps, tick

    store = LosingStore(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]), store=store)
    tick(deps)

    healed = LoopDeps(**{**deps.__dict__, "store": store._inner})
    healed.source.verdicts = []
    healed.now.advance()
    assert tick(healed) == 1
    assert healed.lanes.created == [("Ylopo/example#42", 1)]
    loop = only_loop(healed)
    assert loop.state is LoopState.REMEDIATING and loop.lane_id == "t_1"
    due = healed.store.claim_due(now=healed.now() + LANE_POLL_SECONDS + 1, limit=5)
    assert [claimed.loop_id for claimed in due] == [loop.loop_id], \
        "an adopted lane is due for the lane pass like any other"


def _orphan_note(deps, loop_id, lane_id="t_1", round_number=1):
    """The recording write a lapsed lease lost, landing *after* the rival's.

    This ordering is the only one an orphan can arise in -- the lease lapsed
    because a rival took the row over, so the rival's lane-carrying transition
    is already in the trail when our note lands -- and it is precisely the
    ordering `_orphaned_lane`'s superseding rule cannot see.
    """
    deps.store.record_note(loop_id, "orphaned_lane",
                           {"lane_id": lane_id, "round": round_number,
                            "head_sha": "a" * 40}, deps.now())


def _three_rounds(deps):
    """Drive the loop to its round cap, one real lane per round."""
    from pr_remediation.loop import tick

    for n in range(1, 4):
        deps.source.verdicts = [verdict(rid=f"br_{n}")]
        deps.now.advance()
        tick(deps)
        if n < 3:
            rereview_posted(deps)
    return only_loop(deps)


def test_a_stale_orphan_never_hijacks_a_loop_that_records_its_own_lane(tmp_path):
    """Nothing in trail order ever supersedes an orphan note that lands after
    the transition which overtook it, so `_orphaned_lane` goes on offering it
    rounds later. Adopting it then is not a correction but a hijack: the loop
    is pointed at a lane that finished rounds ago, its head is rewound to that
    lane's, and it is dropped back into `remediating` -- where the in-flight
    guard makes every later due pass skip it, so it is stuck for good.

    The round floor could not save any of that: it guards `round_count` alone.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([]))
    for n, head in ((1, "a" * 40), (2, "f" * 40)):
        deps.source.verdicts = [verdict(rid=f"br_{n}", head=head)]
        deps.now.advance()
        tick(deps)
        rereview_posted(deps)  # the lane finished and the re-review went out
    loop_id = _loop_id(deps)
    loop = deps.store.get(loop_id)
    assert (loop.round_count, loop.lane_id, loop.head_sha) == (2, "t_2", "f" * 40)

    _orphan_note(deps, loop_id)  # lane t_1, round 1, head aaaa: a round dead

    deps.source.verdicts = [verdict(rid="br_3", head="f" * 40)]
    deps.now.advance()
    assert tick(deps) == 1, "the loop must go on, not stall on a stale claim"
    loop = deps.store.get(loop_id)
    assert loop.lane_id == "t_3", "a lane that finished a round ago took the loop over"
    assert loop.head_sha == "f" * 40, "the head was rewound to a dead lane's"
    assert loop.round_count == 3
    assert deps.lanes.created[-1] == ("Ylopo/example#42", 3), \
        "the round this verdict paid for must actually have been dispatched"


def test_an_orphan_that_can_no_longer_apply_has_its_claim_deleted(tmp_path):
    """Refusing a stale claim is not enough while the claim survives: the row's
    lane is cleared when a lane pass finishes with it, and the note would be
    adopted then, however many rounds later. The due pass deletes it -- the
    pass with nothing else to spend, because a verdict is consumed whatever is
    done with it and the verdict path has a loop to drive.
    """
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]))
    tick(deps)
    loop_id = _loop_id(deps)
    rereview_posted(deps)  # that lane finished; the row still names it
    _orphan_note(deps, loop_id, lane_id="t_ghost")

    deps.source.verdicts = []
    deps.now.advance(LANE_POLL_SECONDS + 1)
    assert tick(deps) == 0, "deleting a stale claim is bookkeeping, not a step"
    assert "orphan_superseded" in kinds(deps, loop_id)

    # ...so nothing comes back when the row's own lane is cleared.
    lane_cleared(deps)
    deps.now.advance()
    assert tick(deps) == 0
    loop = deps.store.get(loop_id)
    assert loop.lane_id is None, "a dead lane came back out of the trail"
    assert loop.state is LoopState.REREVIEW_POSTED


def test_an_adopted_lane_brings_the_head_it_was_dispatched_against(tmp_path):
    """The row's head is stale by construction here: the write that would have
    advanced it is the very one the orphan lost. Adopting the row's own head
    instead points the loop at a head its lane is not pushing to -- and the
    whole suite missed that, because every other orphan test dispatches against
    the head the row already carried.
    """
    from pr_remediation.loop import LoopDeps, tick

    store = LosingStore(Store(tmp_path / "r.db"))
    healed = build(tmp_path, SourceFake([
        verdict(rid="br_1", outcome="REVIEW_INCOMPLETE", origin="system")]),
        store=store._inner)
    tick(healed)  # a re-review: the row is opened at head aaaa, with no lane
    loop_id = _loop_id(healed)
    assert healed.store.get(loop_id).head_sha == "a" * 40

    losing = LoopDeps(**{**healed.__dict__, "store": store})
    losing.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    losing.now.advance()
    assert tick(losing) == 0, "the lane exists; the write recording it is lost"
    assert healed.store.get(loop_id).head_sha == "a" * 40, "the write that was lost"

    healed.source.verdicts = []
    healed.now.advance()
    assert tick(healed) == 1
    loop = healed.store.get(loop_id)
    assert loop.lane_id == "t_1"
    assert loop.head_sha == "b" * 40, \
        "the loop must record the head its lane is really pushing to"


def test_the_due_pass_never_adopts_an_orphan_over_a_live_lane(tmp_path):
    """`_remediate` guards adoption with the in-flight rule and the due pass
    did not, so the pass rewrote a loop at round 3 with a stale round-1 orphan:
    two rounds of budget handed back to a loop that had exhausted its cap, and
    the lane the row actually recorded stranded."""
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    deps = build(tmp_path, SourceFake([]))
    loop = _three_rounds(deps)
    assert (loop.round_count, loop.lane_id) == (3, "t_3")
    assert loop.state is LoopState.REMEDIATING, "a lane is in flight"
    _orphan_note(deps, loop.loop_id)

    deps.source.verdicts = []
    deps.now.advance(LANE_POLL_SECONDS + 1)
    assert tick(deps) == 0, "nothing was owed and nothing should have moved"
    loop = only_loop(deps)
    assert loop.lane_id == "t_3", "the lane the row records was stranded"
    assert loop.round_count == 3, "a capped loop had two rounds handed back"


def test_a_stale_orphan_is_dropped_rather_than_preserved_while_the_lane_runs(
        tmp_path):
    """Regression: `_route_due` used to return early on `_lane_in_flight`,
    before it could reach the drop-orphan route below. That guard was
    redundant for its stated purpose -- in-flight implies `loop.lane_id is
    not None`, which is exactly the condition the code below it already
    routes to drop-orphan on, never adopt -- so it could never have stopped
    an adopt. Its only real effect was to preserve a stale claim through
    precisely the window in which orphans are born: a lapsed lease means a
    rival records *its* lane first, so the orphan note lands after the row
    is already `remediating` under the rival's lane.

    Left alone, the claim survives every due tick while that lane runs and
    gets adopted backwards the moment the lane finally clears -- rewinding a
    round-3 loop to a lane that finished two rounds ago. This is what would
    happen if the guard came back.
    """
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    deps = build(tmp_path, SourceFake([]))
    for n, head in ((1, "a" * 40), (2, "b" * 40), (3, "f" * 40)):
        deps.source.verdicts = [verdict(rid=f"br_{n}", head=head)]
        deps.now.advance()
        tick(deps)
        if n < 3:
            rereview_posted(deps)
    loop_id = _loop_id(deps)
    loop = deps.store.get(loop_id)
    assert (loop.round_count, loop.lane_id, loop.head_sha) == (3, "t_3", "f" * 40)
    assert loop.state is LoopState.REMEDIATING, "the row's own lane is in flight"

    _orphan_note(deps, loop_id)  # lane t_1, round 1, head aaaa: two rounds dead

    # Several due ticks pass while the row's own lane is still in flight.
    for _ in range(3):
        deps.now.advance(LANE_POLL_SECONDS + 1)
        assert tick(deps) == 0, "dropping a claim is bookkeeping, not a step"
        loop = deps.store.get(loop_id)
        assert (loop.round_count, loop.lane_id, loop.head_sha) == \
            (3, "t_3", "f" * 40), "the in-flight lane must not be rewound"
        assert loop.state is LoopState.REMEDIATING

    assert "orphan_superseded" in kinds(deps, loop_id), \
        "the stale claim must be dropped while the lane runs, not preserved"

    # The row's own lane completes and lane_id is cleared -- the window a
    # preserved claim would have been adopted into.
    lane_cleared(deps)
    deps.now.advance()
    assert tick(deps) == 0
    loop = deps.store.get(loop_id)
    assert loop.lane_id is None, "must not be adopted backwards to the dead lane"
    assert loop.head_sha == "f" * 40, "must not be rewound to the dead orphan's head"
    assert loop.round_count == 3, "must not be rewound to the dead orphan's round"
    assert loop.state is LoopState.REREVIEW_POSTED, \
        "must not be dropped back into remediating under the dead lane"


def lane_cleared(deps):
    """That lane finished and the row no longer points at it.

    The only shape a spent loop can still adopt into: a row that records a lane
    of its own is never rewritten from an orphan note, so a stale note can only
    reach a row whose lane has been cleared.
    """
    deps.store._db.execute(
        "UPDATE loops SET state=?, lane_id=NULL WHERE loop_id=?",
        (LoopState.REREVIEW_POSTED.value, _loop_id(deps)))
    deps.store._db.commit()


def test_adopting_an_orphan_never_rewinds_the_round_cap(tmp_path):
    """Adoption is a correction, not a rewind. It records the lane that is
    really running; it must not restore budget the loop has already spent.

    The row must carry no lane for adoption to apply at all now -- a row that
    records one is never rewritten from an orphan note, which is
    `test_a_stale_orphan_never_hijacks_a_loop_that_records_its_own_lane` -- so
    the third round's lane is cleared here the way its completion clears it.
    That is also the only way `round_count` can outrun an orphan's round, which
    is what makes the floor this test is about reachable.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([]))
    loop = _three_rounds(deps)
    lane_cleared(deps)  # that lane finished, so nothing is in flight now
    _orphan_note(deps, loop.loop_id)

    deps.source.verdicts = [verdict(rid="br_4")]
    deps.now.advance()
    assert tick(deps) == 1
    loop = only_loop(deps)
    assert loop.lane_id == "t_1", "the lane that is really running is adopted"
    assert loop.round_count == 3, "a capped loop had two rounds handed back"
    assert len(deps.lanes.created) == 3, "adoption dispatches nothing"

    # ...so the cap still bites, which is the whole point of not rewinding it.
    rereview_posted(deps)
    deps.source.verdicts = [verdict(rid="br_5")]
    deps.now.advance()
    assert tick(deps) == 1
    assert len(deps.lanes.created) == 3, "the cap must stop a fourth lane"
    assert deps.slack.escalations[-1][2] == "round_cap"


# --- an escalation is not finished until a human has it --------------------


def model_incomplete(rid="br_1"):
    return verdict(rid=rid, outcome="REVIEW_INCOMPLETE", origin="model")


def test_an_undelivered_escalation_does_not_close_the_loop(tmp_path):
    """Terminal means "a human was told". Nobody was told."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([model_incomplete()]),
                 slack=SlackFake(escalation_ts=None))
    assert tick(deps) == 0
    assert len(deps.slack.escalations) == 1, \
        "the retry pass must not re-post what this tick just tried"
    loop = only_loop(deps)
    assert loop is not None, "a silent escalation must not look like success"
    assert loop.state is not LoopState.ESCALATED
    assert "escalation_pending" in kinds(deps, loop.loop_id)
    assert deps.github.released == [], "the PR stays owned until the human has it"


def test_a_pending_escalation_is_retried_until_it_lands(tmp_path):
    """No further verdict will ever come for an escalated PR, so the retry
    cannot depend on one."""
    from pr_remediation.loop import NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    assert len(slack.escalations) == 1

    deps.source.verdicts = []
    slack._escalation_ts = "9.2"
    deps.now.advance(NOTIFY_RETRY_SECONDS)
    assert tick(deps) == 1
    assert len(slack.escalations) == 2
    assert deps.lanes.created == [], "a retry is a post, not a round"
    loop = deps.store.get(_loop_id(deps))
    assert loop.state is LoopState.ESCALATED
    assert loop.round_count == 0
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_a_delivered_escalation_is_never_notified_twice(tmp_path):
    from pr_remediation.loop import NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    deps.source.verdicts = []
    slack._escalation_ts = "9.2"
    deps.now.advance(NOTIFY_RETRY_SECONDS)
    tick(deps)
    deps.now.advance()
    assert tick(deps) == 0
    assert len(slack.escalations) == 2
    assert deps.store.get(_loop_id(deps)).state is LoopState.ESCALATED


def test_a_verdict_during_a_pending_escalation_retries_instead_of_remediating(tmp_path):
    """A stopped loop stays stopped: the verdict pays for the post, not a lane."""
    from pr_remediation.loop import tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    slack._escalation_ts = "9.2"
    deps.source.verdicts = [verdict(rid="br_2")]
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.lanes.created == []
    assert len(slack.escalations) == 2
    loop = deps.store.get(_loop_id(deps))
    assert loop.round_count == 0
    assert loop.state is LoopState.ESCALATED
    assert "verdict_deferred" in kinds(deps, loop.loop_id)


def test_a_permanently_unreachable_channel_stops_retrying(tmp_path):
    """Retrying forever would pin the PR's loop open and never notify anyone."""
    from pr_remediation.loop import NOTIFY_ATTEMPTS, NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    deps.source.verdicts = []
    # Driven well PAST the budget, and the count asserted as a literal. The
    # earlier version looped `range(NOTIFY_ATTEMPTS)` and then asserted the
    # count equalled `NOTIFY_ATTEMPTS`, which passes under every value of the
    # very constant it was supposed to be pinning -- 5 -> 3 and 5 -> 50 both
    # survived the whole suite.
    for _ in range(12):
        deps.now.advance(NOTIFY_RETRY_SECONDS)
        tick(deps)
    assert NOTIFY_ATTEMPTS == 5, \
        "the literal below is derived from this; re-derive it if the budget moves"
    assert len(slack.escalations) == 5, \
        "attempts must stop at the budget however many ticks follow"
    loop_id = _loop_id(deps)
    assert deps.store.get(loop_id).state is LoopState.ESCALATED, \
        "a loop held open forever blocks every later loop on this PR"
    assert "escalation_unreachable" in kinds(deps, loop_id)


def spend_until(deps, spent, cap_seconds):
    """Tick at the due pass's own cadence until `spent()` next rises.

    Returns the wall-clock the loop actually waited. The pass revisits a loop
    it leaves open every DUE_BACKOFF_SECONDS, so this is the interval a budget
    spent one-per-visit really runs at -- the thing neither of these two
    budgets had, and the thing no count can substitute for.
    """
    from pr_remediation.loop import DUE_BACKOFF_SECONDS, tick

    before, waited = spent(), 0.0
    while waited < cap_seconds:
        deps.now.advance(DUE_BACKOFF_SECONDS)
        waited += DUE_BACKOFF_SECONDS
        tick(deps)
        if spent() > before:
            return waited
    return None


def test_a_refused_escalation_post_waits_before_it_tries_again(tmp_path):
    """The budget is five attempts; the tolerance is five attempts times the
    wait between them, and there was no wait.

    Spent one per due-pass visit at DUE_BACKOFF_SECONDS, NOTIFY_ATTEMPTS gave
    up on Slack after sixty seconds -- closing the loop `escalation_unreachable`,
    releasing the ownership label and telling nobody, which `_settle_escalation`
    calls the worst outcome this system can produce. Nothing about this delays
    an escalation that is going to land: a reachable Slack takes the post on the
    first attempt, and only a refused one waits.
    """
    from pr_remediation.loop import NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    assert tick(deps) == 0
    assert len(slack.escalations) == 1, "the first attempt, refused"
    deps.source.verdicts = []

    waited = spend_until(deps, lambda: len(slack.escalations),
                         NOTIFY_RETRY_SECONDS * 2)
    assert waited is not None, "the retry must still happen"
    assert waited >= NOTIFY_RETRY_SECONDS, \
        "a refused post must not be retried at the pass's own cadence"


def test_the_notify_budget_rides_out_an_outage_of_more_than_an_hour(tmp_path):
    """The whole budget, measured end to end at the real cadence rather than
    multiplied out of two constants. An hour is the floor this asserts against
    because a minute -- what it bought before -- is not a tolerance for any
    outage anyone has ever had."""
    from pr_remediation.loop import DUE_BACKOFF_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    assert tick(deps) == 0
    deps.source.verdicts = []

    loop_id = _loop_id(deps)
    start, gave_up = deps.now(), None
    while deps.now() - start < 24 * 60 * 60:
        deps.now.advance(DUE_BACKOFF_SECONDS)
        tick(deps)
        if "escalation_unreachable" in kinds(deps, loop_id):
            gave_up = deps.now() - start
            break
    assert gave_up is not None, "the loop must still close rather than pin the PR"
    assert gave_up > 60 * 60, \
        f"gave up on Slack after only {gave_up / 60:.0f} minutes"


def test_one_sweep_spends_at_most_one_notification_attempt(tmp_path):
    """NOTIFY_ATTEMPTS is a budget for riding out an outage across ticks. Spent
    inside one tick it closes the loop unheard over a momentary blip.

    What bounds it is the newest-verdict rule: one verdict per pull request per
    sweep means one trip through the escalation path per sweep, so a second
    verdict for the same PR cannot spend a second attempt seconds after the
    first.
    """
    from pr_remediation.loop import tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1"),
                                       verdict(rid="br_2")]), slack=slack)
    assert tick(deps) == 0
    assert len(slack.escalations) == 1, "one sweep, one attempt at one outage"
    loop_id = _loop_id(deps)
    assert kinds(deps, loop_id).count("escalation_pending") == 1
    deferred = deferrals(deps, loop_id)
    assert [d["request_id"] for d in deferred] == ["br_2"]
    assert deferred[0]["reason"] == "superseded_verdict"


def test_a_delivered_escalation_whose_close_was_lost_is_closed_later_in_silence(tmp_path):
    """"Notified" is not "closed". A loop that never reaches a terminal state
    blocks every future generation for that PR, forever."""
    from pr_remediation.loop import tick

    store = LosesTheClosingWrite(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([model_incomplete()]), store=store)
    assert tick(deps) == 0
    loop_id = _loop_id(deps)
    assert len(deps.slack.escalations) == 1
    assert "escalation_delivered" in kinds(deps, loop_id)
    assert deps.store.get(loop_id).state is not LoopState.ESCALATED

    deps.source.verdicts = []
    deps.now.advance()
    assert tick(deps) == 1, "nothing else will ever come back for this loop"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert len(deps.slack.escalations) == 1, "the human already has it"
    # The retry re-attempts every write still owed, the stand-down included.
    # Both its acts are idempotent, and gating them on `notified` (as this once
    # did) is what leaves a label stranded when a settle dies between the
    # delivery note and the stand-down -- see the test below.
    assert deps.github.released == [("Ylopo", "example", 42)] * 2


class ReactionRemovalDies(SlackFake):
    """A Slack whose reaction removal explodes exactly once.

    Lands the settle in the one window that strands the PR: the delivery note
    is written, so no later tick will ever post again, and the stand-down that
    should follow it never runs.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.explode = True

    def remove_thinkies(self, channel, ts):
        if self.explode:
            self.explode = False
            raise RuntimeError("slack ate the reaction removal")
        return super().remove_thinkies(channel, ts)


def test_a_stand_down_that_died_after_delivery_is_retried(tmp_path):
    """Gating the whole notify-and-stand-down half on `notified` means a settle
    that throws between the delivery note and the stand-down leaves the
    ownership label and the reaction applied, with no later tick that would
    ever clear them. Only the notification may be gated."""
    from pr_remediation.loop import tick

    slack = ReactionRemovalDies()
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    assert tick(deps) == 0
    loop_id = _loop_id(deps)
    assert "escalation_delivered" in kinds(deps, loop_id), "the human has it"
    assert deps.github.released == [], "the label is stranded on the PR"

    deps.source.verdicts = []
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.github.released == [("Ylopo", "example", 42)], \
        "no later tick would ever hand the PR back"
    assert deps.slack.thinkies_removed == ["2.0"]
    assert len(slack.escalations) == 1, "the human already has the notice"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_a_delivered_escalation_never_lets_a_later_verdict_restart_remediation(tmp_path):
    """The ownership label went back to Chad when the notice landed; a lane
    started after that is remediating a PR this service no longer holds."""
    from pr_remediation.loop import tick

    store = LosesTheClosingWrite(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1")]), store=store)
    tick(deps)
    loop_id = _loop_id(deps)

    deps.source.verdicts = [verdict(rid="br_2")]
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.lanes.created == []
    assert deps.github.claimed == []
    assert len(deps.slack.escalations) == 1
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deferrals(deps, loop_id)[-1]["reason"] == "escalation_owed"


# --- the claim discipline --------------------------------------------------


def test_a_rival_lease_blocks_every_side_effect(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 store=UnclaimableStore(Store(tmp_path / "r.db")))
    assert tick(deps) == 0
    assert deps.github.claimed == []
    assert deps.lanes.created == []
    assert deps.slack.thinkies_added == []


def test_a_lost_transition_leaves_the_lane_recorded_not_celebrated(tmp_path):
    """The lane already exists; the round did not land. Say so, loudly."""
    from pr_remediation.loop import tick

    store = LosingStore(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict()]), store=store)
    assert tick(deps) == 0
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    assert deps.slack.thinkies_added == [], \
        "a loop we no longer drive must not be decorated as running"
    loop = only_loop(deps)
    assert loop.round_count == 0
    assert "orphaned_lane" in kinds(deps, loop.loop_id)
    assert "transition_lost" in kinds(deps, loop.loop_id)


def test_a_lease_we_cannot_drop_does_not_discard_the_sweep(tmp_path):
    """release() runs in a finally at the end of the tick. Unguarded, one
    failure there throws past tick() and takes the verdict pass's result with
    it -- over a lease that expires on its own anyway."""
    from pr_remediation.loop import NOTIFY_RETRY_SECONDS, tick

    store = UnreleasableStore(Store(tmp_path / "r.db"))
    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1")]),
                 slack=slack, store=store)
    tick(deps)
    stuck = only_loop(deps).loop_id
    store.explode.add(stuck)

    slack._escalation_ts = "9.2"
    deps.source.verdicts = [verdict(rid="br_2", pr=43,
                                    outcome="READY_FOR_HUMAN_REVIEW")]
    deps.now.advance(NOTIFY_RETRY_SECONDS)
    assert tick(deps) == 2, "the escalation settled and the ready PR closed"
    assert deps.store.get(stuck).state is LoopState.ESCALATED


def test_a_lost_transition_is_recorded_on_the_ready_route_too(tmp_path):
    from pr_remediation.loop import tick

    store = LosingStore(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 store=store)
    assert tick(deps) == 0
    assert "transition_lost" in kinds(deps, only_loop(deps).loop_id)


# --- downstream refusals ---------------------------------------------------


def test_a_failed_github_claim_escalates_instead_of_opening_a_lane(tmp_path):
    """No ownership label means other agents never see the PR is taken."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 github=GitHubFake(claim_ok=False))
    tick(deps)
    assert deps.lanes.created == []
    assert deps.slack.escalations[0][2] == "github_unwritable"
    assert only_loop(deps) is None


def test_a_refused_lane_escalates_without_consuming_a_round(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]), lanes=LanesFake(lane_id=None))
    tick(deps)
    assert deps.slack.escalations[0][2] == "lane_failed"
    assert deps.slack.thinkies_added == []
    loop = deps.store.get(_loop_id(deps))
    assert loop.round_count == 0, "no lane ran, so no round was spent"
    assert loop.state is LoopState.ESCALATED


def test_a_refused_lane_hands_the_pr_back(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]), lanes=LanesFake(lane_id=None))
    tick(deps)
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_a_failed_rereview_post_keeps_the_loop_retriable(tmp_path):
    """A Slack blip is transient: do not burn the loop over it."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(outcome="REVIEW_INCOMPLETE", origin="system")]),
        slack=SlackFake(repost_ts=None))
    assert tick(deps) == 0
    assert deps.slack.escalations == []
    loop = only_loop(deps)
    assert loop.state is LoopState.OBSERVED
    assert "rereview_post_failed" in kinds(deps, loop.loop_id)


def test_a_rereview_with_no_thread_escalates(tmp_path):
    """Without a thread the gate silently drops the post -- the loop would stall."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(outcome="REVIEW_INCOMPLETE", origin="system", thread="")]))
    tick(deps)
    assert deps.slack.repost_retries == []
    assert deps.slack.escalations[0][2] == "rereview_unpostable"


def test_a_failed_notification_still_closes_the_loop_but_says_so(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 slack=SlackFake(ready_ts=None))
    assert tick(deps) == 1
    loop_id = _loop_id(deps)
    assert "notify_failed" in kinds(deps, loop_id)


def test_a_failed_label_removal_is_recorded_not_fatal(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]),
                 github=GitHubFake(release_ok=False))
    assert tick(deps) == 1
    assert "release_failed" in kinds(deps, _loop_id(deps))


def test_an_unrecognised_outcome_is_noted_and_ignored(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="WHO_KNOWS")]))
    assert tick(deps) == 0
    assert deps.lanes.created == []
    assert deps.slack.escalations == []
    assert "unknown_outcome" in kinds(deps, only_loop(deps).loop_id)


# --- error isolation -------------------------------------------------------


def test_one_exploding_verdict_does_not_abort_the_sweep(tmp_path):
    from pr_remediation.loop import tick

    deps = build(
        tmp_path,
        SourceFake([verdict(rid="br_1", pr=41), verdict(rid="br_2", pr=42),
                    verdict(rid="br_3", pr=43)]),
        github=GitHubFake(hold_raises=(42,)),
    )
    assert tick(deps) == 2
    assert deps.lanes.created == [("Ylopo/example#41", 1), ("Ylopo/example#43", 1)]


def test_a_corrupt_row_is_contained_to_its_own_pr(tmp_path):
    from pr_remediation.loop import tick

    store = Store(tmp_path / "r.db")
    deps = build(tmp_path, SourceFake([verdict(rid="br_0", pr=42)]), store=store)
    tick(deps)
    loop_id = _loop_id(deps)
    store._db.execute("UPDATE loops SET state='martian' WHERE loop_id=?", (loop_id,))
    store._db.commit()

    deps.source.verdicts = [verdict(rid="br_1", pr=42), verdict(rid="br_2", pr=43)]
    assert tick(deps) == 1
    assert ("Ylopo/example#43", 1) in deps.lanes.created
    assert "corrupt_row" in kinds(deps, loop_id)


# --- every edge the driver performs is one the store allows ---------------


@pytest.mark.parametrize("outcome,origin,target", [
    ("ISSUES_FOUND", None, LoopState.REMEDIATING),
    ("READY_FOR_HUMAN_REVIEW", None, LoopState.READY),
    ("REVIEW_INCOMPLETE", "system", LoopState.REREVIEW_POSTED),
    ("REVIEW_INCOMPLETE", "model", LoopState.ESCALATED),
])
@pytest.mark.parametrize("prior", [
    LoopState.OBSERVED, LoopState.REMEDIATING,
    LoopState.AWAITING_HEAD_MOVE, LoopState.REREVIEW_POSTED,
])
def test_every_edge_the_driver_takes_is_legal(tmp_path, prior, outcome, origin, target):
    """The store raises on an illegal edge, so an unsupported one fails here."""
    from pr_remediation.loop import tick

    store = Store(tmp_path / "r.db")
    deps = build(tmp_path, SourceFake([verdict(rid="br_0")]), store=store)
    tick(deps)
    loop_id = _loop_id(deps)
    store._db.execute("UPDATE loops SET state=?, round_count=0 WHERE loop_id=?",
                      (prior.value, loop_id))
    store._db.commit()

    deps.source.verdicts = [verdict(rid="br_1", outcome=outcome, origin=origin)]
    if prior is LoopState.REMEDIATING:
        # The whole row of cells where the driver deliberately takes no edge at
        # all: the lane the first tick opened is still in flight, and while one
        # is, no verdict moves this loop whatever its outcome. Four cells, not
        # the one an earlier reading of this matrix saw -- `ISSUES_FOUND` is
        # merely the outcome that would have dispatched the rival lane itself,
        # while the system-origin incomplete produced the same pair one verdict
        # later by swapping the row out of `remediating` first, and the two
        # closing outcomes stand the pull request down over a worker still
        # pushing to it. See loop.py's `_route`.
        assert tick(deps) == 0
        assert store.get(loop_id).state is LoopState.REMEDIATING
        return
    assert tick(deps) == 1
    assert store.get(loop_id).state is target


@pytest.mark.parametrize("outcome,origin,target", [
    ("ISSUES_FOUND", None, LoopState.REMEDIATING),
    ("READY_FOR_HUMAN_REVIEW", None, LoopState.READY),
    ("REVIEW_INCOMPLETE", "system", LoopState.REREVIEW_POSTED),
    ("REVIEW_INCOMPLETE", "model", LoopState.ESCALATED),
])
def test_a_remediating_loop_with_no_lane_still_takes_its_edges(
        tmp_path, outcome, origin, target):
    """The row the matrix above skips, and the only shape that still takes an
    edge out of `remediating` on a verdict: a row in that state with no lane to
    poll (a hand-edit, or a round an older build's shadow mode wrote before a
    paused tick stopped writing state) has nothing in flight, so a verdict
    drives it exactly as it would from anywhere else -- including the
    `remediating -> remediating` edge no other path reaches."""
    from pr_remediation.loop import tick

    store = Store(tmp_path / "r.db")
    deps = build(tmp_path, SourceFake([verdict(rid="br_0")]), store=store)
    tick(deps)
    loop_id = _loop_id(deps)
    store._db.execute(
        "UPDATE loops SET state=?, lane_id=NULL, round_count=0 WHERE loop_id=?",
        (LoopState.REMEDIATING.value, loop_id))
    store._db.commit()

    deps.source.verdicts = [verdict(rid="br_1", outcome=outcome, origin=origin)]
    deps.now.advance()
    assert tick(deps) == 1
    assert store.get(loop_id).state is target


def test_a_system_incomplete_mid_round_opens_no_second_lane(tmp_path):
    """C1, end to end: two engineer lanes on one branch.

    A review lane boris_intake gives up on at ninety minutes emits a
    system-origin `REVIEW_INCOMPLETE`, and a remediation round runs for twenty
    to ninety -- so this verdict arrives *inside* a round as a matter of
    routine, on the route that produces a fifth of live verdicts. It used to
    take `ROUTE_REPOST_REREVIEW` and swap `remediating -> rereview_posted` with
    the lane still running: `_lane_in_flight` reads the row, so it then answered
    False, and the next `ISSUES_FOUND` dispatched a second code-editing worker
    against the same branch. The first lane was overwritten on the row and
    polled by nobody after that, so `LANE_WAIT_SECONDS` could never fire for it
    either.
    """
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path)
    loop_id = only_loop(deps).loop_id

    deps.source.verdicts = [verdict(rid="br_2", outcome="REVIEW_INCOMPLETE",
                                    origin="system")]
    assert tick(deps) == 0
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.REMEDIATING, \
        "the row must not leave `remediating` while its lane runs"
    assert loop.lane_id == "t_1"
    assert deps.slack.repost_retries == [], "and nothing is re-posted"
    assert deferrals(deps, loop_id)[-1]["reason"] == "lane_in_flight"

    deps.now.advance()
    deps.source.verdicts = [verdict(rid="br_3", head="b" * 40)]
    assert tick(deps) == 0
    assert deps.lanes.created == [("Ylopo/example#42", 1)], \
        "a second code-editing worker on one branch"
    assert deps.store.get(loop_id).lane_id == "t_1"


def test_a_ready_verdict_never_hands_back_a_pr_a_lane_is_pushing_to(tmp_path):
    """The same collision `_settle_escalation` already refuses to cause, on the
    path that had never been asked the question.

    Releasing `hermes-owner-andrej` is how every other agent -- and every human
    reading the label -- learns the branch is free, and a kanban lane cannot be
    cancelled from here. A READY verdict arriving mid-round was computed against
    a head this lane is in the middle of replacing, so closing on it both hands
    the branch back under a live worker and calls a pull request finished that
    is still receiving unreviewed commits.
    """
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.github.released.clear()

    deps.source.verdicts = [verdict(rid="br_2", outcome="READY_FOR_HUMAN_REVIEW")]
    assert tick(deps) == 0
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    assert deps.slack.ready == [], "nobody is told a live branch is ready"
    assert deps.github.released == [], "and the ownership label stays on"
    assert "lane_abandoned" not in kinds(deps, loop_id), \
        "nothing was abandoned, because nothing was closed"


def _loop_id(deps):
    row = deps.store._db.execute(
        "SELECT loop_id FROM loops ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["loop_id"]


# --- lane completion: the head must actually have moved --------------------


class CountingStore:
    """A real store that counts how many due batches a tick leases."""

    def __init__(self, inner):
        self._inner = inner
        self.due_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def claim_due(self, *, now, limit):
        self.due_calls += 1
        return self._inner.claim_due(now=now, limit=limit)


class LeaseLapses:
    """A real store that reports our lease gone for whichever loops are named.

    LEASE_SECONDS is short relative to a due batch that makes network calls per
    loop, so a long batch loses ownership partway through -- which the driver
    must notice at the loop it happens to, and not by watching a later
    compare-and-swap fail after the side effects already fired.
    """

    def __init__(self, inner):
        self._inner = inner
        self.lapsed = set()
        self.renewed = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def renew_claim(self, loop_id, *, now):
        self.renewed.append(loop_id)
        if loop_id in self.lapsed:
            return False
        return self._inner.renew_claim(loop_id, now=now)


class ReadRecordingLanes(LanesFake):
    """A kanban that remembers being polled. `off` must poll it nothing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.polled = []

    def status(self, lane_id):
        self.polled.append(lane_id)
        return super().status(lane_id)


def drive_to_remediating(tmp_path, lanes=None, github=None, slack=None,
                         store=None, thread="1.0", status="2.0"):
    """A live tick that opened a lane, with its verdict consumed.

    What the next sweep finds: a `remediating` row with a lane to poll, a
    thinkies reaction on Boris's status message, and no verdict left to
    route -- so the due pass, not the verdict pass, is what drives everything
    below.
    """
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    deps = build(tmp_path, SourceFake([verdict(thread=thread, status=status)]),
                 lanes=lanes, github=github, slack=slack, store=store)
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    deps.source.verdicts = []
    deps.now.advance(LANE_POLL_SECONDS + 1)
    return deps


def test_a_finished_lane_that_moved_the_head_asks_for_a_rereview(tmp_path):
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    deps.github.head = "b" * 40  # Andrej pushed
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "the round that just finished, not the one about to start"
    assert deps.slack.thinkies_removed == ["2.0"], \
        "the running signal must be cleared"
    loop = only_loop(deps)
    assert loop.state is LoopState.REREVIEW_POSTED
    assert loop.head_sha == "b" * 40, "the head the next verdict will be about"
    assert loop.round_count == 1, "asking for a re-review spends no round"
    assert deps.slack.escalations == []

    # ...and the pass is finished with this loop: a terminal lane still named
    # by the row must not be read back as a second completion, which would
    # re-post the re-review and then call the unmoved head a no-op.
    deps.now.advance()
    assert tick(deps) == 0
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    assert deps.slack.escalations == []


def test_a_still_running_lane_is_left_alone_and_silent(tmp_path):
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path)  # LanesFake defaults to in_progress
    loop_id = _loop_id(deps)
    before = kinds(deps, loop_id)
    deps.github.head = "b" * 40  # a push mid-round settles nothing
    assert tick(deps) == 0
    assert deps.slack.reposts == [] and deps.slack.escalations == []
    assert deps.slack.thinkies_removed == []
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.REMEDIATING
    assert loop.head_sha == "a" * 40
    assert kinds(deps, loop_id) == before, \
        "a healthy loop must not grow its trail on every tick"


def test_a_lane_that_pushed_nothing_gets_one_grace_window_then_escalates(tmp_path):
    """kanban marks a lane terminal when its worker exits, which can be moments
    before GitHub serves the pushed head. Calling that a no-op on the first
    look escalates a race; never calling it one re-reviews an unchanged PR
    forever."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    assert tick(deps) == 1
    loop = only_loop(deps)
    assert loop.state is LoopState.AWAITING_HEAD_MOVE
    assert deps.slack.escalations == [], "a push can land just behind its lane"
    assert deps.slack.reposts == []
    assert deps.slack.thinkies_removed == ["2.0"], "no round is running now"

    deps.now.advance(61.0)  # literal: one second past the 60s window
    assert tick(deps) == 1
    assert deps.slack.escalations[0][2] == "no_op_lane"
    assert deps.slack.reposts == [], "an unchanged PR must never be re-reviewed"
    assert only_loop(deps) is None, "an escalated loop is finished"
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_the_grace_window_catches_a_push_that_landed_late(tmp_path):
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    assert tick(deps) == 1
    assert only_loop(deps).state is LoopState.AWAITING_HEAD_MOVE

    deps.github.head = "b" * 40  # GitHub was simply behind the lane
    deps.now.advance(61.0)  # literal: one second past the 60s window
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    assert deps.slack.escalations == []
    loop = only_loop(deps)
    assert loop.state is LoopState.REREVIEW_POSTED
    assert loop.head_sha == "b" * 40


def test_a_tick_inside_the_grace_window_neither_escalates_nor_re_reviews(tmp_path):
    """Every test above advances the clock *past* HEAD_MOVE_GRACE_SECONDS
    before ticking again, so none of them ever ticks *inside* the window --
    which is exactly the race the window exists to survive: kanban says the
    lane is done, GitHub has not yet served the push, and only a beat has
    passed. The due pass must not even look yet: `next_attempt_at` is what
    keeps `claim_due` from selecting this loop at all until the window has
    actually elapsed."""
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = _loop_id(deps)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.AWAITING_HEAD_MOVE
    before = kinds(deps, loop_id)

    # Pinned against a LITERAL, and it is the pin every other grace-window
    # literal is derived from. The earlier `> 1.0` guard paired with a 1.0
    # advance asserted nothing about the window's size: 60 -> 600 survived the
    # whole suite, so a ten-fold longer race went untested in both directions.
    assert HEAD_MOVE_GRACE_SECONDS == 60.0, \
        "re-derive the literal edges in the grace-window tests if this moves"
    deps.now.advance(59.0)  # one second SHORT of the window's edge
    assert tick(deps) == 0
    assert deps.slack.escalations == []
    assert deps.slack.reposts == []
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.AWAITING_HEAD_MOVE
    assert kinds(deps, loop_id) == before, \
        "the window is still open; a tick inside it must not touch the loop at all"


def test_a_held_pr_stops_the_lane_pass_before_any_write(tmp_path):
    """The lane pass writes -- a reaction, a post, a label -- so it reads Chad's
    kill switch first, and `has_hold` fails closed on an unreadable GitHub."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    deps.github.hold = True
    deps.github.head = "b" * 40
    loop_id = _loop_id(deps)
    for _ in range(3):
        deps.now.advance()
        assert tick(deps) == 0
    assert deps.slack.reposts == [] and deps.slack.escalations == []
    assert deps.slack.thinkies_removed == []
    assert deps.github.released == []
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    assert kinds(deps, loop_id).count("held") == 1, \
        "a standing observation is recorded once, not once a tick"


def test_an_unreadable_head_waits_instead_of_calling_the_lane_a_no_op(tmp_path):
    """None is "GitHub could not be read", not "Andrej pushed nothing". The
    wait is bounded (HEAD_READ_ATTEMPTS), but three ticks is nowhere near it,
    so each is still just a wait -- see the budget tests below for the bound
    itself."""
    from pr_remediation.loop import HEAD_READ_RETRY_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(3):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        assert tick(deps) == 0
    assert deps.slack.escalations == [], "a read blip must not burn the loop"
    assert deps.slack.reposts == []
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    # Each attempt is its own note now, not one deduped sentence: the trail is
    # the only durable place the count can live, and the append is bounded by
    # the budget itself rather than growing without end.
    assert [e["detail"]["attempt"] for e in deps.store.events(loop_id)
            if e["kind"] == "head_unreadable"] == [1, 2, 3]

    deps.github.head = "b" * 40  # GitHub came back
    deps.now.advance(HEAD_READ_RETRY_SECONDS)
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]


def test_the_head_read_budget_exhausting_escalates_with_a_reason_distinct_from_no_op_lane(
        tmp_path):
    """A permanently unreadable head is not a no-op lane. `no_op_lane` means
    the read succeeded and the lane genuinely pushed nothing, which asks a
    human to accept the round as spent; this reason means the read itself
    never landed, which asks a human to go look at GitHub instead."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        tick(deps)
    assert deps.slack.escalations == \
        [("Ylopo/example#42", 1, "head_read_exhausted")]
    assert only_loop(deps) is None, "an escalated loop is finished"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_removed == ["2.0"]
    assert kinds(deps, loop_id).count("head_unreadable") == HEAD_READ_ATTEMPTS - 1, \
        "the exhausting attempt escalates instead of recording one more wait"


def test_a_failed_head_read_waits_before_it_tries_again(tmp_path):
    """The same missing spacing on the other budget of the same shape.

    Five attempts spent one per due-pass visit gave up on GitHub after sixty
    seconds and stranded the pull request as `head_read_exhausted` -- against a
    GitHub incident this branch measured lasting hours. A read that lands, lands
    on the first attempt, so nothing here slows the ordinary path down.
    """
    from pr_remediation.loop import HEAD_READ_RETRY_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    assert tick(deps) == 0
    spent = lambda: kinds(deps, loop_id).count("head_unreadable")
    assert spent() == 1, "the first attempt"

    waited = spend_until(deps, spent, HEAD_READ_RETRY_SECONDS * 2)
    assert waited is not None, "the retry must still happen"
    assert waited >= HEAD_READ_RETRY_SECONDS, \
        "a blip must not be re-read at the pass's own cadence"


def test_the_head_read_budget_rides_out_an_outage_of_more_than_an_hour(tmp_path):
    """The whole budget end to end at the real cadence, against the same floor
    the notify budget's own measurement uses and for the same reason."""
    from pr_remediation.loop import DUE_BACKOFF_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    start, gave_up = deps.now(), None
    while deps.now() - start < 24 * 60 * 60:
        deps.now.advance(DUE_BACKOFF_SECONDS)
        tick(deps)
        if deps.slack.escalations:
            gave_up = deps.now() - start
            break
    assert gave_up is not None, "the wait must still end rather than pin the PR"
    assert deps.slack.escalations[0][2] == "head_read_exhausted"
    assert gave_up > 60 * 60, \
        f"gave up on GitHub after only {gave_up / 60:.0f} minutes"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_a_head_read_escalation_retries_through_the_same_notify_budget(tmp_path):
    """head_read_exhausted is an ordinary escalation once it exists: a failed
    post leaves the loop non-terminal and owned, and retries through
    NOTIFY_ATTEMPTS exactly like every other reason -- there is no bespoke
    delivery path for this one to have its own bugs."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, NOTIFY_RETRY_SECONDS, tick

    slack = SlackFake(escalation_ts=None)
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None), slack=slack)
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        tick(deps)
    assert deps.store.get(loop_id).state is not LoopState.ESCALATED, \
        "nobody has heard yet"
    assert "escalation_pending" in kinds(deps, loop_id)
    assert deps.github.released == [], "the PR stays owned until the notice lands"
    assert len(slack.escalations) == 1, "the exhausting tick spent its first attempt"

    slack._escalation_ts = "9.2"  # Slack recovers
    deps.now.advance(NOTIFY_RETRY_SECONDS)
    assert tick(deps) == 1
    assert [e[2] for e in deps.slack.escalations] == \
        ["head_read_exhausted", "head_read_exhausted"], \
        "the failed attempt and the delivered one, both against the same reason"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_a_recovered_read_does_not_carry_its_failure_count_into_the_next_round(
        tmp_path):
    """Scoped to the lane, not the loop: kanban hands every dispatch a fresh
    id, so a round that opens after a recovery starts this budget at zero
    rather than inheriting the round before it. Carrying the count over would
    escalate a brand new round's very first blip."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, LANE_POLL_SECONDS, tick

    github = GitHubFake(head=None)
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=github)
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS - 1):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        assert tick(deps) == 0
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING, \
        "one tick short of round 1's own budget"

    github.head = "b" * 40  # round 1's push shows up
    deps.now.advance(HEAD_READ_RETRY_SECONDS)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).lane_id == "t_1"

    # Round 2: a fresh verdict opens a new lane with its own id.
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    deps.now.advance()
    assert tick(deps) == 1
    loop = deps.store.get(loop_id)
    assert (loop.round_count, loop.lane_id) == (2, "t_2")
    deps.source.verdicts = []
    deps.now.advance(LANE_POLL_SECONDS + 1)

    github.head = None  # round 2's own blip -- not round 1's leftovers
    deps.now.advance()
    assert tick(deps) == 0
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING, \
        "carrying round 1's four attempts over would escalate on this first one"
    assert deps.slack.escalations == []
    assert [e["detail"]["attempt"] for e in deps.store.events(loop_id)
            if e["kind"] == "head_unreadable" and e["detail"]["lane_id"] == "t_2"] \
        == [1]


def test_a_blip_after_a_landed_read_does_not_inherit_earlier_blips_in_the_same_round(
        tmp_path):
    """The sibling of the test above, *within* one lane rather than across two:
    a landed read -- including the grace window's own -- must reset this
    budget too, not only a fresh lane_id. Four blips, then a read that lands
    (unmoved, so it opens the grace window), then one blip on the next look
    inside that window must be attempt 1, not attempt 5: the grace window's
    own successful read is exactly what makes this reachable, because it is
    the one event that can land between two runs of blips on the very same
    lane_id."""
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, tick

    github = GitHubFake(head=None)
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=github)
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS - 1):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        assert tick(deps) == 0
    assert kinds(deps, loop_id).count("head_unreadable") == HEAD_READ_ATTEMPTS - 1

    github.head = "a" * 40  # the read lands, unmoved: opens the grace window
    deps.now.advance(HEAD_READ_RETRY_SECONDS)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.AWAITING_HEAD_MOVE

    github.head = None  # one blip on the very next look inside that window
    deps.now.advance(HEAD_MOVE_GRACE_SECONDS + 1)
    assert tick(deps) == 0
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.AWAITING_HEAD_MOVE, \
        "this is the round's first blip since the read that landed, not its fifth"
    assert deps.slack.escalations == [], \
        "the cumulative bug would escalate head_read_exhausted right here"
    unreadable = [e["detail"] for e in deps.store.events(loop_id)
                  if e["kind"] == "head_unreadable"]
    assert unreadable[-1]["attempt"] == 1


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_tick_neither_escalates_nor_spends_the_head_read_budget(
        tmp_path, mode):
    """A pause must not quietly burn through HEAD_READ_ATTEMPTS while nobody is
    driving: whatever ticks pass while paused must cost the live budget
    nothing, or resuming live would escalate sooner than the budget promises.

    The paused half runs at the pass's own cadence and the live half at
    HEAD_READ_RETRY_SECONDS, which is the point: only a spent attempt spaces
    the next one, and a paused tick spends none."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)

    paused = in_mode(deps, mode)
    for _ in range(HEAD_READ_ATTEMPTS * 2):
        paused.now.advance()
        assert tick(paused) == 0
    assert deps.slack.escalations == []
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    assert "head_unreadable" not in kinds(deps, loop_id), \
        "a paused tick must not write the note a live tick spends its budget on"

    # Resuming live still gets the full budget: nothing was spent while paused.
    live = in_mode(deps, "live")
    for _ in range(HEAD_READ_ATTEMPTS):
        live.now.advance(HEAD_READ_RETRY_SECONDS)
        tick(live)
    assert deps.slack.escalations == \
        [("Ylopo/example#42", 1, "head_read_exhausted")]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_a_paused_head_unreadable_wait_does_not_grow_the_trail_without_end(
        tmp_path):
    """A paused tick never writes the `head_unreadable` note a live tick spends
    its budget on, so `_head_read_attempts` reads zero on every single paused
    tick and the route it names is therefore identical, tick after tick.
    `standing=True` on that route is what lets `_shadow` recognise the repeat
    and record it once; drop it and an hour-long outage leaves 240 identical
    sentences in the trail."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    shadow = in_mode(deps, "shadow")
    for _ in range(20):
        shadow.now.advance()
        assert tick(shadow) == 0
    assert kinds(deps, loop_id).count("shadow_decision") == 1, \
        "an outage held open under a pause must not grow the trail per tick"


def test_a_paused_head_read_exhaustion_decision_does_not_grow_the_trail_either(
        tmp_path):
    """Same shape, past the budget: once a live run has spent
    HEAD_READ_ATTEMPTS - 1 real attempts, one more tick is enough for
    `_route_due` to name `head_read_exhausted` -- and it goes on naming it
    every single tick after that too, live or paused, because nothing paused
    ever advances the count that would move it past this point. If that next
    tick (and every one after it) is paused, `standing=True` is what keeps the
    decision to one recorded sentence rather than one per tick for as long as
    the pause lasts."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS - 1):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        assert tick(deps) == 0

    shadow = in_mode(deps, "shadow")
    # Past the wait the last live attempt bought itself: a paused tick spends
    # no attempt and so spaces none, but it still has to wait out the one the
    # live tick before it did spend.
    shadow.now.advance(HEAD_READ_RETRY_SECONDS)
    for _ in range(20):
        shadow.now.advance()
        assert tick(shadow) == 0
    escalate_notes = [e["detail"] for e in deps.store.events(loop_id)
                      if e["kind"] == "shadow_decision"
                      and e["detail"].get("would") == "escalate"]
    assert len(escalate_notes) == 1, \
        "a paused outage past the live budget must not grow the trail per tick"
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING, \
        "a paused tick must not actually close the loop"


def test_a_lane_pass_that_cannot_post_the_rereview_escalates(tmp_path):
    """No thread means the gate silently drops the post, so the loop would
    stall waiting for a verdict that can never come."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                thread="")
    deps.github.head = "b" * 40
    assert tick(deps) == 1
    assert deps.slack.reposts == []
    assert deps.slack.escalations[0][2] == "rereview_unpostable"
    assert only_loop(deps) is None


def test_a_failed_rereview_post_leaves_the_lane_pass_to_retry(tmp_path):
    """A Slack blip is transient: do not burn the round over it."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                slack=SlackFake(repost_ts=None))
    deps.github.head = "b" * 40
    loop_id = _loop_id(deps)
    for _ in range(3):
        deps.now.advance()
        assert tick(deps) == 0
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    assert deps.slack.escalations == []
    assert kinds(deps, loop_id).count("rereview_post_failed") == 1, \
        "a standing failure is recorded once, not once a tick: the trail is " \
        "read back in full on every tick, so an outage must not grow it"

    deps.slack._repost_ts = "9.0"  # Slack is back
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED


def test_the_lane_pass_leases_no_batch_of_its_own(tmp_path):
    """Lane completion joins the existing due pass rather than adding a third
    claim: a second batch would lease loops the first one is still working."""
    from pr_remediation.loop import tick

    store = CountingStore(Store(tmp_path / "r.db"))
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                store=store)
    store.due_calls = 0
    deps.github.head = "b" * 40
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    assert store.due_calls == 1, "a second due batch is a third claim pass"


def test_a_lease_lost_mid_batch_stops_that_loop_and_no_other(tmp_path):
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    store = LeaseLapses(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict(rid="br_1", pr=42),
                                       verdict(rid="br_2", pr=43)]),
                 lanes=LanesFake(status="done"), store=store)
    assert tick(deps) == 2
    lost = only_loop(deps, 42).loop_id
    kept = only_loop(deps, 43).loop_id
    store.lapsed.add(lost)

    deps.source.verdicts = []
    deps.github.head = "b" * 40
    deps.now.advance(LANE_POLL_SECONDS + 1)
    assert tick(deps) == 1
    assert set(store.renewed) == {lost, kept}, \
        "every loop the batch works must renew the lease it works under"
    assert deps.slack.reposts == [("Ylopo/example#43", 1)]
    assert deps.store.get(lost).state is LoopState.REMEDIATING, \
        "a loop we no longer own must not be driven"
    assert deps.store.get(kept).state is LoopState.REREVIEW_POSTED


def test_a_lapsed_lease_is_found_before_any_read_the_due_pass_would_make(tmp_path):
    """The lease is renewed before `_observe_lane` is ever called for that
    loop, matching `_settle_due`'s own account of itself: renewing first turns
    a lapsed lease into a refusal before any side effect. Reading first
    instead would spend a kanban poll and up to two GitHub calls on a loop
    somebody else already owns -- API budget spent on a loop this tick has no
    business looking at."""
    from pr_remediation.loop import tick

    lanes = ReadRecordingLanes(status="done")
    github = ReadRecordingGitHub()
    store = LeaseLapses(Store(tmp_path / "r.db"))
    deps = drive_to_remediating(tmp_path, lanes=lanes, github=github, store=store)
    loop_id = _loop_id(deps)
    lanes.polled.clear()
    github.reads.clear()
    store.lapsed.add(loop_id)

    deps.now.advance()
    assert tick(deps) == 0
    assert lanes.polled == [], \
        "a lease we no longer hold must not be read against at all"
    assert github.reads == []
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_lane_pass_completes_nothing_a_live_run_started(tmp_path, mode):
    """The pause procedure arriving mid-round: a live tick opened the lane, and
    a paused one must neither post the re-review nor move the loop."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = _loop_id(deps)
    deps.github.head = "b" * 40

    paused = in_mode(deps, mode)
    for _ in range(3):
        paused.now.advance()
        assert tick(paused) == 0
    assert deps.slack.reposts == [], "a paused run posted a re-review"
    assert deps.slack.thinkies_removed == []
    assert deps.slack.escalations == []
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.REMEDIATING
    assert loop.head_sha == "a" * 40

    if mode == "shadow":
        assert [event["detail"]["would"] for event in deps.store.events(loop_id)
                if event["kind"] == "shadow_decision"] == ["lane_pushed"], \
            "a standing decision is rehearsed once, however long the pause"

    # ...and the round is still there for whoever turns the service back on.
    live = in_mode(deps, "live")
    live.now.advance()
    assert tick(live) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED


def test_the_off_switch_polls_no_lane_and_reads_no_github(tmp_path):
    """`off` stops the work, not merely the writes -- in the due pass too,
    where a lane poll and two API reads per open loop per tick is rate budget
    spent on decisions nobody will act on."""
    from pr_remediation.loop import tick

    github = ReadRecordingGitHub()
    lanes = ReadRecordingLanes(status="done")
    deps = drive_to_remediating(tmp_path, lanes=lanes, github=github)
    github.reads.clear()
    lanes.polled.clear()

    off = in_mode(deps, "off")
    off.now.advance()
    assert tick(off) == 0
    assert lanes.polled == [], "the kill switch still went looking at the lane"
    assert github.reads == [], "the kill switch still called GitHub"

    shadow = in_mode(deps, "shadow")
    shadow.now.advance()
    assert tick(shadow) == 0
    assert lanes.polled == ["t_1"], "shadow rehearses the live decision, so it reads"
    assert github.reads == [("has_hold", 42), ("state", 42), ("head_sha", 42)]


def test_a_lane_still_running_when_the_loop_closes_is_recorded(tmp_path):
    """A loop that closes mid-round leaves its lane to nobody: terminal states
    are absorbing and unclaimable, so that worker finishes, pushes to a PR this
    service has just handed back, and is polled by no later tick.

    Driven through the one verdict-path route that still closes over a live
    lane -- an escalation this loop already owes, which is settled ahead of
    every other consideration precisely because a stopped loop stays stopped.
    The outcome routes no longer reach here at all (see
    `test_a_ready_verdict_never_hands_back_a_pr_a_lane_is_pushing_to`); the
    due pass's own `lane_timeout` close is pinned separately.
    """
    from pr_remediation.loop import LANE_TIMEOUT, tick

    deps = drive_to_remediating(tmp_path)  # a lane still in progress
    loop_id = _loop_id(deps)
    # A lane_timeout escalation whose notice Slack refused: the debt is on the
    # trail, the row is still `remediating`, and the lane is still going.
    deps.store.record_note(loop_id, "escalation_pending",
                           {"reason": LANE_TIMEOUT, "attempt": 1}, deps.now())

    deps.source.verdicts = [verdict(rid="br_2", outcome="READY_FOR_HUMAN_REVIEW")]
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    abandoned = [event["detail"] for event in deps.store.events(loop_id)
                 if event["kind"] == "lane_abandoned"]
    assert [d["lane_id"] for d in abandoned] == ["t_1"], \
        "nothing will ever poll that lane again, so say so"


def test_an_escalation_over_an_outstanding_orphan_records_the_lane_it_strands(
        tmp_path):
    """The other shape of the same leak: the row never recorded the lane, so
    only the trail knows one is still running when the loop closes."""
    from pr_remediation.loop import LoopDeps, tick

    store = LosingStore(Store(tmp_path / "r.db"))
    deps = build(tmp_path, SourceFake([verdict(rid="br_1")]), store=store)
    assert tick(deps) == 0
    loop_id = _loop_id(deps)
    assert "orphaned_lane" in kinds(deps, loop_id)

    healed = LoopDeps(**{**deps.__dict__, "store": store._inner})
    healed.source.verdicts = [model_incomplete(rid="br_2")]
    healed.now.advance()
    assert tick(healed) == 1
    assert healed.store.get(loop_id).state is LoopState.ESCALATED
    abandoned = [event["detail"] for event in healed.store.events(loop_id)
                 if event["kind"] == "lane_abandoned"]
    assert [d["lane_id"] for d in abandoned] == ["t_1"]


def test_a_closed_loop_that_stranded_nothing_records_nothing(tmp_path):
    """The note is a leak report, not a footer on every close."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(outcome="READY_FOR_HUMAN_REVIEW")]))
    assert tick(deps) == 1
    assert "lane_abandoned" not in kinds(deps, _loop_id(deps))


def test_a_finished_lane_hands_the_loop_to_the_next_verdict(tmp_path):
    """The circle this pass closes, walked end to end: a verdict opens round 1,
    the lane pushes, the due pass asks for a re-review, and the verdict that
    comes back opens round 2 against the head the lane really pushed to."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = _loop_id(deps)
    deps.github.head = "b" * 40
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]

    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    deps.now.advance()
    assert tick(deps) == 1
    loop = deps.store.get(loop_id)
    assert loop.round_count == 2, "the re-review's verdict starts the next round"
    assert loop.state is LoopState.REMEDIATING
    assert loop.lane_id == "t_2"
    assert deps.lanes.created == [("Ylopo/example#42", 1), ("Ylopo/example#42", 2)]
    assert deps.slack.thinkies_added == ["2.0", "2.0"]


def test_a_no_op_escalation_never_claims_the_lane_is_still_running(tmp_path):
    """`lane_abandoned` means "a worker is still going and nobody will poll it".

    The no-op escalation is the opposite of that: the lane exited, which is how
    the loop reached `awaiting_head_move` at all. Widening the leak report to
    every close from that state -- rather than to the states where a worker is
    genuinely still running -- would fire the alarm on the single most ordinary
    way a round fails, and on a lane this pass had *just* watched exit.
    """
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = _loop_id(deps)
    assert tick(deps) == 1
    deps.now.advance(HEAD_MOVE_GRACE_SECONDS + 1)
    assert tick(deps) == 1
    assert deps.slack.escalations[0][2] == "no_op_lane"
    assert "lane_abandoned" not in kinds(deps, loop_id)


def test_a_head_read_exhaustion_escalation_never_claims_the_lane_is_still_running(
        tmp_path):
    """The same false alarm as the test above, on the other escalation that can
    close a `remediating` loop with a lane still on the row: `head_read_exhausted`
    only ever fires once `_route_due` has already read this lane as finished by
    kanban's own account -- `lane.finished` gates the branch it comes from. The
    row still says `remediating` because a lane whose head never once came back
    readable never earns the transition into `awaiting_head_move` that would say
    otherwise, and `_lane_in_flight` only has the row to go on. Widening the leak
    report to trust it here would fire the alarm on every GitHub outage that
    outlasts the read budget, once per affected PR, beside the notice a human is
    already being sent."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, HEAD_READ_RETRY_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS):
        deps.now.advance(HEAD_READ_RETRY_SECONDS)
        tick(deps)
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.slack.escalations == \
        [("Ylopo/example#42", 1, "head_read_exhausted")]
    assert "lane_abandoned" not in kinds(deps, loop_id)


def test_a_verdict_in_the_grace_window_closes_on_a_lane_that_had_exited(tmp_path):
    """A loop closing from `awaiting_head_move` strands no worker: the lane had
    already exited -- observing that is what put the loop in this state. The
    lane it ran stays recoverable either way, because the transition that
    recorded it is in the trail whatever the row goes on to say.
    """
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = _loop_id(deps)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.AWAITING_HEAD_MOVE

    deps.source.verdicts = [verdict(rid="br_2", outcome="READY_FOR_HUMAN_REVIEW")]
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.READY
    assert "lane_abandoned" not in kinds(deps, loop_id)
    assert "t_1" in [event["detail"].get("lane_id")
                     for event in deps.store.events(loop_id)
                     if event["kind"] == "transition"], \
        "the lane that ran must stay findable in the trail"


# --- C1: an archived lane is a finished lane -------------------------------


def test_an_archived_lane_is_read_as_finished_and_the_loop_moves_on(tmp_path):
    """The board-hygiene path, end to end.

    Archiving a stuck card is what a human does to tidy the board; it is a real
    kanban status (151 rows live) and nothing moves a lane out of it. Before
    C1 this service polled for `failed`/`cancelled` -- statuses kanban rejects
    outright -- and never for `archived`, so an archived lane read as "still
    running" on every tick: the loop stayed in `remediating`, kept the
    ownership label on the pull request, and `active_for_pr` refused every
    later verdict for it, forever.
    """
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="archived"))
    deps.github.head = "b" * 40  # the lane did push before it was archived
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    assert only_loop(deps).state is LoopState.REREVIEW_POSTED


# --- I2: the loop must not write to a merged or closed pull request ---------


def test_a_merged_pr_is_closed_as_stale_instead_of_remediated(tmp_path):
    """The commonest thing in today's backlog: 9 of the 11 distinct pull
    requests this loop would pick up are already MERGED.

    Before I2, `gh pr view` deliberately did not ask for `state`, so a PR
    merged between the verdict and the tick was labelled as owned, handed an
    engineer lane told to "fix them at this head and push", and had a re-review
    posted about it.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 github=GitHubFake(pr_state="MERGED"))
    assert tick(deps) == 1
    assert deps.lanes.created == [], "no lane may be dispatched at a merged PR"
    assert deps.github.claimed == [], "the ownership label must not be applied"
    assert deps.slack.reposts == []
    assert only_loop(deps) is None, "the loop is closed"


def test_closing_stale_notifies_nobody_but_still_hands_the_pr_back(tmp_path):
    """Silent, not absent. A merge is a human's own act and the ordinary end of
    a pull request, so a Slack notice would page whoever merged it about their
    own merge on the commonest path there is. The signals this loop added are
    still cleared -- a stuck ownership label is what makes a PR look owned
    forever."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 github=GitHubFake(pr_state="CLOSED"))
    tick(deps)
    assert deps.slack.escalations == [], "a closed PR is not an escalation"
    assert deps.slack.ready == []
    assert deps.github.released == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_removed == ["2.0"]


def test_a_stale_close_is_recorded_as_stale_not_as_an_escalation(tmp_path):
    from pr_remediation.loop import PR_NOT_OPEN, tick

    store = Store(tmp_path / "r.db")
    deps = build(tmp_path, SourceFake([verdict()]), store=store,
                 github=GitHubFake(pr_state="MERGED"))
    tick(deps)
    loop_id = next(iter(store._db.execute("SELECT loop_id, state, "
                                            "escalation_reason FROM loops")))
    assert loop_id[1] == LoopState.STALE.value
    assert loop_id[2] == PR_NOT_OPEN
    assert PR_NOT_OPEN in kinds(deps, loop_id[0])


def test_an_unreadable_state_is_not_read_as_a_merged_pr(tmp_path):
    """None means unknown, never "not open". Closing a loop is irreversible --
    terminal states are absorbing and active_for_pr then refuses every later
    generation for the PR -- so a blip must leave the loop where it was."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 github=GitHubFake(pr_state=None))
    assert tick(deps) == 1
    assert deps.lanes.created == [("Ylopo/example#42", 1)]
    assert only_loop(deps).state is LoopState.REMEDIATING


def test_the_due_pass_does_not_post_a_rereview_into_a_merged_pr(tmp_path):
    """The other half of I2. The lane ran, pushed, and by the time it finished
    the PR had been merged -- so the head moved, which is exactly the condition
    that earns a re-review. Posting one asks Boris to review a merged PR and
    then waits forever for a verdict about it."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    deps.github.head = "b" * 40           # the lane really did push
    deps.github.pr_state = "MERGED"       # ...and then it was merged
    assert tick(deps) == 1
    assert deps.slack.reposts == [], "no re-review may be posted at a merged PR"
    assert deps.slack.escalations == []
    assert only_loop(deps) is None
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_a_merge_inside_the_grace_window_closes_the_loop_stale(tmp_path):
    """`awaiting_head_move -> stale`, driven. The lane exited without a visible
    push, the loop took its one grace window, and the pull request was merged
    while it waited -- which is a live edge in the store's table and the
    commonest way this particular wait ends, since a human merging is what
    usually makes a late push moot."""
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = _loop_id(deps)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.AWAITING_HEAD_MOVE

    deps.github.pr_state = "MERGED"
    deps.now.advance(HEAD_MOVE_GRACE_SECONDS + 1)
    assert tick(deps) == 1
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.STALE, \
        "a merged PR is not a no-op lane a human must accept"
    assert loop.escalation_reason == "pr_not_open"
    assert deps.slack.escalations == [], "nobody is paged about their own merge"
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_a_merge_while_boris_re_reviews_closes_the_loop_stale(tmp_path):
    """`rereview_posted -> stale`, driven from the verdict side. The re-review
    ran to completion and the pull request was merged before its verdict landed
    -- the ordinary ending, and the other live edge into `stale` that nothing
    pinned."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.github.pr_state = "MERGED"
    deps.source.verdicts = [verdict(rid="br_9", head="b" * 40)]
    deps.now.advance()

    assert tick(deps) == 1
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.STALE
    assert loop.escalation_reason == "pr_not_open"
    assert deps.lanes.created == [("Ylopo/example#42", 1)], \
        "no round is opened against a branch nobody can push to"
    assert deps.slack.escalations == []


def test_a_paused_tick_records_the_stale_decision_and_closes_nothing(tmp_path):
    """The mode invariant holds for the new route like every other: shadow
    decides and writes no loop state."""
    from pr_remediation.loop import ROUTE_CLOSE_STALE, tick

    deps = build(tmp_path, SourceFake([verdict()]),
                 github=GitHubFake(pr_state="MERGED"))
    shadow = in_mode(deps, "shadow")
    assert tick(shadow) == 0
    loop = only_loop(shadow)
    assert loop is not None and loop.state is LoopState.OBSERVED
    assert shadow.github.released == []
    decisions = [e["detail"] for e in shadow.store.events(loop.loop_id)
                 if e["kind"] == "shadow_decision"]
    assert decisions == [{"would": ROUTE_CLOSE_STALE, "pr_state": "MERGED"}]


# --- I1: the due pass must rotate ------------------------------------------


class RecordsDueBatches:
    """A real store that remembers which loops each due batch actually held."""

    def __init__(self, inner):
        self._inner = inner
        self.batches = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def claim_due(self, *, now, limit):
        claimed = self._inner.claim_due(now=now, limit=limit)
        self.batches.append([loop.loop_id for loop in claimed])
        return claimed


def _open_many(store, clock, count):
    """`count` loops on distinct pull requests, oldest first."""
    loop_ids = []
    for index in range(count):
        clock.advance(1.0)
        loop = store.open_loop(
            owner="Ylopo", repo="example", pr_number=100 + index,
            head_sha="a" * 40, boris_request_id=f"br_{index}",
            root_thread_ts=f"{index}.0", now=clock())
        loop_ids.append(loop.loop_id)
    return loop_ids


def test_the_due_pass_visits_every_loop_and_starves_none(tmp_path):
    """The fairness the pass had none of.

    `claim_due` selects `next_attempt_at <= now` ordered by age, LIMIT 20, and
    no route that leaves a loop non-terminal used to advance `next_attempt_at`
    -- not ROUTE_HELD, not ROUTE_AUTHOR_UNREADABLE, not ROUTE_UNKNOWN_OUTCOME,
    nor a `rereview_posted` loop whose verdict never arrives, nor the ordinary
    "nothing owed" return. So the same oldest 20 were claimed on every tick and
    loops 21+ were never visited at all: once 20 permanently-due loops
    accumulate, remediation halts for every other pull request, with no alarm.

    Note the compounding the reproduction showed: the likeliest operational
    failure -- re-review posts not being admitted -- is itself what manufactures
    permanently-due rows.
    """
    from pr_remediation.config import RemediationConfig
    from pr_remediation.loop import DUE_LIMIT, tick

    clock = Clock()
    store = RecordsDueBatches(Store(tmp_path / "r.db"))
    loop_ids = _open_many(store, clock, DUE_LIMIT + 5)
    deps = build(tmp_path, SourceFake([]), store=store, clock=clock)

    for _ in range(6):
        clock.advance(RemediationConfig().poll_seconds)
        tick(deps)

    visited = {loop_id for batch in store.batches for loop_id in batch}
    starved = [loop_id for loop_id in loop_ids if loop_id not in visited]
    assert not starved, (
        f"{len(starved)} of {len(loop_ids)} loops were never claimed in six "
        f"ticks; the pass is stuck on its oldest {DUE_LIMIT}")


def test_a_loop_the_due_pass_leaves_open_is_not_immediately_due_again(tmp_path):
    """The mechanism behind the rotation above, pinned on its own so a future
    route cannot quietly reintroduce a permanently-due row."""
    from pr_remediation.loop import tick

    clock = Clock()
    store = Store(tmp_path / "r.db")
    loop_id = _open_many(store, clock, 1)[0]
    deps = build(tmp_path, SourceFake([]), store=store, clock=clock)

    clock.advance(1.0)
    tick(deps)
    row = store._db.execute("SELECT next_attempt_at FROM loops WHERE loop_id=?",
                            (loop_id,)).fetchone()
    assert row["next_attempt_at"] > clock(), \
        "the loop is still due the instant the pass that just visited it ends"


def test_rescheduling_never_shortens_a_wait_a_route_chose(tmp_path):
    """The grace window is the case that matters: `_await_head_move` parks the
    loop for HEAD_MOVE_GRACE_SECONDS, and a backoff applied on top of it must
    not cut that short -- the window exists to let a late push become visible."""
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    loop_id = only_loop(deps).loop_id
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.AWAITING_HEAD_MOVE
    row = deps.store._db.execute(
        "SELECT next_attempt_at FROM loops WHERE loop_id=?", (loop_id,)).fetchone()
    assert row["next_attempt_at"] == deps.now() + HEAD_MOVE_GRACE_SECONDS


# --- C2: a lane that never terminates must not wedge the pull request -------


def test_a_lane_that_never_finishes_is_given_up_on_and_escalated(tmp_path):
    """Every other bound in this module starts counting only once kanban has
    already called the lane finished, so a lane that never says so outlived all
    of them: the loop stayed in `remediating` forever, holding the ownership
    label, and `active_for_pr` refused every later generation of that pull
    request while this pass polled kanban every few seconds indefinitely.

    boris_intake bounds the same shape at 90 minutes (REVIEW_WAIT_SECONDS),
    added after a real incident.

    The loop still closes at the bound -- `active_for_pr` must eventually stop
    finding it -- but a `running` card is kanban's own word that a worker may
    still be pushing to this branch, so standing down here would be the
    collision this service exists to prevent: a human sees the label gone and
    reasonably takes that as permission to touch a branch a live worker still
    owns. So the label and the reaction stay, and the notice names the lane
    that is still going. See test_a_terminal_lane_at_settle_time_stands_down_
    as_before for the case where kanban itself has since called the lane
    finished.
    """
    from pr_remediation.loop import LANE_TIMEOUT, LANE_TIMEOUT_LANE_ALIVE, \
        LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    loop_id = only_loop(deps).loop_id

    deps.now.advance(LANE_WAIT_SECONDS)
    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [], \
        "a lane kanban has not called finished may still push -- the label " \
        "must stay, not be handed back while a worker can still commit to it"
    assert deps.slack.thinkies_removed == [], \
        "keep the reaction consistent with the label -- both signal the " \
        "same thing, that work may still be in flight"
    assert deps.slack.escalation_lane_ids == ["t_1"], \
        "the escalation must name the still-running lane"
    kept = [e["detail"] for e in deps.store.events(loop_id)
            if e["kind"] == LANE_TIMEOUT_LANE_ALIVE]
    assert kept and kept[0]["lane_id"] == "t_1"


def test_a_terminal_lane_at_settle_time_stands_down_as_before(tmp_path):
    """The due pass's own observation only ever requires "not yet terminal" to
    reach the lane_timeout bound at all, so a settle re-checks kanban fresh
    right before deciding whether to keep the ownership signals. If kanban's
    account has moved on to a terminal status by then -- the card finished in
    the gap between the due pass's read and this one -- today's behaviour
    holds exactly as it did before this change: stand down like any other
    escalation.
    """
    from pr_remediation.loop import LANE_TIMEOUT, LANE_TIMEOUT_LANE_ALIVE, \
        LANE_WAIT_SECONDS, tick

    lanes = SequencedLanesFake(["running", "done"])
    deps = drive_to_remediating(tmp_path, lanes=lanes)
    loop_id = only_loop(deps).loop_id

    deps.now.advance(LANE_WAIT_SECONDS)
    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)], \
        "a lane kanban now calls finished earns the ordinary stand-down"
    assert deps.slack.thinkies_removed
    assert deps.slack.escalation_lane_ids == [None], \
        "a lane confirmed finished is not named as still running"
    assert not [e for e in deps.store.events(loop_id)
                if e["kind"] == LANE_TIMEOUT_LANE_ALIVE]


def test_an_unreadable_lane_status_at_settle_time_is_treated_as_alive(tmp_path):
    """A read that fails cannot tell "the worker just finished" from "kanban
    is having a bad moment" -- those look identical from here. Reading the
    failure as "finished" would release the label and the reaction over a
    lane that, for all this settle knows, is still pushing to the branch; the
    safe default treats an unreadable status the same as one this build
    merely does not recognise as terminal, and keeps both signals.
    """
    from pr_remediation.loop import LANE_TIMEOUT_LANE_ALIVE, LANE_WAIT_SECONDS, tick

    lanes = RaisesOnSecondReadLanesFake()
    deps = drive_to_remediating(tmp_path, lanes=lanes)
    loop_id = only_loop(deps).loop_id

    deps.now.advance(LANE_WAIT_SECONDS)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [], \
        "an unreadable status must not be read as 'the lane is done'"
    assert deps.slack.thinkies_removed == []
    assert deps.slack.escalation_lane_ids == ["t_1"]
    assert [e for e in deps.store.events(loop_id)
            if e["kind"] == LANE_TIMEOUT_LANE_ALIVE]


def test_other_escalation_reasons_still_release_ownership_with_a_lane_in_flight(
        tmp_path):
    """The lane_timeout exception is keyed on the reason string, not on
    whether a lane happens to still be recorded on the row: a verdict that
    escalates for an unrelated reason while a lane is genuinely still running
    must keep releasing the ownership signals exactly as it always has."""
    from pr_remediation.loop import NOT_CHAD_AUTHORED, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    loop_id = only_loop(deps).loop_id
    deps.github.author_login = "someone-else"
    deps.source.verdicts = [verdict(rid="br_2")]

    assert tick(deps) == 1
    assert deps.slack.escalations[-1][2] == NOT_CHAD_AUTHORED
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_removed
    assert deps.slack.escalation_lane_ids == [None], \
        "notify_escalation must not be handed a lane_id for a reason that " \
        "is not lane_timeout"


def test_a_deleted_kanban_card_does_not_wedge_the_loop_forever(tmp_path):
    """`lane_status()` answers None for a card that is gone. Nothing else in
    this pass can ever move such a loop on: it is not finished, so no head is
    read, no grace window opens and no head-read budget is spent.

    None is not evidence the lane is done, either -- it is kanban saying it
    found no row for this id, which reads the same whether a human archived
    the card or the read itself came back short. Releasing the label on that
    basis would be exactly the "unknown read as dead" mistake this loop's
    settle refuses to make -- see _lane_still_running.
    """
    from pr_remediation.loop import LANE_TIMEOUT, LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status=None))
    deps.now.advance(LANE_WAIT_SECONDS)

    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert only_loop(deps) is None
    assert deps.github.released == [], \
        "an unresolvable card is not evidence the lane is done"


class RaisingLanesFake(LanesFake):
    """A kanban whose status() raises once armed.

    `KanbanIO.lane_status` turns any `sqlite3.Error` into a `RuntimeError`, and
    a busy or locked `kanban.db` is a known transient in this service -- worker
    spawn bursts contend on the WAL. Armed after dispatch, because the round has
    to open before there is a lane to fail to read.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.armed = False

    def status(self, lane_id):
        if self.armed:
            raise RuntimeError("database is locked")
        return super().status(lane_id)


def test_an_unreadable_kanban_does_not_wedge_the_loop_past_its_bound(tmp_path):
    """An unreadable lane is an unfinished lane, not an exception.

    Unguarded, the raise landed in `_settle_due`'s per-loop `except`, so
    `_route_due` never ran for this loop at all -- and LANE_WAIT_SECONDS is the
    one bound here that does not wait for a finished lane, so it was the only
    thing that could ever have ended the wait. Ten times that bound later the
    row was still `remediating`, nothing had been escalated, the ownership
    label was still on the pull request and `active_for_pr` was still refusing
    every later generation of it.

    `_lane_still_running` catches exactly this exception at the sibling call
    site and answers "still running"; this is the same answer, given by the
    same reasoning, at the site that was missing it.
    """
    from pr_remediation.loop import LANE_TIMEOUT, LANE_WAIT_SECONDS, tick

    lanes = RaisingLanesFake(status="running")
    deps = drive_to_remediating(tmp_path, lanes=lanes)
    loop_id = only_loop(deps).loop_id
    lanes.armed = True

    deps.now.advance(LANE_WAIT_SECONDS)
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert deps.github.released == [], \
        "a kanban that cannot be read is not evidence the lane is done"


def test_an_unreadable_kanban_inside_the_bound_leaves_the_loop_alone(tmp_path):
    """And it is a blip until the bound says otherwise: unreadable is neither
    "finished" (which would read an unmoved head as a no-op round and escalate
    a lane that may well be working) nor grounds for escalating on its own."""
    from pr_remediation.loop import LANE_WAIT_SECONDS, tick

    lanes = RaisingLanesFake(status="running")
    deps = drive_to_remediating(tmp_path, lanes=lanes)
    loop_id = only_loop(deps).loop_id
    lanes.armed = True

    deps.now.advance(LANE_WAIT_SECONDS - 60)
    assert tick(deps) == 0
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    assert deps.slack.escalations == []


def test_a_running_lane_inside_the_bound_is_left_alone(tmp_path):
    """The bound must not cut a lane that is simply taking a while: it is
    dispatched with --max-retries 1, so kanban may legitimately run the work
    twice inside one card's life."""
    from pr_remediation.loop import LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    deps.now.advance(LANE_WAIT_SECONDS - 60)

    assert tick(deps) == 0
    assert deps.slack.escalations == []
    assert only_loop(deps).state is LoopState.REMEDIATING


def test_the_lane_timeout_reason_is_its_own_dead_end(tmp_path):
    """Three different things to go and look at: `no_op_lane` means the lane
    finished and pushed nothing (look at the branch), `head_read_exhausted`
    means GitHub would not say what it pushed (look at GitHub), and this means
    the lane never finished at all (look at the kanban card)."""
    from pr_remediation.loop import HEAD_READ_EXHAUSTED, LANE_TIMEOUT
    from pr_remediation.models import EscalationReason

    assert LANE_TIMEOUT not in (HEAD_READ_EXHAUSTED,
                                EscalationReason.NO_OP_LANE.value)


def test_a_wedged_lane_that_is_escalated_is_reported_as_still_running(tmp_path):
    """Nothing here can cancel a kanban lane, and a `running` card may well
    still be pushing to the branch this loop has just handed back -- so the
    human who is being told the PR is stuck is also told where the lane went."""
    from pr_remediation.loop import EVENT_LANE_ABANDONED, LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    loop_id = only_loop(deps).loop_id
    deps.now.advance(LANE_WAIT_SECONDS)
    tick(deps)

    abandoned = [e["detail"] for e in deps.store.events(loop_id)
                 if e["kind"] == EVENT_LANE_ABANDONED]
    assert abandoned and abandoned[0]["lane_id"] == "t_1"


def test_a_paused_tick_records_the_lane_timeout_and_escalates_nothing(tmp_path):
    from pr_remediation.loop import LANE_TIMEOUT, LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    loop_id = only_loop(deps).loop_id
    deps.now.advance(LANE_WAIT_SECONDS)

    shadow = in_mode(deps, "shadow")
    assert tick(shadow) == 0
    assert shadow.slack.escalations == []
    assert shadow.store.get(loop_id).state is LoopState.REMEDIATING
    decisions = [e["detail"] for e in shadow.store.events(loop_id)
                 if e["kind"] == "shadow_decision"]
    assert decisions[-1]["would"] == "escalate"
    assert decisions[-1]["reason"] == LANE_TIMEOUT


# --- the still-running progress notice --------------------------------------
#
# The gap the first live ISSUES_FOUND run exposed: a round ran for twenty
# minutes with only the ownership label and the thinkies reaction to show for
# it, and those look identical whether the round is healthy or wedged. The due
# pass now says, once, that a round is still going once it has run past
# STILL_RUNNING_THRESHOLD_SECONDS -- well under LANE_WAIT_SECONDS, which stays
# exactly as it was.

def test_a_round_under_the_threshold_posts_nothing(tmp_path):
    from pr_remediation.loop import STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path)  # LanesFake defaults to in_progress
    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS - 60)
    assert tick(deps) == 0
    assert deps.slack.still_running == []


def test_a_round_over_the_threshold_posts_the_notice_once(tmp_path):
    from pr_remediation.loop import STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path, thread="1.0")
    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS)
    assert tick(deps) == 0, "a progress notice advances nothing"
    assert len(deps.slack.still_running) == 1
    slug, elapsed_minutes, timeout_minutes, thread_ts = deps.slack.still_running[0]
    assert slug == "Ylopo/example#42"
    assert elapsed_minutes >= 30
    assert timeout_minutes == 180
    assert thread_ts == "1.0", "posted to the review thread, not the reaction anchor"
    loop_id = only_loop(deps).loop_id
    assert only_loop(deps).state is LoopState.REMEDIATING, \
        "a progress notice changes no loop state"
    assert kinds(deps, loop_id).count("still_running") == 1


def test_a_further_tick_does_not_post_the_notice_again(tmp_path):
    from pr_remediation.loop import STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path)
    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS)
    assert tick(deps) == 0
    assert len(deps.slack.still_running) == 1

    for _ in range(3):
        deps.now.advance(30.0)
        assert tick(deps) == 0
    assert len(deps.slack.still_running) == 1, \
        "a standing notice is posted once per round, not once a tick"
    loop_id = only_loop(deps).loop_id
    assert kinds(deps, loop_id).count("still_running") == 1


def test_the_notice_carries_no_state_change_and_is_not_counted_as_progress(
        tmp_path):
    from pr_remediation.loop import STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path)
    loop_id = only_loop(deps).loop_id
    before = deps.store.get(loop_id)
    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS)
    assert tick(deps) == 0
    after = deps.store.get(loop_id)
    assert after.state == before.state
    assert after.round_count == before.round_count
    assert after.lane_id == before.lane_id
    assert after.head_sha == before.head_sha


def test_a_paused_tick_posts_no_still_running_notice(tmp_path):
    """The kill switch, structurally: `_rehearse` never reaches
    `_notify_still_running`, so a paused tick must post nothing at all --
    only the shadow decision is recorded."""
    from pr_remediation.loop import STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS)

    shadow = in_mode(deps, "shadow")
    assert tick(shadow) == 0
    assert shadow.slack.still_running == []
    assert shadow.store.get(loop_id).state is LoopState.REMEDIATING
    decisions = [e["detail"] for e in shadow.store.events(loop_id)
                 if e["kind"] == "shadow_decision"]
    assert decisions[-1]["would"] == "still_running"


def test_a_failed_notice_post_does_not_disturb_the_loop(tmp_path):
    """Handled the way every other Slack call here is: no raise, no block,
    nothing consumed. The notice is best-effort and is not retried forever --
    it is marked said for this round whether or not it actually landed."""
    from pr_remediation.loop import STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path)
    deps.slack.still_running_fails = True
    loop_id = only_loop(deps).loop_id
    before = deps.store.get(loop_id)
    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS)

    assert tick(deps) == 0  # must not raise
    after = deps.store.get(loop_id)
    assert after.state == before.state
    assert after.round_count == before.round_count
    assert after.lane_id == before.lane_id
    assert len(deps.slack.still_running) == 1, "the attempt still happened"

    # And it does not retry forever: a further tick posts nothing more.
    deps.now.advance(30.0)
    assert tick(deps) == 0
    assert len(deps.slack.still_running) == 1


def test_the_escalation_timeout_still_fires_at_its_own_bound_regardless(
        tmp_path):
    """The still-running notice must not delay, replace, or otherwise touch
    LANE_WAIT_SECONDS: a round that keeps not finishing still escalates at
    exactly the same bound as before this notice existed."""
    from pr_remediation.loop import LANE_TIMEOUT, LANE_WAIT_SECONDS, \
        STILL_RUNNING_THRESHOLD_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    loop_id = only_loop(deps).loop_id

    deps.now.advance(STILL_RUNNING_THRESHOLD_SECONDS)
    assert tick(deps) == 0
    assert len(deps.slack.still_running) == 1
    assert deps.slack.escalations == []

    deps.now.advance(LANE_WAIT_SECONDS - STILL_RUNNING_THRESHOLD_SECONDS)
    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    # The lane is still `running` by kanban's own account, so the ownership
    # label stays -- see test_a_lane_that_never_finishes_is_given_up_on_and_
    # escalated for why.
    assert deps.github.released == []


# --- a re-review request that never reached Boris ---------------------------
#
# `rereview_posted` is left only by an arriving verdict, and boris_intake's
# gate reads GitHub synchronously while admitting a message -- so during a
# GitHub outage the gate cannot tell a failed read from a missing pull request
# and rejects the loop's post outright (`pr_not_found`,
# `gate_snapshot_unavailable`). Three live loops parked that way in one
# afternoon: `_lane_pending` excludes `rereview_posted`, so `_observe_lane`
# answered None, `_route_due` returned on `lane is None`, and every tick
# deferred the row with nothing recorded and no alarm. pr_discovery could not
# rescue them either -- the loop holds `hermes-owner-andrej`, which discover.py
# treats as a kill switch.


def drive_to_rereview_posted(tmp_path, **kwargs):
    """A round that ran, pushed, and had its re-review posted.

    Where a loop waits for the verdict that is the only thing which leaves
    this state -- and where, if the gate never admitted that post, it waits
    for good. Driven through the real path (`_lane_pushed`) rather than a
    hand-written row, so the head the loop carries is the one its lane pushed
    and not the one the round started from: the acknowledgement check asks
    about the former, and a fixture that let the two be the same literal
    could not tell them apart.
    """
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                **kwargs)
    deps.github.head = "b" * 40  # the lane pushed
    assert tick(deps) == 1
    loop = only_loop(deps)
    assert loop.state is LoopState.REREVIEW_POSTED
    assert loop.head_sha == "b" * 40
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    return deps


def reposts(deps, loop_id):
    """The re-posts that actually landed."""
    return [e["detail"] for e in deps.store.events(loop_id)
            if e["kind"] == "rereview_reposted"]


def repost_attempts(deps, loop_id):
    """Every re-post attempt, landed or refused -- what the budget counts."""
    return [(e["kind"], e["detail"]["attempt"]) for e in deps.store.events(loop_id)
            if e["kind"] in ("rereview_reposted", "rereview_repost_failed")]


def test_an_admitted_rereview_is_left_alone(tmp_path):
    """The verdict is coming, so there is nothing owed and nothing to say --
    however long it takes. Deleting the `admitted is True` branch would
    re-post on top of a live review the moment the window elapsed, which is
    a duplicate review and a second lane's worth of noise in the thread."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)  # SourceFake admits by default
    deps.now.advance(3 * 60 * 60)
    assert tick(deps) == 0
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "one post, the original -- a review already exists at this head"
    assert only_loop(deps).state is LoopState.REREVIEW_POSTED


def test_a_rereview_the_gate_never_admitted_is_posted_again(tmp_path):
    """The three stranded loops, recovered."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False  # the gate rejected it: pr_not_found
    loop_id = only_loop(deps).loop_id
    deps.now.advance(10 * 60)

    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1),
                                  ("Ylopo/example#42", 1)]
    assert reposts(deps, loop_id) == [{"attempt": 1}]
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.REREVIEW_POSTED, \
        "the self-edge re-arms the window; it does not start a new round"
    assert loop.round_count == 1, "a lost post is not a spent round"
    assert deps.lanes.created == [("Ylopo/example#42", 1)], \
        "re-sending a request must not dispatch a second remediation lane"


def test_the_acknowledgement_is_checked_at_the_loops_own_head(tmp_path):
    """`b`, the head the lane pushed and the re-review was posted for -- not
    `a`, the head the round started from. A stale request at the previous
    head is exactly what a round leaves behind, and reading one as this
    request's acknowledgement strands the loop silently."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.now.advance(10 * 60)
    tick(deps)
    assert deps.source.exists_calls[-1][:4] == ("Ylopo", "example", 42, "b" * 40)


def test_the_repost_waits_out_the_grace_window(tmp_path):
    """Nine minutes is inside it, ten is not.

    The window is not slack for the gate, which admits synchronously -- it is
    for `catch_up`, which replays a message missed while the daemon was down
    on the next start and can legitimately admit minutes late. Re-posting
    inside it would race that replay and buy a duplicate.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.now.advance(9 * 60)
    assert tick(deps) == 0
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "nine minutes in, the post may still be being replayed"

    deps.now.advance(60)
    assert tick(deps) == 1
    assert len(deps.slack.reposts) == 2


def test_an_unreadable_intake_database_provokes_no_repost(tmp_path):
    """None is not False. Waiting costs a tick; guessing costs a duplicate
    review, so an intake database this pass could not read leaves the loop
    exactly where it is -- for as long as it stays unreadable."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = None
    deps.now.advance(2 * 60 * 60)
    assert tick(deps) == 0
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]
    assert only_loop(deps).state is LoopState.REREVIEW_POSTED


def test_an_unreadable_intake_database_says_so_once(tmp_path):
    """The one branch here that can repeat indefinitely on a fault nothing in
    this service controls, and it used to record nothing at all -- so a loop
    could sit through an outage of any length leaving no trace whatever, which
    is precisely the silence this route exists to end. Standing, like
    `head_unreadable`, its closest analogue: said once, not once a tick."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = None
    loop_id = only_loop(deps).loop_id
    for _ in range(20):
        deps.now.advance(10 * 60)
        assert tick(deps) == 0
    notes = [e["detail"] for e in deps.store.events(loop_id)
             if e["kind"] == "intake_unreadable"]
    assert notes == [{"head_sha": "b" * 40}], \
        "one sentence for the outage, naming the head it could not ask about"


def test_a_held_pr_is_never_re_reviewed_by_the_acknowledgement_guard(tmp_path):
    """Chad's kill switch reaches this route too.

    `hermes-hold` says a pull request has been taken back, and this is the one
    place the loop asks boris_intake to start a whole review -- which publishes
    a GitHub review on it. That gate has no hold check of its own (its
    admission looks at CI state and the review identity, nothing else), so if
    this route does not read the label nothing downstream will. `has_hold`
    fails closed, so a GitHub this pass cannot read lands here as well.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False  # the gate rejected the post
    loop_id = only_loop(deps).loop_id
    deps.github.hold = True
    deps.now.advance(10 * 60)

    assert tick(deps) == 0
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "the original post and no other: Chad has taken this PR back"
    assert only_loop(deps).state is LoopState.REREVIEW_POSTED
    assert reposts(deps, loop_id) == [], "a held PR spends none of the budget"
    assert "held" in kinds(deps, loop_id)


def test_a_merged_pr_waiting_on_a_rejected_repost_is_closed_stale(tmp_path):
    """Chad merging while Boris re-reviews is the ordinary ending, not an
    incident -- and the gate rejects every post for a pull request that is no
    longer open (`pr_not_open`).

    Without the state read this loop spent all twelve retries against that
    rejection, two hours of them, and then escalated as
    `rereview_unacknowledged`: a page about somebody's own merge, carrying the
    one reason that sends them to the intake gate's admission log to find out
    why. `_close_stale` exists precisely so that ending is silent.
    """
    from pr_remediation.loop import REREVIEW_REPOST_ATTEMPTS, tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    loop_id = only_loop(deps).loop_id
    deps.github.pr_state = "MERGED"

    for _ in range(REREVIEW_REPOST_ATTEMPTS + 2):
        deps.now.advance(10 * 60)
        tick(deps)

    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.STALE
    assert loop.escalation_reason == "pr_not_open"
    assert deps.slack.escalations == [], "nobody is paged about their own merge"
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "and no re-review is asked for on a branch nobody can push to"
    assert deps.github.released == [("Ylopo", "example", 42)], \
        "the ownership label still comes off"


def test_the_acknowledgement_guard_reads_github_only_when_it_would_post(tmp_path):
    """Three reads, ordered by cost, and each one able to end the tick.

    The grace window is two numbers already in hand; the admission check is a
    local sqlite query; the GitHub read is a `gh pr view` subprocess, and this
    route is reached on every tick for every `rereview_posted` loop -- including
    every loop that is admitted and simply waiting for its verdict. Taking that
    read any earlier spends a subprocess per loop per tick to learn there is
    nothing to do.
    """
    from pr_remediation.loop import tick

    github = ReadRecordingGitHub()
    deps = drive_to_rereview_posted(tmp_path, github=github)
    github.reads.clear()

    for _ in range(19):  # inside the grace window
        deps.now.advance(30.0)
        assert tick(deps) == 0
    assert github.reads == [], "nothing inside the window is worth a read"

    deps.now.advance(30.0)  # past it, and the gate admitted the request
    assert tick(deps) == 0
    assert github.reads == [], "an admitted request owes no write and no read"

    deps.source.admitted = False  # now the post is owed
    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    assert github.reads == [("has_hold", 42), ("state", 42)], \
        "read before write, on the tick that actually writes"


def test_the_off_switch_reads_no_github_for_the_acknowledgement_guard(tmp_path):
    """The route grew a GitHub read, so it grew the kill switch's reach too.

    `off` stops the work and not merely the writes, and it must not claim a
    decision it never took -- an unobserved tick is recorded as exactly that,
    the same answer `_route` gives when nothing was read.
    """
    from pr_remediation.loop import tick

    github = ReadRecordingGitHub()
    deps = drive_to_rereview_posted(tmp_path, github=github)
    deps.source.admitted = False
    loop_id = only_loop(deps).loop_id
    github.reads.clear()
    deps.now.advance(10 * 60)

    off = in_mode(deps, "off")
    assert tick(off) == 0
    assert github.reads == [], "the kill switch still called GitHub"
    assert reposts(off, loop_id) == []
    assert [e["detail"]["would"] for e in off.store.events(loop_id)
            if e["kind"] == "shadow_decision"] == ["unobserved"]


def test_the_window_is_waited_out_without_reading_the_intake_database(tmp_path):
    """Two numbers already in hand versus a database this process does not
    own. Inside the window the read cannot change the answer, and this route
    is reached on every tick for every `rereview_posted` loop -- including
    every loop that is admitted and simply waiting for its verdict."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.exists_calls.clear()
    for _ in range(19):
        deps.now.advance(30.0)
        assert tick(deps) == 0
    assert deps.source.exists_calls == [], \
        "nothing inside the window is worth a read of boris_intake"

    deps.now.advance(30.0)
    tick(deps)
    assert len(deps.source.exists_calls) == 1, "and the window ending earns one"


def test_a_review_that_never_completes_escalates_on_the_absolute_deadline(tmp_path):
    """`admitted is True` is not a promise that a verdict is coming.

    boris_intake has a second terminal state -- `slack_unavailable`, whose
    transition set is empty -- so a request can end there and never be
    `complete`. `new_verdicts` then never returns it, `request_exists` goes on
    answering True at this loop's head, and the loop parks with nothing
    recorded and no alarm: the original bug, through the door the new guard
    holds open. A request wedged in `reviewing` does the same.

    The deadline is the one check here that does not depend on the intake
    read, which is why it is the only one that can close this wait.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)  # admitted, and it stays admitted
    loop_id = only_loop(deps).loop_id

    deps.now.advance(3 * 60 * 60)
    assert tick(deps) == 0, \
        "three hours in, an admitted review may still legitimately be running"
    assert deps.slack.escalations == []

    deps.now.advance(60 * 60 + 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")], \
        "and the notice says which of the two ways this wait can end happened: " \
        "the gate took this request, so the place to look is that request's row"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_a_wait_older_than_the_deadline_still_gets_its_retries(tmp_path):
    """The deadline and the budget answer different questions, and the backlog
    is where applying one to the other's case shows.

    Every loop this guard was written to rescue is already hours into its wait
    when the guard first runs, and a loop parked overnight is a day into it. A
    deadline measured from when the wait began, applied to a request the gate
    never admitted, escalates all of them on the first tick that ever looks --
    with not one retry -- so whether a given pull request is recovered or
    permanently stranded turns on when this service happened to be deployed.
    The retry budget already bounds this case, at twelve attempts a window
    apart, counted from the first tick that finds it and not from whenever the
    row was last touched.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.source.admitted = False
    deps.now.advance(2 * 24 * 60 * 60)  # parked since the day before yesterday

    assert tick(deps) == 1
    assert deps.slack.escalations == [], \
        "nothing has been retried even once, so nothing is owed to a human"
    assert reposts(deps, loop_id) == [{"attempt": 1}]

    for _ in range(20):
        deps.now.advance(10 * 60)
        tick(deps)
        if deps.store.get(loop_id).state is LoopState.ESCALATED:
            break
    assert len(reposts(deps, loop_id)) == 12, "the whole budget, however old"
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_unacknowledged")]


def test_the_absolute_deadline_covers_an_unreadable_intake_too(tmp_path):
    """The `admitted is None` branch needs no attempt bound of its own: it
    cannot be retried into an answer, and this deadline ends its wait like
    every other."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = None
    loop_id = only_loop(deps).loop_id
    deps.now.advance(4 * 60 * 60 + 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_the_admission_question_is_anchored_to_the_wait_not_the_last_move(tmp_path):
    """`updated_at` slides forward on every landed re-post. The wait does not.

    Asked from the row's last move instead of from where the wait began, a
    request admitted earlier in this same wait reads as somebody else's. The
    gap is real and this pass opens it itself: the intake read is taken first,
    the re-post goes out, and only then is the row moved -- so a gate that
    admits that re-post in between writes a row that sits behind
    `updated_at` and in front of nothing. Every later tick would then re-post
    on top of a review that is genuinely running, and go on doing so until the
    budget escalates a pull request under active review.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    started = deps.store.get(loop_id).updated_at
    deps.source.admitted = False
    deps.now.advance(10 * 60)
    assert tick(deps) == 1, "the gate had nothing at the read, so this re-posts"
    moved = deps.store.get(loop_id).updated_at
    assert moved > started, "and the row moves, which is the whole hazard"

    # The gate took that re-post, in the gap between the read and the swap.
    deps.source.admitted_at = moved - 0.5
    for _ in range(3):
        deps.now.advance(10 * 60)
        assert tick(deps) == 0
    assert len(deps.slack.reposts) == 2, \
        "the original and one re-post: the review on record is this wait's own"
    assert deps.slack.escalations == []


def test_an_unreadable_intake_is_bounded_by_the_wait_not_the_last_move(tmp_path):
    """The same anchor, and the same slide, on the bound that is supposed to be
    absolute. Measured from the row's last move it is re-armed by every landed
    re-post -- a full budget's worth of drift, and a wedged review then never
    escalates at four hours at all.

    An unreadable intake is where that is still observable, and it is also the
    branch that most needs it: it has no retry budget, it can repeat for as long
    as the fault lasts, and with no admission time to anchor to, the wait's own
    start is the only bound it has. Asked of an *admitted* request the question
    no longer has two answers -- a re-post that lands is admitted at the moment
    it lands, so the row's last move and that admission are the same instant, and
    the anchor that would have to be wrong (`loop.updated_at`) coincides with the
    one that is right. Which is the whole reason the deadline moved off the wait
    for admitted requests: see the test below.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    started = deps.store.get(loop_id).updated_at
    deps.source.admitted = False
    spend_reposts(deps, loop_id, 3)
    assert deps.store.get(loop_id).updated_at == started + 30 * 60, \
        "half an hour of re-posting has dragged the row's last move with it"

    # And now boris_intake will not say whether any of them got in. Nothing here
    # can be retried into an answer and no re-post is owed, so the deadline is
    # the only thing left that can end this wait.
    deps.source.admitted = None
    deps.now.advance(4 * 60 * 60 + 60 - 30 * 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")], \
        "four hours after the wait began, not after its last re-post"


def test_an_admission_late_in_a_wait_gets_the_whole_deadline_from_there(tmp_path):
    """The live scenario, and the defect this anchor exists to fix.

    A loop stranded in `rereview_posted` is *hours* old before anything looks at
    it -- that is what this whole guard was written to rescue, and every loop it
    rescued on its first day was in exactly that state. So the re-review that
    finally gets in gets in late: Chad re-kicks the pull request by hand at 3h50m
    and boris_intake admits it there.

    Anchored at the wait, that review had ten minutes before this guard escalated
    it as REREVIEW_VERDICT_OVERDUE -- a reason that sends a human to read that
    request's own row, where they would find a review that started moments ago and
    is running perfectly. `forge-content-service#111` dodged it only by accident:
    the manual kick landed on a different head, because commits had been pushed
    in the meantime, so it flowed through the verdict path instead of this one.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.now.advance(3 * 60 * 60 + 50 * 60)  # parked, and nothing looked at it

    # Re-kicked by hand, and this time the gate takes it.
    deps.source.admitted_at = deps.now()
    assert tick(deps) == 0, "the request is in; the review has not begun"

    deps.now.advance(11 * 60)
    assert tick(deps) == 0
    assert deps.slack.escalations == [], \
        "the wait is four hours old and the review it waits on is eleven minutes"

    deps.now.advance(4 * 60 * 60 - 12 * 60)
    assert tick(deps) == 0
    assert deps.slack.escalations == [], \
        "a minute short of four hours from the admission, and still its own"

    deps.now.advance(2 * 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")], \
        "and then it has had its four hours and produced nothing"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


def test_an_admission_dated_in_the_future_buys_no_extra_deadline(tmp_path):
    """`created_at` is another process's epoch column, and this loop compares it
    against its own clock.

    A row from the future -- clock skew, a manual repair, a bad migration -- would
    push this bound out by exactly that skew if the anchor trusted it, and one
    such row would then disable the backstop for the life of the loop: the very
    silent park the guard exists to end, reinstated by a single bad timestamp. A
    timestamp that has not happened yet evidences nothing about how long a review
    has been running, so it buys no window and the wait keeps the bound it would
    have had with no admission time at all.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted_at = deps.now() + 30 * 60 * 60  # dated tomorrow
    deps.now.advance(4 * 60 * 60 + 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")], \
        "four hours after the wait began, as if the row carried no time at all"


def test_an_admission_one_second_in_the_future_buys_no_extra_deadline(tmp_path):
    """The extreme case above (a row dated a day out) cannot see a mutant that
    grants an hour of skew tolerance before treating a row as future: a day is
    so far past any such tolerance that both the real clamp and a leaky
    one-hour one reject it alike. The boundary is what pins the hour: a row
    dated a single second past the instant of the check must still buy
    nothing, because the clamp compares against `deps.now()` as read, not
    against a tolerance band around it.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    started = deps.store.get(loop_id).updated_at
    deps.source.admitted_at = started + 4 * 60 * 60 + 1  # one second past the
                                                          # would-be deadline
    deps.now.advance(4 * 60 * 60)  # now == started + 4h, exactly
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")], \
        "one second into the future must buy not one second of extra " \
        "deadline, let alone an hour of it"


def test_an_admission_at_this_very_instant_counts_as_admitted_here_and_now(tmp_path):
    """The clamp's own equality case, and the same argument the query's own
    inclusive `>=` already makes: a timestamp that IS now evidences an
    admission that has already happened, not one still to come.

    Parked long enough that an anchor stuck at the episode start would already
    be overdue, this drives the admission to the exact instant of the check --
    `admitted_at == deps.now()` -- so a `<` in place of the clamp's `<=`
    rejects it as though it were still in the future, falls back to the
    episode start, and escalates a review that in fact just began.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.now.advance(4 * 60 * 60 + 60)  # already past an episode-start deadline
    deps.source.admitted_at = deps.now()  # admitted at this very instant
    assert tick(deps) == 0
    assert deps.slack.escalations == [], \
        "admitted exactly now, with the whole four hours still to run"


def test_the_admitted_deadline_does_not_borrow_the_rows_last_move(tmp_path):
    """The floor's other operand matters even though the branch that reaches
    it cannot fire in production (see the comment on `anchor` in
    `_route_rereview_ack`): `loop.updated_at` drifts forward on every landed
    re-post the way `episode.started_at` does not, so a floor written against
    the row's last move instead of the wait's own start would extend a
    genuinely late admission's deadline by however much re-posting happened
    first. `test_an_unreadable_intake_is_bounded_by_the_wait_not_the_last_move`
    pins that same row-drift hazard on the UNREADABLE branch; this pins it on
    the branch that actually reaches this line.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    started = deps.store.get(loop_id).updated_at
    deps.source.admitted = False
    spend_reposts(deps, loop_id, 3)
    assert deps.store.get(loop_id).updated_at == started + 30 * 60, \
        "half an hour of re-posting has dragged the row's last move with it"

    # The gate actually took the very first post, moments into the wait --
    # long before any of that re-posting happened.
    deps.source.admitted_at = started + 1
    deps.now.advance(4 * 60 * 60 + 60 - 30 * 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_verdict_overdue")], \
        "four hours after the admission, not after the row's last move"


def test_a_wait_with_no_opening_transition_claims_the_less_of_the_two(tmp_path):
    """A row whose trail cannot say how its wait began -- hand-edited, or older
    than this trail -- still gets a re-send, and it must be the message that
    claims only what can still be shown. `repost_rereview` says a request is
    being sent again and nothing else; `post_rereview` would announce that a
    remediation lane ran and pushed, which is exactly the fact nothing here
    evidences. Defaulting to the louder of the two would put a false sentence in
    the review thread on precisely the rows nobody can reconstruct.
    """
    import sqlite3
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    with sqlite3.connect(tmp_path / "r.db") as db:
        db.execute("DELETE FROM loop_events WHERE loop_id=? AND kind='transition'",
                   (loop_id,))
        db.commit()
    deps.source.admitted = False
    deps.now.advance(10 * 60)

    assert tick(deps) == 1
    assert deps.slack.repost_retries == ["Ylopo/example#42"], \
        "re-sent as the only thing the trail can still support"
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "the lane's own post, and no second claim of a push"


def test_the_repost_claims_the_push_the_lane_really_made(tmp_path):
    """`post_rereview`, not `repost_rereview`. A remediation lane genuinely
    ran and genuinely pushed, so "Round 1 -- remediation pushed" is a true
    claim and the round number is one there is to report. `repost_rereview`
    says the opposite ("no remediation round was spent"), which would leave
    a human scanning the thread unable to tell what actually happened."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    assert deps.slack.reposts[-1] == ("Ylopo/example#42", 1), \
        "the same round, re-sent -- not the next one"
    assert deps.slack.repost_retries == [], \
        "repost_rereview would deny the push the lane really made"
    assert deps.slack.repost_targets[-1] == "1.0", \
        "the review thread, not the reaction anchor"


def test_the_repost_rearms_the_window_rather_than_firing_every_tick(tmp_path):
    """The self-edge is what makes this a bounded retry instead of a flood:
    `updated_at` moves, and the window is measured from it."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    assert len(deps.slack.reposts) == 2

    for _ in range(5):
        deps.now.advance(60)
        assert tick(deps) == 0
    assert len(deps.slack.reposts) == 2, \
        "a re-post every tick would flood the review thread"

    deps.now.advance(5 * 60)
    assert tick(deps) == 1
    assert len(deps.slack.reposts) == 3


def test_a_failed_repost_leaves_the_loop_where_it_is_and_tries_again(tmp_path):
    """The same ordering `_lane_pushed` uses, for the same reason: post
    first, move the row second. A row moved ahead of a post that never
    landed would re-arm the window for a request nobody received, and the
    row would say a request is outstanding when none is."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.slack.post_rereview_fails = True
    loop_id = only_loop(deps).loop_id
    before = deps.store.get(loop_id)
    deps.now.advance(10 * 60)

    assert tick(deps) == 0, "a post that never landed advanced nothing"
    after = deps.store.get(loop_id)
    assert after.state is LoopState.REREVIEW_POSTED
    assert after.updated_at == before.updated_at, "the window must not re-arm"
    assert reposts(deps, loop_id) == [], "nothing landed, so nothing is recorded landed"
    assert repost_attempts(deps, loop_id) == [("rereview_repost_failed", 1)], \
        "but the attempt is on record, individually and by number"

    deps.slack.post_rereview_fails = False
    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    # Three calls: the original, the one Slack refused, and the retry that
    # landed. The refused one already spent attempt 1, so this is attempt 2.
    assert len(deps.slack.reposts) == 3
    assert repost_attempts(deps, loop_id) == [("rereview_repost_failed", 1),
                                              ("rereview_reposted", 2)]


def test_a_rereview_slack_will_never_take_still_escalates(tmp_path):
    """A route that decides and performs nothing, indefinitely, with no
    record, is the hazard this whole task exists to remove -- and counting
    only landed posts rebuilt it inside the fix. With `post_rereview` refused
    forever the budget never advanced, so the loop tried once every fifteen
    seconds until someone noticed, which nobody would: `_note_standing`'s
    dedup left exactly one sentence in the trail for all of it.

    An attempt is an attempt, landed or refused. Escalation is then reachable
    on every path out of this state.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.slack.post_rereview_fails = True
    loop_id = only_loop(deps).loop_id

    for _ in range(40):
        deps.now.advance(10 * 60)
        tick(deps)
        if deps.store.get(loop_id).state is LoopState.ESCALATED:
            break

    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_unacknowledged")]
    assert repost_attempts(deps, loop_id) == [
        ("rereview_repost_failed", n) for n in range(1, 13)], \
        "twelve refusals, each on record by number -- and no thirteenth"


def test_a_refused_repost_waits_a_window_rather_than_retrying_every_tick(tmp_path):
    """Retrying a refused post on the very next tick is four attempts a
    minute at a Slack that has just said no, and the trail records one of
    them. The same window a landed post re-arms is the right wait here too:
    six attempts an hour, each one recorded."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.slack.post_rereview_fails = True
    deps.now.advance(10 * 60)
    assert tick(deps) == 0
    assert len(deps.slack.reposts) == 2, "the original, and the one Slack refused"

    for _ in range(19):
        deps.now.advance(30.0)  # the daemon's own tick interval
        assert tick(deps) == 0
    assert len(deps.slack.reposts) == 2, \
        "nineteen ticks inside the window must cost Slack nothing"

    deps.now.advance(30.0)
    tick(deps)
    assert len(deps.slack.reposts) == 3, "and the window ending must end the wait"


def test_a_rereview_that_never_lands_escalates_after_twelve_attempts(tmp_path):
    """Escalation is absorbing, so it is deliberately slow: twelve attempts
    at ten minutes is about two hours, longer than a typical GitHub incident.
    Escalating earlier permanently strands a pull request that would have
    recovered on its own; never escalating leaves the loop non-terminal, and
    `active_for_pr` then blocks every later generation of that PR behind a
    human nobody told.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    loop_id = only_loop(deps).loop_id

    for _ in range(20):
        deps.now.advance(10 * 60)
        tick(deps)
        if deps.store.get(loop_id).state is LoopState.ESCALATED:
            break

    assert len(reposts(deps, loop_id)) == 12
    assert [d["attempt"] for d in reposts(deps, loop_id)] == list(range(1, 13))
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 1, "rereview_unacknowledged")]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)], \
        "no lane is running here, so the ownership label goes back"


def spend_reposts(deps, loop_id, count):
    """Burn `count` landed re-post attempts, one per grace window."""
    from pr_remediation.loop import tick

    for _ in range(count):
        deps.now.advance(10 * 60)
        tick(deps)
    assert len(reposts(deps, loop_id)) == count


def test_the_repost_budget_belongs_to_the_wait_not_the_whole_loop(tmp_path):
    """A later round must not be charged for an earlier round's retries.

    A head that moves ends the *wait* -- moving it takes a lane, a lane takes a
    round, and a round leaves `rereview_posted` -- but it does not clear the
    *count*. Summed over the whole loop, a round that spent eleven re-posts
    riding out a GitHub incident leaves the next round one attempt and a
    twenty-minute fuse; twelve leaves it none at all. Escalation is absorbing,
    so a single transient `gate_snapshot_unavailable` then strands the pull
    request for good -- exactly the outcome the budget exists to prevent.
    """
    from pr_remediation.loop import LANE_POLL_SECONDS, tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.source.admitted = False
    spend_reposts(deps, loop_id, 11)

    # A verdict arrives after all, and its round runs, pushes, and asks for a
    # re-review of its own: a second request outstanding, and a second wait.
    deps.source.admitted = True
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40)]
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING
    deps.source.verdicts = []
    deps.now.advance(LANE_POLL_SECONDS + 1)
    deps.github.head = "c" * 40  # round two pushed
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED

    # ...and the gate rejects this one too. The new wait gets the whole budget.
    deps.source.admitted = False
    for _ in range(20):
        deps.now.advance(10 * 60)
        tick(deps)
        if deps.store.get(loop_id).state is LoopState.ESCALATED:
            break
    numbered = [d["attempt"] for d in reposts(deps, loop_id)]
    assert numbered[11:] == list(range(1, 13)), \
        "the second wait counts from one and gets all twelve attempts"
    assert deps.slack.escalations == [
        ("Ylopo/example#42", 2, "rereview_unacknowledged")]


def test_a_repost_does_not_reset_the_budget_it_is_spending(tmp_path):
    """The other half of the same rule, and the direction that matters more:
    the self-edge a re-post takes must NOT open a new wait. The same request is
    still the one outstanding -- it was merely sent again -- so resetting there
    would zero the budget on every attempt and put escalation permanently out
    of reach, which is the silent park this whole route exists to end.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.source.admitted = False
    spend_reposts(deps, loop_id, 3)
    assert [d["attempt"] for d in reposts(deps, loop_id)] == [1, 2, 3], \
        "three re-posts of one request, numbered consecutively"


# --- the other way into `rereview_posted`, where the guard was inert --------
#
# `_repost` (a system-origin REVIEW_INCOMPLETE) transitions with no `head_sha`,
# so `loop.head_sha` stays the head of the verdict that opened the loop -- and
# a `review_requests` row provably exists at that head, because it is the
# request that produced that verdict. A guard that asks "does a request exist
# at this head" therefore reads True forever on this route and never fires.


def drive_to_a_repost(tmp_path):
    """A loop parked in `rereview_posted` by `_repost`, not by a lane.

    Nothing was pushed, no round was spent, and `head_sha` never moved -- so
    the request that produced this very verdict is still sitting at exactly the
    head the acknowledgement check asks about.
    """
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake(
        [verdict(outcome="REVIEW_INCOMPLETE", origin="system")]))
    # boris_intake admitted that request one second before this tick.
    deps.source.admitted_at = deps.now() - 1
    assert tick(deps) == 1
    loop = only_loop(deps)
    assert loop.state is LoopState.REREVIEW_POSTED
    assert loop.head_sha == "a" * 40, "this route moves no head"
    assert loop.round_count == 0, "and spends no round"
    assert deps.slack.repost_retries == ["Ylopo/example#42"]
    deps.source.verdicts = []
    return deps


def test_a_repost_the_gate_never_admitted_is_sent_again(tmp_path):
    """The strand this task was meant to close, through the door the guard
    itself held open. The stale request at the loop's head is not this
    episode's, and asking "admitted since this wait began" is the only way to
    say so -- there is no second head here to tell them apart with."""
    from pr_remediation.loop import tick

    deps = drive_to_a_repost(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.now.advance(10 * 60)

    assert tick(deps) == 1
    assert reposts(deps, loop_id) == [{"attempt": 1}]
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED


def test_a_repost_resent_says_no_round_was_spent(tmp_path):
    """`repost_rereview`, because that is how this episode began. Nothing was
    pushed and no round was spent, so `post_rereview`'s "Round N of M --
    remediation pushed" would announce work that never happened, under a
    `Round 0` that names nothing. slack_io.py keeps the two methods apart
    precisely so a human scanning the thread can tell which occurred."""
    from pr_remediation.loop import tick

    deps = drive_to_a_repost(tmp_path)
    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    assert deps.slack.repost_retries == ["Ylopo/example#42"] * 2, \
        "the same message, re-sent"
    assert deps.slack.reposts == [], \
        "post_rereview would claim a push and a round that never happened"
    assert deps.slack.repost_retry_targets[-1] == "1.0", \
        "the review thread, not the reaction anchor"


def test_a_lane_pushed_episode_still_claims_its_push(tmp_path):
    """The other selection, pinned from the other side: an episode a lane
    opened re-sends with `post_rereview`, because there the push and the round
    are both real. Swapping the two selections must fail in both directions."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    assert deps.slack.reposts[-1] == ("Ylopo/example#42", 1)
    assert deps.slack.repost_retries == []


def test_an_admitted_repost_is_left_alone(tmp_path):
    """The windowed read must not turn every `_repost` wait into a re-post:
    a request admitted *after* the wait began is this episode's, and the loop
    owes nothing."""
    from pr_remediation.loop import tick

    deps = drive_to_a_repost(tmp_path)
    # `catch_up` replayed the message and the gate took it, three minutes in.
    deps.source.admitted_at = deps.now() + 3 * 60
    deps.now.advance(3 * 60 * 60)
    assert tick(deps) == 0
    assert len(deps.slack.repost_retries) == 1, "the original post and no other"
    assert only_loop(deps).state is LoopState.REREVIEW_POSTED


# --- and the same route arriving at a loop already waiting -----------------
#
# `_repost` is not only a way *into* `rereview_posted`: a system-origin
# REVIEW_INCOMPLETE arriving at a loop already parked there takes the self-edge,
# and the transition it writes is byte-identical to the one the guard's own
# re-send writes. Read as a continuation, the wait stays pinned to the request
# whose verdict prompted the re-send -- which is admitted by construction -- so
# `request_exists` answers True forever and the guard never fires once. Live
# boris_intake: 14 of 71 completed reviews are system-origin incompletes, so
# this is a fifth of the traffic, not a corner.


def drive_to_a_repost_inside_a_wait(tmp_path):
    """A lane's wait interrupted by a system-origin incomplete.

    Round one ran, pushed, and had its re-review admitted; the review came back
    REVIEW_INCOMPLETE with a system origin, so `_repost` sends a fresh request
    into a loop that is already in `rereview_posted`. Nothing about the head
    can tell the two waits apart -- this route moves no head -- and the request
    that produced this very verdict is sitting at it.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    started = deps.store.get(loop_id).updated_at
    deps.source.admitted_at = started + 1  # the gate took the lane's request
    deps.now.advance(20 * 60)
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40,
                                    outcome="REVIEW_INCOMPLETE",
                                    origin="system")]
    assert tick(deps) == 1
    loop = deps.store.get(loop_id)
    assert loop.state is LoopState.REREVIEW_POSTED, "the self-edge"
    assert loop.round_count == 1, "and no round spent"
    assert loop.head_sha == "b" * 40, "and no head moved"
    assert deps.slack.repost_retries == ["Ylopo/example#42"]
    deps.source.verdicts = []
    return deps


def test_a_repost_into_a_waiting_loop_starts_a_new_wait(tmp_path):
    """The request `_repost` just sent is the one outstanding now, and the
    admitted one it replaced answers for nothing. Asked about the old wait,
    boris_intake says "already admitted" -- from the very request whose verdict
    this re-send is reacting to -- and the guard goes inert on the route that
    produces a fifth of live verdicts, which is the strand this whole guard
    exists to end, rebuilt one door along."""
    from pr_remediation.loop import tick

    deps = drive_to_a_repost_inside_a_wait(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.now.advance(10 * 60)

    assert tick(deps) == 1
    assert reposts(deps, loop_id) == [{"attempt": 1}]
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED


def test_a_repost_into_a_waiting_loop_resends_its_own_message(tmp_path):
    """And it re-sends as what it is. The wait now open was opened by
    `_repost`, which pushed nothing and spent no round, so `post_rereview`'s
    "Round 1 of 3 -- remediation pushed" would claim the earlier round's work
    for a request that has nothing to do with it."""
    from pr_remediation.loop import tick

    deps = drive_to_a_repost_inside_a_wait(tmp_path)
    deps.now.advance(10 * 60)

    assert tick(deps) == 1
    assert deps.slack.repost_retries == ["Ylopo/example#42"] * 2, \
        "the message this wait can truthfully carry, re-sent"
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "the lane's own post, and no second claim of a push"


def test_a_repost_into_a_waiting_loop_brings_its_own_budget(tmp_path):
    """A new request outstanding is a new thing to be patient about. Inheriting
    the interrupted wait's count would hand this one whatever the last one left
    -- nothing at all, if that one had spent its twelve -- and escalation is
    absorbing, so the pull request would be stranded on the first blip."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    started = deps.store.get(loop_id).updated_at
    deps.source.admitted = False
    spend_reposts(deps, loop_id, 11)

    # The gate took one of those after all, and the review it produced came
    # back a system-origin incomplete: a fresh request goes out.
    deps.source.admitted_at = started + 1
    deps.source.verdicts = [verdict(rid="br_2", head="b" * 40,
                                    outcome="REVIEW_INCOMPLETE",
                                    origin="system")]
    assert tick(deps) == 1
    deps.source.verdicts = []

    # ...and the gate rejects that one. The new wait gets the whole budget.
    deps.source.admitted_at = None
    deps.source.admitted = False
    for _ in range(20):
        deps.now.advance(10 * 60)
        tick(deps)
        if deps.store.get(loop_id).state is LoopState.ESCALATED:
            break
    numbered = [d["attempt"] for d in reposts(deps, loop_id)]
    assert numbered[11:] == list(range(1, 13)), \
        "the second wait counts from one and gets all twelve attempts"


def test_a_repost_whose_swap_is_lost_still_names_the_request_it_sent(tmp_path):
    """The send is the fact; the swap is only the record of it.

    `_repost` posts and then moves the row, and that move can be lost -- our
    lease lapsed, or another tick took the row over. The request is out there
    either way, so a marker written only once the swap landed would leave the
    guard waiting on the request this one replaced: admitted by construction,
    so the intake answers "already admitted" forever and the loop parks
    silently, which is the whole failure this marker exists to prevent.
    """
    from pr_remediation.loop import LoopDeps, tick

    deps = drive_to_rereview_posted(tmp_path)
    loop_id = only_loop(deps).loop_id
    deps.source.admitted_at = deps.store.get(loop_id).updated_at + 1
    losing = LoopDeps(**{**deps.__dict__,
                         "store": LosesTheRepostSelfEdge(deps.store)})

    losing.now.advance(20 * 60)
    losing.source.verdicts = [verdict(rid="br_2", head="b" * 40,
                                      outcome="REVIEW_INCOMPLETE",
                                      origin="system")]
    assert tick(losing) == 0, "the swap was lost, so the row advanced nothing"
    assert losing.slack.repost_retries == ["Ylopo/example#42"], \
        "but the request went out, and it is the one outstanding now"
    losing.source.verdicts = []

    losing.now.advance(10 * 60)
    assert tick(losing) == 1
    assert losing.slack.repost_retries == ["Ylopo/example#42"] * 2, \
        "so the guard waits on it, not on the request it replaced"


def test_an_escalation_already_owed_outranks_the_repost(tmp_path):
    """Ordering, and not an arbitrary one: a stopped loop's escalation debt
    is a write already owed to a human, and it must still take precedence
    over a request this pass would otherwise re-send into a loop that has
    already given up."""
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    loop_id = only_loop(deps).loop_id
    deps.store.record_note(loop_id, "escalation_pending",
                           {"reason": "no_op_lane"}, deps.now())

    deps.now.advance(10 * 60)
    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, "no_op_lane")]
    assert deps.slack.reposts == [("Ylopo/example#42", 1)], \
        "the debt is settled, not overwritten with another request"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_tick_records_the_repost_and_sends_nothing(tmp_path, mode):
    """The kill switch, structurally: routing decides, `_consumer` picks who
    consumes, and `_rehearse` never reaches `_perform`'s branch.

    `shadow` reaches the decision itself, because rehearsing a decision taken
    without the facts would not be rehearsing the decision a live tick takes.
    `off` does not, and that is not a fourth mode gate gone missing either: the
    branch this route would take is the one that writes, so it reads GitHub
    first (a hold, and whether the pull request is still open), and `off` stops
    the *work* as well as the writes. What it records is what `_route` records
    for the same reason -- an unobserved tick, never a decision it did not take.
    Nothing is posted in either mode.

    Ticked twice, because a pause is not one tick: this decision is retaken
    unchanged on every one of them, and a route that forgot to mark itself
    `standing` would append the same sentence to the trail four times a minute
    for as long as the service stayed paused -- to a trail every later tick
    reads back in full.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    loop_id = only_loop(deps).loop_id
    deps.now.advance(10 * 60)

    paused = in_mode(deps, mode)
    assert tick(paused) == 0
    paused.now.advance(30.0)
    assert tick(paused) == 0
    assert len(paused.slack.reposts) == 1, "the original post and no other"
    assert paused.store.get(loop_id).state is LoopState.REREVIEW_POSTED
    assert reposts(paused, loop_id) == [], "a pause spends none of the budget"
    decisions = [e["detail"] for e in paused.store.events(loop_id)
                 if e["kind"] == "shadow_decision"]
    expected = ({"would": "rereview_unacknowledged", "attempt": 1,
                 "origin": "lane_pushed"} if mode == "shadow"
                else {"would": "unobserved", "mode": "off"})
    assert decisions == [expected], \
        "one sentence for a decision that is retaken every tick"


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_tick_at_the_attempt_bound_records_the_escalation_once(tmp_path, mode):
    """The other new route, under the same rule and pinned the same way.

    A loop that has spent its budget re-decides `escalate` on every tick until
    something settles it, and in a paused mode nothing ever will -- so this is
    the route where a missing `standing` costs the most.

    `off` stops short of the decision, as it does for the re-post above and for
    the same reason: this branch reads GitHub before it writes, and the hard
    kill switch reads nothing.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)
    deps.source.admitted = False
    loop_id = only_loop(deps).loop_id
    spend_reposts(deps, loop_id, 12)
    assert deps.store.get(loop_id).state is LoopState.REREVIEW_POSTED, \
        "the budget is spent but nothing has escalated yet"

    paused = in_mode(deps, mode)
    paused.now.advance(10 * 60)
    assert tick(paused) == 0
    paused.now.advance(30.0)
    assert tick(paused) == 0
    assert paused.slack.escalations == [], "a pause escalates nothing"
    assert paused.store.get(loop_id).state is LoopState.REREVIEW_POSTED
    decisions = [e["detail"] for e in paused.store.events(loop_id)
                 if e["kind"] == "shadow_decision"]
    assert decisions == [{"would": "escalate",
                          "reason": "rereview_unacknowledged"}
                         if mode == "shadow"
                         else {"would": "unobserved", "mode": "off"}]


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_a_paused_tick_past_the_deadline_records_the_escalation_once(tmp_path, mode):
    """The third route out of this state, under the same rule again.

    Nothing settles a decision in a paused mode, so this one is retaken on
    every tick for as long as the pause lasts -- four a minute, appended to a
    trail every later tick reads back in full, which is the growth this
    module's standing-note dedup exists to prevent. The two routes above were
    pinned; this one was reached only by a test that ticked once, where a
    missing `standing` looks exactly like a present one.
    """
    from pr_remediation.loop import tick

    deps = drive_to_rereview_posted(tmp_path)  # admitted, and it stays admitted
    loop_id = only_loop(deps).loop_id
    deps.now.advance(4 * 60 * 60 + 60)

    paused = in_mode(deps, mode)
    assert tick(paused) == 0
    paused.now.advance(30.0)
    assert tick(paused) == 0
    assert paused.slack.escalations == [], "a pause escalates nothing"
    assert paused.store.get(loop_id).state is LoopState.REREVIEW_POSTED
    decisions = [e["detail"] for e in paused.store.events(loop_id)
                 if e["kind"] == "shadow_decision"]
    assert decisions == [{"would": "escalate",
                          "reason": "rereview_verdict_overdue"}]
