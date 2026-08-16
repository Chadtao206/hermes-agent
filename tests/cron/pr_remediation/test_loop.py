"""The driver: verdict routing, round accounting, and the claim discipline.

Every fake here is a fake -- no GitHub, no Slack, no kanban, no network. The
store is the real one against a tmp_path database, because the lease and
compare-and-swap semantics the driver depends on are exactly what these tests
are checking.
"""
import pytest

from pr_remediation.boris_source import Verdict
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
    def __init__(self, verdicts=()):
        self.verdicts = list(verdicts)

    def new_verdicts(self, limit=50):
        return tuple(self.verdicts)


class GitHubFake:
    """claim/release return bool, as GitHubIO now does."""

    def __init__(self, head="a" * 40, author="Chadtao206", hold=False,
                 claim_ok=True, release_ok=True, hold_raises=(),
                 pr_state="OPEN"):
        self.head, self.author_login, self.hold = head, author, hold
        self.claim_ok, self.release_ok = claim_ok, release_ok
        self.hold_raises = set(hold_raises)
        self.claimed, self.released = [], []
        # What `gh pr view --json state` reports: OPEN / MERGED / CLOSED, or
        # None when GitHub could not be read.
        self.pr_state = pr_state

    def head_sha(self, o, r, p):
        return self.head

    def author(self, o, r, p):
        return self.author_login

    def state(self, o, r, p):
        return self.pr_state

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
        self._repost_ts = repost_ts
        self._ready_ts = ready_ts
        self._escalation_ts = escalation_ts

    def post_rereview(self, verdict, thread_ts, round_number):
        self.reposts.append((verdict.slug, round_number))
        self.repost_targets.append(thread_ts)
        return self._repost_ts

    def repost_rereview(self, verdict, thread_ts):
        self.repost_retries.append(verdict.slug)
        self.repost_retry_targets.append(thread_ts)
        return self._repost_ts

    def add_thinkies(self, channel, ts):
        self.thinkies_added.append(ts)

    def remove_thinkies(self, channel, ts):
        self.thinkies_removed.append(ts)

    def notify_ready(self, verdict, rounds):
        self.ready.append((verdict.slug, rounds))
        return self._ready_ts

    def notify_escalation(self, verdict, rounds, reason):
        self.escalations.append((verdict.slug, rounds, reason))
        return self._escalation_ts


class LanesFake:
    def __init__(self, status="in_progress", lane_id=True):
        self.created, self._status, self._lane_id = [], status, lane_id

    def create(self, verdict, round_number):
        self.created.append((verdict.slug, round_number))
        if self._lane_id is None:
            return None
        return f"t_{len(self.created)}"

    def status(self, lane_id):
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
                   ("Null deref",), status)


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
    loop's own persisted anchor from when it opened, not whatever a later
    verdict happens to carry."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict(status="2.0")]))
    tick(deps)
    assert deps.slack.thinkies_added == ["2.0"]

    deps.source.verdicts = [verdict(rid="br_2", status="9.5",
                                    outcome="READY_FOR_HUMAN_REVIEW")]
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
    from pr_remediation.loop import tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1")]), slack=slack)
    tick(deps)
    loop_id = _loop_id(deps)
    assert kinds(deps, loop_id).count("escalation_pending") == 1

    slack._escalation_ts = "9.2"  # Slack is back. The service is not.
    paused = in_mode(deps, mode)
    paused.source.verdicts = [verdict(rid="br_2")]
    paused.now.advance()
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
    live.now.advance()
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
    from pr_remediation.loop import tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete(rid="br_1")]), slack=slack)
    assert tick(deps) == 0, "a live tick that owes a human a notice"
    loop_id = _loop_id(deps)
    assert kinds(deps, loop_id).count("escalation_pending") == 1

    slack._escalation_ts = "9.2"  # Slack is back. The service is not.
    paused = in_mode(deps, mode)
    paused.source.verdicts = []  # nothing to route: the due pass is the tick
    paused.now.advance()
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
    """A GitHub that remembers being asked. `off` must ask it nothing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reads = []

    def head_sha(self, o, r, p):
        self.reads.append(("head_sha", p))
        return super().head_sha(o, r, p)

    def author(self, o, r, p):
        self.reads.append(("author", p))
        return super().author(o, r, p)

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
    assert github.reads == [("has_hold", 42), ("author", 42)], \
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
    from pr_remediation.loop import tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    assert len(slack.escalations) == 1

    deps.source.verdicts = []
    slack._escalation_ts = "9.2"
    deps.now.advance()
    assert tick(deps) == 1
    assert len(slack.escalations) == 2
    assert deps.lanes.created == [], "a retry is a post, not a round"
    loop = deps.store.get(_loop_id(deps))
    assert loop.state is LoopState.ESCALATED
    assert loop.round_count == 0
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_a_delivered_escalation_is_never_notified_twice(tmp_path):
    from pr_remediation.loop import tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    deps.source.verdicts = []
    slack._escalation_ts = "9.2"
    deps.now.advance()
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
    from pr_remediation.loop import NOTIFY_ATTEMPTS, tick

    slack = SlackFake(escalation_ts=None)
    deps = build(tmp_path, SourceFake([model_incomplete()]), slack=slack)
    tick(deps)
    deps.source.verdicts = []
    for _ in range(NOTIFY_ATTEMPTS):
        deps.now.advance()
        tick(deps)
    assert len(slack.escalations) == NOTIFY_ATTEMPTS
    loop_id = _loop_id(deps)
    assert deps.store.get(loop_id).state is LoopState.ESCALATED, \
        "a loop held open forever blocks every later loop on this PR"
    assert "escalation_unreachable" in kinds(deps, loop_id)


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
    from pr_remediation.loop import tick

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
    deps.now.advance()
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
    if prior is LoopState.REMEDIATING and outcome == "ISSUES_FOUND":
        # The one cell where the driver deliberately takes no edge at all: the
        # lane opened by the first tick is still in flight.
        assert tick(deps) == 0
        assert store.get(loop_id).state is LoopState.REMEDIATING
        return
    assert tick(deps) == 1
    assert store.get(loop_id).state is target


def test_a_remediating_loop_with_no_lane_takes_the_remediating_edge(tmp_path):
    """The cell the matrix above now skips, and the only path that still
    reaches `remediating -> remediating`: a row in that state with no lane to
    poll (a hand-edit, or a round an older build's shadow mode wrote before a
    paused tick stopped writing state) has nothing in flight, so a verdict
    starts a real round from there."""
    from pr_remediation.loop import tick

    store = Store(tmp_path / "r.db")
    deps = build(tmp_path, SourceFake([verdict(rid="br_0")]), store=store)
    tick(deps)
    loop_id = _loop_id(deps)
    store._db.execute(
        "UPDATE loops SET state=?, lane_id=NULL, round_count=0 WHERE loop_id=?",
        (LoopState.REMEDIATING.value, loop_id))
    store._db.commit()

    deps.source.verdicts = [verdict(rid="br_1")]
    deps.now.advance()
    assert tick(deps) == 1
    loop = store.get(loop_id)
    assert loop.state is LoopState.REMEDIATING
    assert loop.round_count == 1
    assert len(deps.lanes.created) == 2


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
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    assert tick(deps) == 1
    loop = only_loop(deps)
    assert loop.state is LoopState.AWAITING_HEAD_MOVE
    assert deps.slack.escalations == [], "a push can land just behind its lane"
    assert deps.slack.reposts == []
    assert deps.slack.thinkies_removed == ["2.0"], "no round is running now"

    deps.now.advance(HEAD_MOVE_GRACE_SECONDS + 1)
    assert tick(deps) == 1
    assert deps.slack.escalations[0][2] == "no_op_lane"
    assert deps.slack.reposts == [], "an unchanged PR must never be re-reviewed"
    assert only_loop(deps) is None, "an escalated loop is finished"
    assert deps.github.released == [("Ylopo", "example", 42)]


def test_the_grace_window_catches_a_push_that_landed_late(tmp_path):
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"))
    assert tick(deps) == 1
    assert only_loop(deps).state is LoopState.AWAITING_HEAD_MOVE

    deps.github.head = "b" * 40  # GitHub was simply behind the lane
    deps.now.advance(HEAD_MOVE_GRACE_SECONDS + 1)
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

    assert HEAD_MOVE_GRACE_SECONDS > 1.0
    deps.now.advance(1.0)  # nowhere near the window's edge
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
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(3):
        deps.now.advance()
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
    deps.now.advance()
    assert tick(deps) == 1
    assert deps.slack.reposts == [("Ylopo/example#42", 1)]


def test_the_head_read_budget_exhausting_escalates_with_a_reason_distinct_from_no_op_lane(
        tmp_path):
    """A permanently unreadable head is not a no-op lane. `no_op_lane` means
    the read succeeded and the lane genuinely pushed nothing, which asks a
    human to accept the round as spent; this reason means the read itself
    never landed, which asks a human to go look at GitHub instead."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS):
        deps.now.advance()
        tick(deps)
    assert deps.slack.escalations == \
        [("Ylopo/example#42", 1, "head_read_exhausted")]
    assert only_loop(deps) is None, "an escalated loop is finished"
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)]
    assert deps.slack.thinkies_removed == ["2.0"]
    assert kinds(deps, loop_id).count("head_unreadable") == HEAD_READ_ATTEMPTS - 1, \
        "the exhausting attempt escalates instead of recording one more wait"


def test_a_head_read_escalation_retries_through_the_same_notify_budget(tmp_path):
    """head_read_exhausted is an ordinary escalation once it exists: a failed
    post leaves the loop non-terminal and owned, and retries through
    NOTIFY_ATTEMPTS exactly like every other reason -- there is no bespoke
    delivery path for this one to have its own bugs."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, tick

    slack = SlackFake(escalation_ts=None)
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None), slack=slack)
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS):
        deps.now.advance()
        tick(deps)
    assert deps.store.get(loop_id).state is not LoopState.ESCALATED, \
        "nobody has heard yet"
    assert "escalation_pending" in kinds(deps, loop_id)
    assert deps.github.released == [], "the PR stays owned until the notice lands"
    assert len(slack.escalations) == 1, "the exhausting tick spent its first attempt"

    slack._escalation_ts = "9.2"  # Slack recovers
    deps.now.advance()
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
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, LANE_POLL_SECONDS, tick

    github = GitHubFake(head=None)
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=github)
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS - 1):
        deps.now.advance()
        assert tick(deps) == 0
    assert deps.store.get(loop_id).state is LoopState.REMEDIATING, \
        "one tick short of round 1's own budget"

    github.head = "b" * 40  # round 1's push shows up
    deps.now.advance()
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
    from pr_remediation.loop import HEAD_MOVE_GRACE_SECONDS, HEAD_READ_ATTEMPTS, tick

    github = GitHubFake(head=None)
    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=github)
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS - 1):
        deps.now.advance()
        assert tick(deps) == 0
    assert kinds(deps, loop_id).count("head_unreadable") == HEAD_READ_ATTEMPTS - 1

    github.head = "a" * 40  # the read lands, unmoved: opens the grace window
    deps.now.advance()
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
    nothing, or resuming live would escalate sooner than the budget promises."""
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, tick

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
        live.now.advance()
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
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS - 1):
        deps.now.advance()
        assert tick(deps) == 0

    shadow = in_mode(deps, "shadow")
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
    assert github.reads == [("has_hold", 42), ("head_sha", 42)]


def test_a_lane_still_running_when_the_loop_closes_is_recorded(tmp_path):
    """A ready verdict arriving mid-round closes the loop, and terminal states
    are absorbing and unclaimable -- so that lane is polled by nobody, finishes,
    and pushes to a PR this service has just handed back."""
    from pr_remediation.loop import tick

    deps = drive_to_remediating(tmp_path)  # a lane still in progress
    loop_id = _loop_id(deps)
    deps.source.verdicts = [verdict(rid="br_2", outcome="READY_FOR_HUMAN_REVIEW")]
    assert tick(deps) == 1
    assert deps.store.get(loop_id).state is LoopState.READY
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
    from pr_remediation.loop import HEAD_READ_ATTEMPTS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="done"),
                                github=GitHubFake(head=None))
    loop_id = _loop_id(deps)
    for _ in range(HEAD_READ_ATTEMPTS):
        deps.now.advance()
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
    loop_id = [e for e in store._db.execute("SELECT loop_id, state, "
                                            "escalation_reason FROM loops")][0]
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
    """
    from pr_remediation.loop import LANE_TIMEOUT, LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status="running"))
    loop_id = only_loop(deps).loop_id

    deps.now.advance(LANE_WAIT_SECONDS)
    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert deps.store.get(loop_id).state is LoopState.ESCALATED
    assert deps.github.released == [("Ylopo", "example", 42)], \
        "the pull request must be handed back, not left looking owned"


def test_a_deleted_kanban_card_does_not_wedge_the_loop_forever(tmp_path):
    """`lane_status()` answers None for a card that is gone. Nothing else in
    this pass can ever move such a loop on: it is not finished, so no head is
    read, no grace window opens and no head-read budget is spent."""
    from pr_remediation.loop import LANE_TIMEOUT, LANE_WAIT_SECONDS, tick

    deps = drive_to_remediating(tmp_path, lanes=LanesFake(status=None))
    deps.now.advance(LANE_WAIT_SECONDS)

    assert tick(deps) == 1
    assert deps.slack.escalations == [("Ylopo/example#42", 1, LANE_TIMEOUT)]
    assert only_loop(deps) is None


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
