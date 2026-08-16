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
                 claim_ok=True, release_ok=True, hold_raises=()):
        self.head, self.author_login, self.hold = head, author, hold
        self.claim_ok, self.release_ok = claim_ok, release_ok
        self.hold_raises = set(hold_raises)
        self.claimed, self.released = [], []

    def head_sha(self, o, r, p):
        return self.head

    def author(self, o, r, p):
        return self.author_login

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
        self._repost_ts = repost_ts
        self._ready_ts = ready_ts
        self._escalation_ts = escalation_ts

    def post_rereview(self, verdict, thread_ts, round_number):
        self.reposts.append((verdict.slug, round_number))
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
            pr=42, thread="1.0"):
    return Verdict(rid, "Ylopo", "example", pr, head, thread, outcome, origin,
                   ("Null deref",))


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
    assert deps.slack.thinkies_removed == ["1.0"]


def test_thinkies_is_anchored_on_the_loop_thread_not_the_verdict(tmp_path):
    """A reaction can only be removed from the message it was added to."""
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]))
    tick(deps)
    assert deps.slack.thinkies_added == ["1.0"]

    deps.source.verdicts = [verdict(rid="br_2", thread="2.0",
                                    outcome="READY_FOR_HUMAN_REVIEW")]
    tick(deps)
    assert deps.slack.thinkies_removed == ["1.0"]


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
    assert deps.slack.reposts, "a transient incomplete should be retried"
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
    assert tick(deps) == 4
    assert deps.lanes.created == []
    assert deps.github.claimed == deps.github.released == []
    assert deps.slack.reposts == deps.slack.ready == deps.slack.escalations == []
    assert deps.slack.thinkies_added == deps.slack.thinkies_removed == []


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


def test_the_same_verdict_is_never_consumed_twice(tmp_path):
    from pr_remediation.loop import tick

    deps = build(tmp_path, SourceFake([verdict()]))
    tick(deps)
    tick(deps)
    assert len(deps.lanes.created) == 1


# --- round accounting ------------------------------------------------------


def rereview_posted(deps):
    """Force the loop past its in-flight lane, the way the lane path will.

    A verdict is refused while a lane is still in flight (that lane, not a
    second one, drives the next step), so driving round accounting from the
    verdict path alone means standing in for the lane-completion pass Task 9
    adds: the lane finished, the head moved, the re-review went out.
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
    assert deferred[0]["reason"] == "lane_in_flight"


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
    inside one tick it closes the loop unheard over a momentary blip."""
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
    assert deferred[0]["reason"] == "escalation_attempt_spent"


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
    assert deps.github.released == [("Ylopo", "example", 42)], \
        "the retry re-attempts the write, not the stand-down"


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
    assert deps.slack.reposts == []
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
    poll (a shadow round, or a live flip on top of one) has nothing in flight,
    so a verdict starts a real round from there."""
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
