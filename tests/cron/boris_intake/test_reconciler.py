from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from boris_intake.models import (
    GateCheck,
    GateSnapshot,
    NormalizedEvent,
    PRInfo,
    ReviewCommand,
    ReviewPolicy,
    SemanticOutcome,
    WorkflowState,
)
from boris_intake.result import canonicalize_judgment, validate_judgment
from boris_intake.store import Store


def valid_payload(**changes):
    payload = {
        "summary": "No substantiated blocking issue was found.",
        "evidence_reviewed": [{"kind": "diff", "reference": "complete PR diff"}],
        "verification_limits": ["Static review; code was not executed locally."],
        "blocking_issues": [],
        "non_blocking_issues": [],
        "risk_hypotheses": [],
        "verification_gaps": [],
        "release_conditions": [],
        "nits": [],
    }
    payload.update(changes)
    return payload


def admit_request(tmp_path, *, snapshot=None):
    store = Store(tmp_path / "review.db")
    command = ReviewCommand(
        owner="Ylopo",
        repo="example",
        pr_number=42,
        head_sha="a" * 40,
        pr_author="octocat",
        requester_user_id="U1",
        requester_email="a@ylopo.com",
        channel_id="C1",
        message_ts="1.0",
        root_thread_ts="1.0",
        permalink="https://ylopo.slack.com/archives/C1/p10",
        policy=ReviewPolicy(ignore_ci=False, rubric_version="2"),
    )
    event = NormalizedEvent(
        "C1", "channel", "U1", "1.0", None, "discarded", False, None
    )
    gate = snapshot or GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())
    request = store.admit_event(event, (command,), (gate,), 1_000_000.0).admitted[0]
    return store, request


def required_check_snapshot(state, captured_at="2026-08-14T12:00:00Z"):
    status = "pending" if state in ("queued", "in_progress") else "completed"
    check = GateCheck(
        check_name="unit-tests",
        check_class="ordinary",
        status=status,
        conclusion=state,
        url="https://ci.example/unit-tests",
        required=True,
    )
    return GateSnapshot(f"sha256:{state}", captured_at, (check,))


@dataclass
class Clock:
    value: float = 1_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=30.0):
        self.value += seconds


class InjectedCrash(BaseException):
    pass


class CrashOnce:
    def __init__(self, point=None):
        self.point = point
        self.triggered = False

    def after(self, point):
        if self.point == point and not self.triggered:
            self.triggered = True
            raise InjectedCrash(point)


class SlackFake:
    def __init__(self, crashes=None):
        self.messages = {}
        self.posts = 0
        self.updates = []
        self.crashes = crashes or CrashOnce()
        self.fail_terminal = 0
        self.fail_reaction = 0
        self.root_available = True
        self.uneditable = False
        self.source_reactions = []
        self.finds = 0

    def find_status(self, request):
        from boris_intake.reconciler import SlackRootUnavailable, SlackStatus

        self.finds += 1
        if not self.root_available:
            raise SlackRootUnavailable("thread_not_found")

        message = self.messages.get(request.request_id)
        return SlackStatus(*message) if message else None

    def post_status(self, request, text):
        from boris_intake.reconciler import SlackRootUnavailable

        if not self.root_available:
            raise SlackRootUnavailable("thread_not_found")
        self.posts += 1
        ts = f"2.{self.posts}"
        self.messages[request.request_id] = (ts, text)
        self.crashes.after("status_post")
        return ts

    def replace_status(self, request, text):
        return self.post_status(request, text)

    def update_status(self, request, message_ts, text):
        from boris_intake.reconciler import SlackRootUnavailable, SlackStatusMissing

        if not self.root_available:
            raise SlackRootUnavailable("thread_not_found")
        current = self.messages.get(request.request_id)
        if current is None or current[0] != message_ts:
            raise SlackStatusMissing("message_not_found")
        if self.uneditable:
            self.uneditable = False
            raise SlackStatusMissing("cant_update_message")
        if "READY" in text and self.fail_terminal:
            self.fail_terminal -= 1
            raise RuntimeError("slack temporarily unavailable")
        self.messages[request.request_id] = (message_ts, text)
        self.updates.append(text)
        point = "terminal_update" if "READY" in text else "reviewing_update"
        self.crashes.after(point)

    def reconcile_source_reaction(self, request, reaction):
        if not self.root_available:
            from boris_intake.reconciler import SlackRootUnavailable

            raise SlackRootUnavailable("thread_not_found")
        if self.fail_reaction:
            self.fail_reaction -= 1
            raise RuntimeError("reaction temporarily unavailable")
        self.source_reactions.append(reaction)


class GitHubFake:
    def __init__(self, snapshot, crashes=None):
        self.snapshot = snapshot
        self.head_sha = "a" * 40
        self.reviews = {}
        self.publications = 0
        self.crashes = crashes or CrashOnce()
        self.fail_publish = 0

    def current_pr(self, request):
        return PRInfo(self.head_sha, request.pr_author, "OPEN")

    def capture_gate_snapshot(self, request, captured_at):
        if self.head_sha != request.head_sha:
            return None
        return self.snapshot

    def find_review(self, request):
        return self.reviews.get(request.request_id)

    def publish_review(self, request, body):
        from boris_intake.reconciler import PublishedReview

        if self.fail_publish:
            self.fail_publish -= 1
            raise RuntimeError("github temporarily unavailable")
        self.publications += 1
        review = PublishedReview(
            review_id="review-1",
            url="https://github.example/reviews/1",
            body=body,
        )
        self.reviews[request.request_id] = review
        self.crashes.after("review_publish")
        return review


class KanbanFake:
    def __init__(self, crashes=None):
        self.lanes = {}
        self.retry_lanes = {}
        self.creations = 0
        self.crashes = crashes or CrashOnce()
        self.status = "done"
        self.priors = []
        self.retired = []
        # ended_at of the latest run that bounced off a provider quota wall, if any.
        self.quota_hold_at = None

    def find_lane(self, request, *, retry=False):
        table = self.retry_lanes if retry else self.lanes
        return table.get(request.review_identity)

    def lane_authentication_failed(self, lane_id):
        return getattr(self, "auth_failed", False)

    def lane_quota_hold(self, lane_id):
        return self.quota_hold_at

    def lane_resource_window(self, lane_id):
        return None

    def retire_lane(self, lane_id):
        self.retired.append(lane_id)

    def create_lane(self, request, *, retry=False, prior=None):
        self.priors.append(prior)
        self.creations += 1
        lane_id = f"t_review_{self.creations}"
        self.lanes[request.review_identity] = lane_id
        self.crashes.after("lane_create")
        return lane_id

    def lane_status(self, lane_id):
        return self.status


@pytest.mark.parametrize("existing_retry_lane", (None, "t_retry"))
def test_prior_bound_lane_uses_request_scoped_retry_lane(
    tmp_path, existing_retry_lane
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    class TerminalLaneKanban:
        def __init__(self):
            self.retry_requested = False

        def find_lane(self, request, *, retry=False):
            return existing_retry_lane if retry else "t_prior"

        def create_lane(self, request, *, retry=False, prior=None):
            self.retry_requested = retry
            return "t_retry"

        def lane_status(self, lane_id):
            return "in_progress"

        def lane_authentication_failed(self, lane_id):
            return False

        def lane_quota_hold(self, lane_id):
            return None

        def lane_resource_window(self, lane_id):
            return None

        def retire_lane(self, lane_id):
            pass

    store, original = admit_request(tmp_path)
    expected = WorkflowState.ADMITTED
    for target in (
        WorkflowState.STATUS_MESSAGE_PENDING,
        WorkflowState.STARTING_REVIEW,
        WorkflowState.REVIEWING,
        WorkflowState.DELIVERY_PENDING,
    ):
        fields = {"lane_id": "t_prior"} if target is WorkflowState.REVIEWING else {}
        assert store.transition(original.request_id, expected, target, **fields)
        expected = target
    assert store.transition(
        original.request_id,
        WorkflowState.DELIVERY_PENDING,
        WorkflowState.COMPLETE,
        semantic_outcome=SemanticOutcome.REVIEW_INCOMPLETE,
        validated_result_json="{}",
        terminal_status_confirmed=True,
    )
    retry_command = ReviewCommand(
        owner=original.owner,
        repo=original.repo,
        pr_number=original.pr_number,
        head_sha=original.head_sha,
        pr_author=original.pr_author,
        requester_user_id=original.requester_user_id,
        requester_email=original.requester_email,
        channel_id=original.channel_id,
        message_ts="2.0",
        root_thread_ts="2.0",
        permalink="https://ylopo.slack.com/archives/C1/p20",
        policy=original.policy,
    )
    retry_event = NormalizedEvent(
        "C1", "channel", "U1", "2.0", None, "discarded", False, None
    )
    admitted = store.admit_event(
        retry_event, (retry_command,), (original.gate_snapshot,), 1_000_001.0
    ).admitted[0]
    clock = Clock()
    kanban = TerminalLaneKanban()
    deps = ReconcilerDeps(
        slack=SlackFake(),
        github=GitHubFake(admitted.gate_snapshot),
        kanban=kanban,
        now=clock,
    )

    for _ in range(4):
        tick(store, deps)
        clock.advance()
        if store.get_request(admitted.request_id).workflow_state is WorkflowState.REVIEWING:
            break

    reviewing = store.get_request(admitted.request_id)
    assert reviewing.workflow_state is WorkflowState.REVIEWING
    assert reviewing.lane_id == "t_retry"
    assert kanban.retry_requested is (existing_retry_lane is None)


def admit_batch(tmp_path):
    store = Store(tmp_path / "review.db")
    event = NormalizedEvent(
        "C1", "channel", "U1", "1.0", None, "discarded", False, None
    )
    commands = tuple(
        ReviewCommand(
            owner="Ylopo",
            repo=f"example-{index}",
            pr_number=40 + index,
            head_sha="a" * 40,
            pr_author="octocat",
            requester_user_id="U1",
            requester_email="a@ylopo.com",
            channel_id="C1",
            message_ts="1.0",
            root_thread_ts="1.0",
            permalink="https://ylopo.slack.com/archives/C1/p10",
            policy=ReviewPolicy(ignore_ci=False, rubric_version="2"),
        )
        for index in range(1, 4)
    )
    snapshot = GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())
    requests = store.admit_event(
        event, commands, (snapshot, snapshot, snapshot), 1_000_000.0
    ).admitted
    return store, requests


@pytest.mark.parametrize("completion_order", ((1, 0, 2), (2, 0, 1)))
def test_three_pr_children_complete_independently_and_aggregate_in_any_order(
    tmp_path, completion_order
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, requests = admit_batch(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(requests[0].gate_snapshot)
    kanban = KanbanFake()
    kanban.status = "in_progress"
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while any(
        store.get_request(request.request_id).workflow_state
        is not WorkflowState.REVIEWING
        for request in requests
    ):
        tick(store, deps)
        clock.advance()

    children = [store.get_request(request.request_id) for request in requests]
    assert len({request.status_message_ts for request in children}) == 3
    assert all("REVIEWING" in slack.messages[request.request_id][1] for request in children)
    assert slack.source_reactions[-1] == "rubbing"

    payloads = {
        0: canonicalize_judgment(validate_judgment(valid_payload())),
        1: canonicalize_judgment(
            validate_judgment(
                valid_payload(
                    nits=[{
                        "title": "Rename local variable",
                        "location": "src/update.py:12",
                        "suggestion": "Use owner_id.",
                    }]
                )
            )
        ),
        2: '{"summary":"untyped"}',
    }
    completed = set()
    for index in completion_order:
        request = requests[index]
        store.submit_judgment(request.request_id, payloads[index], clock())
        for _ in range(8):
            tick(store, deps)
            if (
                store.get_request(request.request_id).workflow_state
                is WorkflowState.COMPLETE
            ):
                break
            clock.advance(15)
        assert (
            store.get_request(request.request_id).workflow_state
            is WorkflowState.COMPLETE
        )
        completed.add(index)
        assert slack.source_reactions[-1] == (
            "warning" if 2 in completed else "rubbing"
        )

    assert slack.messages[requests[0].request_id][1].startswith("READY —")
    assert "READY WITH COMMENTS" in slack.messages[requests[1].request_id][1]
    assert "REVIEW INCOMPLETE" in slack.messages[requests[2].request_id][1]
    assert github.publications == 2


@pytest.mark.parametrize(
    ("requests", "expected"),
    (
        ([SimpleNamespace(request_id="a", workflow_state=WorkflowState.REVIEWING, semantic_outcome=None, validated_result_json=None)], "rubbing"),
        ([SimpleNamespace(request_id="a", workflow_state=WorkflowState.COMPLETE, semantic_outcome=SemanticOutcome.READY_FOR_HUMAN_REVIEW, validated_result_json='{"derived":{"presentation":"READY_CLEAN"}}')], "blinkapproved"),
        ([SimpleNamespace(request_id="a", workflow_state=WorkflowState.COMPLETE, semantic_outcome=SemanticOutcome.READY_FOR_HUMAN_REVIEW, validated_result_json='{"derived":{"presentation":"READY_WITH_COMMENTS"}}')], "doge_notes"),
        ([SimpleNamespace(request_id="a", workflow_state=WorkflowState.COMPLETE, semantic_outcome=SemanticOutcome.REVIEW_INCOMPLETE, validated_result_json="{}"), SimpleNamespace(request_id="b", workflow_state=WorkflowState.REVIEWING, semantic_outcome=None, validated_result_json=None)], "warning"),
        ([SimpleNamespace(request_id="a", workflow_state=WorkflowState.COMPLETE, semantic_outcome=SemanticOutcome.ISSUES_FOUND, validated_result_json="{}"), SimpleNamespace(request_id="b", workflow_state=WorkflowState.COMPLETE, semantic_outcome=SemanticOutcome.REVIEW_INCOMPLETE, validated_result_json="{}")], "face_vomiting"),
        ([SimpleNamespace(request_id="a", workflow_state=WorkflowState.DELIVERY_PENDING, semantic_outcome=SemanticOutcome.ISSUES_FOUND, validated_result_json="{}", terminal_status_confirmed=True), SimpleNamespace(request_id="b", workflow_state=WorkflowState.DELIVERY_PENDING, semantic_outcome=SemanticOutcome.READY_FOR_HUMAN_REVIEW, validated_result_json='{"derived":{"presentation":"READY_CLEAN"}}', terminal_status_confirmed=False)], "face_vomiting"),
    ),
)
def test_aggregate_source_reaction_precedence(requests, expected):
    from boris_intake.reconciler import _aggregate_source_reaction

    assert _aggregate_source_reaction(tuple(requests)) == expected


def test_unchanged_source_reaction_is_not_written_twice(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, _reconcile_source_reaction

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    deps = ReconcilerDeps(
        slack=slack,
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )
    request = store.get_request(admitted.request_id)

    _reconcile_source_reaction(store, request, deps)
    _reconcile_source_reaction(store, request, deps)

    assert slack.source_reactions == ["rubbing"]


def test_terminal_child_waits_and_retries_when_source_reaction_fails(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    judgment = validate_judgment(valid_payload())
    store.submit_judgment(
        admitted.request_id, canonicalize_judgment(judgment), clock()
    )
    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.DELIVERY_PENDING
    ):
        tick(store, deps)
        clock.advance()

    slack.fail_reaction = 1
    attempted_at = clock()
    tick(store, deps)

    pending = store.get_request(admitted.request_id)
    assert pending.workflow_state is WorkflowState.DELIVERY_PENDING
    assert pending.terminal_status_confirmed is True
    assert pending.next_attempt_at == attempted_at + 5
    terminal_updates = [text for text in slack.updates if "READY" in text]
    assert len(terminal_updates) == 1

    clock.advance(5)
    tick(store, deps)

    assert store.get_request(admitted.request_id).workflow_state is WorkflowState.COMPLETE
    assert len([text for text in slack.updates if "READY" in text]) == 1
    assert slack.source_reactions[-1] == "blinkapproved"


def test_sibling_workers_serialize_the_shared_source_reaction(tmp_path):
    from boris_intake.reconciler import (
        ReconcilerDeps,
        _reconcile_source_reaction,
    )

    store, requests = admit_batch(tmp_path)
    store._db.execute(
        "UPDATE review_requests SET workflow_state=?, semantic_outcome=?, "
        "validated_result_json=? WHERE request_id=?",
        (
            WorkflowState.DELIVERY_PENDING.value,
            SemanticOutcome.REVIEW_INCOMPLETE.value,
            "{}",
            requests[0].request_id,
        ),
    )
    store._db.execute(
        "UPDATE review_requests SET workflow_state=?, semantic_outcome=?, "
        "validated_result_json=? WHERE request_id=?",
        (
            WorkflowState.DELIVERY_PENDING.value,
            SemanticOutcome.READY_FOR_HUMAN_REVIEW.value,
            '{"derived":{"presentation":"READY_CLEAN"}}',
            requests[1].request_id,
        ),
    )
    store._db.commit()
    clock = Clock()
    slack = SlackFake()
    deps = ReconcilerDeps(
        slack=slack,
        github=GitHubFake(requests[0].gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )
    entered = threading.Event()
    resume = threading.Event()
    original_reconcile = slack.reconcile_source_reaction

    def pause_after_first_reaction(request, reaction):
        original_reconcile(request, reaction)
        if not entered.is_set():
            entered.set()
            if not resume.wait(timeout=5):
                raise AssertionError("source reaction owner did not resume")

    slack.reconcile_source_reaction = pause_after_first_reaction
    db_path = store.path
    store.close()
    owner_errors = []

    def run_owner():
        owner = Store(db_path)
        try:
            current = owner.get_request(requests[0].request_id)
            _reconcile_source_reaction(
                owner,
                current,
                deps,
                delivered_request_id=current.request_id,
            )
        except BaseException as error:
            owner_errors.append(error)
        finally:
            owner.close()

    owner_thread = threading.Thread(target=run_owner)
    owner_thread.start()
    assert entered.wait(timeout=5)
    sibling = Store(db_path)
    current = sibling.get_request(requests[1].request_id)
    try:
        with pytest.raises(RuntimeError, match="source reaction is already claimed"):
            _reconcile_source_reaction(
                sibling,
                current,
                deps,
                delivered_request_id=current.request_id,
            )
    finally:
        resume.set()
    owner_thread.join(timeout=5)

    assert owner_thread.is_alive() is False
    assert owner_errors == []
    assert slack.source_reactions == ["warning"]

    sibling._db.execute(
        "UPDATE review_requests SET workflow_state=? WHERE request_id=?",
        (WorkflowState.COMPLETE.value, requests[0].request_id),
    )
    sibling._db.commit()
    _reconcile_source_reaction(
        sibling,
        current,
        deps,
        delivered_request_id=current.request_id,
    )
    sibling.close()

    assert slack.source_reactions[-1] == "warning"


@pytest.mark.parametrize(
    "external_call",
    ("github_publication", "slack_update", "slack_replacement"),
)
def test_concurrent_workers_make_only_one_external_delivery_call(
    tmp_path, external_call
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)
    target = (
        WorkflowState.PUBLICATION_PENDING
        if external_call == "github_publication"
        else WorkflowState.DELIVERY_PENDING
    )

    for _ in range(12):
        request = store.get_request(admitted.request_id)
        if request.workflow_state is target:
            break
        tick(store, deps)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        clock.advance()
    assert store.get_request(admitted.request_id).workflow_state is target

    entered = threading.Event()
    resume = threading.Event()

    def pause_before_external_call(call):
        def paused(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not resume.wait(timeout=5):
                    raise AssertionError("concurrent worker did not resume")
            return call(*args, **kwargs)

        return paused

    if external_call == "github_publication":
        github.publish_review = pause_before_external_call(github.publish_review)
        external_count = lambda: github.publications
    elif external_call == "slack_update":
        slack.update_status = pause_before_external_call(slack.update_status)
        external_count = lambda: len(slack.updates)
    else:
        del slack.messages[admitted.request_id]
        slack.post_status = pause_before_external_call(slack.post_status)
        external_count = lambda: slack.posts

    calls_before = external_count()
    db_path = store.path
    store.close()
    owner_errors = []

    def run_owner():
        owner = Store(db_path)
        try:
            tick(owner, deps)
        except BaseException as error:
            owner_errors.append(error)
        finally:
            owner.close()

    owner_thread = threading.Thread(target=run_owner)
    owner_thread.start()
    assert entered.wait(timeout=5)
    loser = Store(db_path)
    try:
        loser_count = tick(loser, deps)
    finally:
        loser.close()
        resume.set()
    owner_thread.join(timeout=5)

    assert owner_thread.is_alive() is False
    assert owner_errors == []
    assert loser_count == 0
    assert external_count() - calls_before == 1


def test_admitted_request_converges_to_one_child_lane_review_and_terminal_update(
    tmp_path,
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    for _ in range(12):
        tick(store, deps)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        if request.workflow_state is WorkflowState.COMPLETE:
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert slack.posts == 1
    assert kanban.creations == 1
    assert github.publications == 1
    assert "<!-- boris-review request_id=" in github.reviews[admitted.request_id].body
    assert slack.messages[admitted.request_id][0] == completed.status_message_ts
    assert "READY" in slack.messages[admitted.request_id][1]
    assert completed.github_review_url in slack.messages[admitted.request_id][1]


@pytest.mark.parametrize(
    "crash_point",
    (
        "status_post",
        "lane_create",
        "reviewing_update",
        "review_publish",
        "terminal_update",
    ),
)
def test_crash_after_external_write_recovers_without_duplicate_effects(
    tmp_path, crash_point
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    crashes = CrashOnce(crash_point)
    slack = SlackFake(crashes)
    github = GitHubFake(admitted.gate_snapshot, crashes)
    kanban = KanbanFake(crashes)
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    for _ in range(20):
        try:
            tick(store, deps)
        except InjectedCrash:
            pass
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        if request.workflow_state is WorkflowState.COMPLETE:
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert crashes.triggered is True
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert slack.posts == 1
    assert kanban.creations == 1
    assert github.publications == 1
    assert len([text for text in slack.updates if "REVIEWING" in text]) == 1
    assert len([text for text in slack.updates if "READY" in text]) == 1
    published_body = github.reviews[admitted.request_id].body
    published_hash = published_body.split("result_hash=", 1)[1].split(" ", 1)[0]
    assert published_hash in completed.validated_result_json


@pytest.mark.parametrize(
    ("terminal_gate", "expected_outcome", "expected_publications"),
    (
        ("success", "READY_FOR_HUMAN_REVIEW", 1),
        (None, "REVIEW_INCOMPLETE", 0),
    ),
)
def test_pending_required_checks_complete_or_time_out_from_judgment_acceptance(
    tmp_path, terminal_gate, expected_outcome, expected_publications
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    pending = required_check_snapshot("queued")
    store, admitted = admit_request(tmp_path, snapshot=pending)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(pending)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
    judgment = validate_judgment(valid_payload())
    accepted_at = clock()
    store.submit_judgment(
        admitted.request_id,
        canonicalize_judgment(judgment),
        accepted_at,
    )
    tick(store, deps)
    waiting = store.get_request(admitted.request_id)
    assert waiting.workflow_state is WorkflowState.GATE_WAIT_PENDING

    clock.advance(1_799)
    if terminal_gate is not None:
        github.snapshot = required_check_snapshot(terminal_gate)
    tick(store, deps)
    if terminal_gate is None:
        assert (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.GATE_WAIT_PENDING
        )
        clock.advance(2)
    for _ in range(8):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance(30)

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.semantic_outcome.value == expected_outcome
    assert github.publications == expected_publications
    if terminal_gate is None:
        assert "GATE_WAIT_TIMEOUT" in completed.validated_result_json
        assert "REVIEW INCOMPLETE" in slack.messages[admitted.request_id][1]


@pytest.mark.parametrize("supersede_at", ("before_dispatch", "before_publication"))
def test_head_change_before_dispatch_or_publication_is_incomplete_without_publish(
    tmp_path, supersede_at
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    target = (
        WorkflowState.STARTING_REVIEW
        if supersede_at == "before_dispatch"
        else WorkflowState.PUBLICATION_PENDING
    )
    for _ in range(8):
        request = store.get_request(admitted.request_id)
        if request.workflow_state is target:
            break
        tick(store, deps)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        clock.advance()

    github.head_sha = "b" * 40
    for _ in range(6):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.semantic_outcome.value == "REVIEW_INCOMPLETE"
    assert github.publications == 0
    assert "HEAD_SUPERSEDED" in completed.validated_result_json
    assert "a" * 40 in completed.validated_result_json
    assert "b" * 40 in completed.validated_result_json
    if supersede_at == "before_dispatch":
        assert kanban.creations == 0


def test_publication_and_delivery_retry_independently_without_rerunning_boris(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    judgment = validate_judgment(valid_payload())
    store.submit_judgment(
        admitted.request_id,
        canonicalize_judgment(judgment),
        clock(),
    )
    tick(store, deps)
    assert (
        store.get_request(admitted.request_id).workflow_state
        is WorkflowState.PUBLICATION_PENDING
    )

    github.fail_publish = 1
    failed_at = clock()
    tick(store, deps)
    publication_failed = store.get_request(admitted.request_id)
    assert publication_failed.workflow_state is WorkflowState.PUBLICATION_PENDING
    assert publication_failed.next_attempt_at == failed_at + 5
    assert publication_failed.last_error.startswith("publication:")
    assert tick(store, deps) == 0

    clock.advance(5)
    tick(store, deps)
    delivery = store.get_request(admitted.request_id)
    assert delivery.workflow_state is WorkflowState.DELIVERY_PENDING

    slack.fail_terminal = 1
    failed_at = clock()
    tick(store, deps)
    delivery_failed = store.get_request(admitted.request_id)
    assert delivery_failed.workflow_state is WorkflowState.DELIVERY_PENDING
    assert delivery_failed.next_attempt_at == failed_at + 5
    assert delivery_failed.last_error.startswith("delivery:")

    clock.advance(5)
    tick(store, deps)
    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.publication_attempts == 2
    assert completed.delivery_attempts == 2
    assert kanban.creations == 1
    accepted = [
        event
        for event in store.request_events(admitted.request_id)
        if event["kind"] == "judgment_accepted"
    ]
    assert len(accepted) == 1


@pytest.mark.parametrize("missing", ("child", "root"))
def test_deleted_child_is_replaced_but_deleted_root_never_gets_an_orphan(
    tmp_path, missing
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    for _ in range(10):
        tick(store, deps)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        if request.workflow_state is WorkflowState.DELIVERY_PENDING:
            break
        clock.advance()

    original_ts = store.get_request(admitted.request_id).status_message_ts
    del slack.messages[admitted.request_id]
    if missing == "root":
        slack.root_available = False
    tick(store, deps)

    terminal = store.get_request(admitted.request_id)
    if missing == "child":
        assert terminal.workflow_state is WorkflowState.COMPLETE
        assert terminal.status_message_ts != original_ts
        assert slack.posts == 2
        assert "READY" in slack.messages[admitted.request_id][1]
    else:
        assert terminal.workflow_state is WorkflowState.SLACK_UNAVAILABLE
        assert slack.posts == 1
        assert admitted.request_id not in slack.messages


@pytest.mark.parametrize("terminal_status", ("done", "archived", "blocked"))
def test_active_lane_without_submission_polls_then_terminal_lane_becomes_incomplete(
    tmp_path, terminal_status
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    kanban.status = "in_progress"
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    polled_at = clock()
    tick(store, deps)
    reviewing = store.get_request(admitted.request_id)
    assert reviewing.workflow_state is WorkflowState.REVIEWING
    assert reviewing.next_attempt_at == polled_at + 15
    assert store.judgment_for_request(admitted.request_id) is None

    kanban.status = terminal_status
    clock.advance(15)
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.semantic_outcome.value == "REVIEW_INCOMPLETE"
    assert "LANE_NO_VALID_RESULT" in completed.validated_result_json
    assert github.publications == 0
    # An archived lane has nothing left to retire; the others are archived so they
    # neither re-run nor land a judgment nobody consumes.
    expected_retired = [] if terminal_status == "archived" else list(kanban.lanes.values())
    assert kanban.retired == expected_retired
    kinds = [event["kind"] for event in store.request_events(admitted.request_id)]
    assert "lane_retire_failed" not in kinds


class AcceptanceFake:
    def __init__(self, evidence=(), error=None):
        self.evidence = evidence
        self.error = error
        self.calls = []

    def fetch(self, title, head_ref):
        self.calls.append((title, head_ref))
        if self.error is not None:
            raise self.error
        return self.evidence


def _drive_to_reviewing(store, admitted, deps, clock):
    from boris_intake.reconciler import tick

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()


def test_ticket_acceptance_evidence_is_authorized_before_the_reviewer_starts(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    evidence = {"kind": "ticket", "reference": "PLAT-2345", "content": "Ship it."}
    acceptance = AcceptanceFake(evidence=(("PLAT-2345", evidence),))
    deps = ReconcilerDeps(
        slack=SlackFake(),
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
        acceptance=acceptance,
    )

    _drive_to_reviewing(store, admitted, deps, clock)

    assert acceptance.calls, "the dispatcher never looked for a ticket"
    assert store.acceptance_evidence(admitted.request_id, "PLAT-2345") == evidence


def test_a_failing_ticket_lookup_never_blocks_the_review(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    acceptance = AcceptanceFake(error=RuntimeError("atlassian is down"))
    deps = ReconcilerDeps(
        slack=SlackFake(),
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
        acceptance=acceptance,
    )

    _drive_to_reviewing(store, admitted, deps, clock)

    reviewing = store.get_request(admitted.request_id)
    assert reviewing.workflow_state is WorkflowState.REVIEWING
    assert store.acceptance_evidence(admitted.request_id, "PLAT-2345") is None
    kinds = [event["kind"] for event in store.request_events(admitted.request_id)]
    assert "acceptance_evidence_unavailable" in kinds


def test_a_reviewer_lane_that_never_finishes_is_bounded(tmp_path):
    """A wedged lane must fail honestly instead of polling for hours."""
    from boris_intake.reconciler import REVIEW_WAIT_SECONDS, ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    kanban.status = "in_progress"  # never reaches a terminal status
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()

    # Inside the deadline a slow reviewer is left alone to keep working.
    clock.advance(REVIEW_WAIT_SECONDS / 2)
    tick(store, deps)
    assert (
        store.get_request(admitted.request_id).workflow_state
        is WorkflowState.REVIEWING
    )

    clock.advance(REVIEW_WAIT_SECONDS)
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.semantic_outcome.value == "REVIEW_INCOMPLETE"
    assert "REVIEW_LANE_TIMEOUT" in completed.validated_result_json
    assert github.publications == 0
    # A finished request must not leave its lane queued: it would keep re-running
    # and land a judgment nobody consumes.
    assert kanban.retired == list(kanban.lanes.values())


def test_starting_review_hands_the_prior_judgment_to_the_lane(tmp_path):
    """A re-review lane is told what the previous Boris review of the same PR
    found, so it can report each prior blocking issue's disposition."""
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    earlier = ReviewCommand(
        owner="Ylopo", repo="example", pr_number=42, head_sha="b" * 40,
        pr_author="octocat", requester_user_id="U1", requester_email="a@ylopo.com",
        channel_id="C1", message_ts="0.5", root_thread_ts="0.5",
        permalink="https://ylopo.slack.com/archives/C1/p05",
        policy=ReviewPolicy(ignore_ci=False, rubric_version="2"),
    )
    prior = store.admit_event(
        NormalizedEvent("C1", "channel", "U1", "0.5", None, "discarded", False, None),
        (earlier,),
        (GateSnapshot("sha256:prior", "2026-08-13T12:00:00Z", ()),),
        999_000.0,
    ).admitted[0]
    # The earlier request already finished; only its stored result matters here.
    store._db.execute(
        "UPDATE review_requests SET validated_result_json=?, workflow_state='complete',"
        " completed_at=999_500.0 WHERE request_id=?",
        (
            json.dumps({
                "derived": {"outcome": "ISSUES_FOUND", "presentation": "ISSUES"},
                "assembled_at": "2026-08-13T12:30:00Z",
                "blocking_issues": [{"title": "Ownership predicate removed"}],
                "non_blocking_issues": [],
            }),
            prior.request_id,
        ),
    )
    store._db.commit()
    clock = Clock()
    kanban = KanbanFake()
    kanban.status = "in_progress"
    deps = ReconcilerDeps(
        slack=SlackFake(), github=GitHubFake(admitted.gate_snapshot), kanban=kanban, now=clock
    )

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()

    assert len(kanban.priors) == 1
    assert kanban.priors[0].head_sha == "b" * 40
    assert kanban.priors[0].blocking_titles == ("Ownership predicate removed",)


def test_retry_lane_also_receives_the_prior_judgment(tmp_path):
    """When the gate-keyed lane already belongs to an earlier request, the retry
    lane created for this request must carry the prior judgment too."""
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    earlier = ReviewCommand(
        owner="Ylopo", repo="example", pr_number=42, head_sha="b" * 40,
        pr_author="octocat", requester_user_id="U1", requester_email="a@ylopo.com",
        channel_id="C1", message_ts="0.5", root_thread_ts="0.5",
        permalink="https://ylopo.slack.com/archives/C1/p05",
        policy=ReviewPolicy(ignore_ci=False, rubric_version="2"),
    )
    prior = store.admit_event(
        NormalizedEvent("C1", "channel", "U1", "0.5", None, "discarded", False, None),
        (earlier,),
        (GateSnapshot("sha256:prior", "2026-08-13T12:00:00Z", ()),),
        999_000.0,
    ).admitted[0]
    store._db.execute(
        "UPDATE review_requests SET validated_result_json=?, workflow_state='complete',"
        " completed_at=999_500.0, lane_id='t_prior' WHERE request_id=?",
        (
            json.dumps({
                "derived": {"outcome": "ISSUES_FOUND", "presentation": "ISSUES"},
                "assembled_at": "2026-08-13T12:30:00Z",
                "blocking_issues": [{"title": "Ownership predicate removed"}],
                "non_blocking_issues": [],
            }),
            prior.request_id,
        ),
    )
    store._db.commit()
    kanban = KanbanFake()
    kanban.status = "in_progress"
    # The gate-keyed lookup finds the earlier request's lane, forcing a retry lane.
    kanban.lanes[admitted.review_identity] = "t_prior"
    clock = Clock()
    deps = ReconcilerDeps(
        slack=SlackFake(), github=GitHubFake(admitted.gate_snapshot), kanban=kanban, now=clock
    )

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()

    assert kanban.creations == 1
    assert [p.head_sha for p in kanban.priors] == ["b" * 40]


def test_a_lane_that_bounced_once_but_recovered_times_out_with_the_plain_code(tmp_path):
    """One quota bounce early on must not relabel a later genuine 90-minute timeout
    as a quota problem once the worker has long been running again."""
    from boris_intake.reconciler import REVIEW_WAIT_SECONDS, tick

    store, admitted, clock, _slack, _github, kanban, deps = _hold_until_reviewing(
        tmp_path, lane_status="in_progress"
    )
    clock.advance(600)
    kanban.quota_hold_at = clock()  # bounced at T+10 min, then recovered
    clock.advance(REVIEW_WAIT_SECONDS + 60)
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert "REVIEW_LANE_TIMEOUT" in completed.validated_result_json
    assert "PROVIDER_QUOTA_EXHAUSTED" not in completed.validated_result_json


def test_a_timed_out_request_leaves_a_running_worker_alone(tmp_path):
    """Archiving a lane whose worker is still running would delete the workspace
    under it and orphan the process; the dispatcher's own bookkeeping ends that run."""
    from boris_intake.reconciler import REVIEW_WAIT_SECONDS, tick

    store, admitted, clock, _slack, _github, kanban, deps = _hold_until_reviewing(
        tmp_path, lane_status="running"
    )
    clock.advance(REVIEW_WAIT_SECONDS + 60)
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert "REVIEW_LANE_TIMEOUT" in completed.validated_result_json
    assert kanban.retired == []


def test_a_quota_walled_lane_holds_past_the_review_deadline_then_fails_honestly(tmp_path):
    """A worker that bounces off the provider quota is requeued, not finished. The
    request must wait through the wall (telling the thread why, and not to repost)
    and fail with a quota-specific code only after the hold cap."""
    from boris_intake.reconciler import (
        QUOTA_HOLD_MAX_SECONDS,
        REVIEW_WAIT_SECONDS,
        ReconcilerDeps,
        tick,
    )

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    kanban.status = "ready"  # requeued by the dispatcher after the quota bounce
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()

    # The worker bounced off the quota a minute before the plain deadline.
    clock.advance(REVIEW_WAIT_SECONDS - 60)
    kanban.quota_hold_at = clock()
    clock.advance(120)
    tick(store, deps)
    held = store.get_request(admitted.request_id)
    assert held.workflow_state is WorkflowState.REVIEWING
    status_text = slack.messages[admitted.request_id][1].lower()
    assert "quota" in status_text
    assert "repost" in status_text
    reads_after_announcement = slack.finds

    # Every fresh bounce slides the deadline, but only up to the hold cap, and the
    # thread is told once: a 15-second poll must not become a Slack read per poll.
    while clock() < admitted.created_at + QUOTA_HOLD_MAX_SECONDS:
        kanban.quota_hold_at = clock()
        tick(store, deps)
        assert (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.REVIEWING
        )
        clock.advance(REVIEW_WAIT_SECONDS / 2)
    assert slack.finds == reads_after_announcement

    kanban.quota_hold_at = clock()
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.semantic_outcome.value == "REVIEW_INCOMPLETE"
    assert "PROVIDER_QUOTA_EXHAUSTED" in completed.validated_result_json
    assert "REVIEW_LANE_TIMEOUT" not in completed.validated_result_json
    assert github.publications == 0
    assert kanban.retired == list(kanban.lanes.values())


REVIEW_POLL_SECONDS_FOR_TEST = 15


def _hold_until_reviewing(tmp_path, *, lane_status="ready"):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    kanban.status = lane_status
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)
    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    return store, admitted, clock, slack, github, kanban, deps


@pytest.mark.parametrize("resumes", [False, True])
def test_disk_admission_wait_does_not_expire_as_reviewer_execution(tmp_path, resumes):
    from boris_intake.reconciler import QUOTA_HOLD_MAX_SECONDS, REVIEW_WAIT_SECONDS, tick

    store, admitted, clock, _slack, github, kanban, deps = _hold_until_reviewing(tmp_path)
    waiting_since = clock()
    kanban.lane_resource_window = lambda lane_id: (waiting_since, None)
    clock.advance(REVIEW_WAIT_SECONDS + 60)
    tick(store, deps)
    assert store.get_request(admitted.request_id).workflow_state is WorkflowState.REVIEWING
    assert not kanban.retired
    assert github.publications == 0
    if resumes:
        resumed_at = clock()
        kanban.lane_resource_window = lambda lane_id: (waiting_since, resumed_at)
        kanban.status = "running"
        clock.advance(REVIEW_WAIT_SECONDS - 60)
        tick(store, deps)
        assert store.get_request(admitted.request_id).workflow_state is WorkflowState.REVIEWING
        clock.advance(120)
    else:
        clock.advance(admitted.created_at + QUOTA_HOLD_MAX_SECONDS - clock() + 1)
    tick(store, deps)
    result = store.get_request(admitted.request_id)
    assert result.workflow_state is WorkflowState.DELIVERY_PENDING
    assert result.semantic_outcome.value == "REVIEW_INCOMPLETE"
    if resumes:
        assert "REVIEW_LANE_TIMEOUT" in result.validated_result_json
        assert not kanban.retired
    else:
        assert "LOCAL_RESOURCE_WAIT_EXPIRED" in result.validated_result_json
        assert kanban.retired == list(kanban.lanes.values())
    assert github.publications == 0


def test_a_lane_blocked_during_a_quota_wall_fails_with_the_quota_code(tmp_path):
    """Lost exit statuses can push a bouncing lane to blocked mid-wall; the thread
    must hear about the quota, not be told to repost into the same wall."""
    from boris_intake.reconciler import tick

    store, admitted, clock, _slack, _github, kanban, deps = _hold_until_reviewing(
        tmp_path, lane_status="in_progress"
    )
    clock.advance(600)
    kanban.quota_hold_at = clock() - 60
    kanban.status = "blocked"
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert "PROVIDER_QUOTA_EXHAUSTED" in completed.validated_result_json
    assert "LANE_NO_VALID_RESULT" not in completed.validated_result_json
    assert kanban.retired == list(kanban.lanes.values())


def test_a_quota_hold_does_not_wedge_on_an_unavailable_slack_thread(tmp_path):
    """Announcing the hold is best effort: a vanished thread must neither block the
    hold's deadline bookkeeping nor produce an error event on every poll."""
    from boris_intake.reconciler import REVIEW_WAIT_SECONDS, tick

    store, admitted, clock, slack, _github, kanban, deps = _hold_until_reviewing(tmp_path)
    clock.advance(REVIEW_WAIT_SECONDS - 60)
    kanban.quota_hold_at = clock()
    slack.root_available = False
    clock.advance(120)
    for _ in range(5):
        tick(store, deps)
        clock.advance(REVIEW_POLL_SECONDS_FOR_TEST)

    request = store.get_request(admitted.request_id)
    assert request.workflow_state is WorkflowState.REVIEWING
    kinds = [event["kind"] for event in store.request_events(admitted.request_id)]
    assert kinds.count("quota_hold") == 1
    assert "error" not in kinds


def test_mechanically_invalid_submission_fails_closed_without_retries(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    assert (
        store.submit_judgment(admitted.request_id, '{"summary":"untyped"}', clock())
        == "accepted"
    )

    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert "INVALID_REVIEW_RESULT" in completed.validated_result_json
    assert completed.last_error is None
    assert github.publications == 0


def test_publication_gate_refresh_pauses_for_pending_and_preserves_boris_categories(
    tmp_path,
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    nit = {
        "title": "Rename local variable",
        "location": "src/update.py:12",
        "suggestion": "Use owner_id.",
    }
    judgment = validate_judgment(valid_payload(nits=[nit]))
    store.submit_judgment(
        admitted.request_id,
        canonicalize_judgment(judgment),
        clock(),
    )
    tick(store, deps)
    assert (
        store.get_request(admitted.request_id).workflow_state
        is WorkflowState.PUBLICATION_PENDING
    )

    github.snapshot = required_check_snapshot("queued")
    paused_at = clock()
    tick(store, deps)
    paused = store.get_request(admitted.request_id)
    assert paused.workflow_state is WorkflowState.PUBLICATION_PENDING
    assert paused.publication_attempts == 0
    assert paused.next_attempt_at == paused_at + 15
    assert paused.last_error is None
    assert github.publications == 0

    github.snapshot = required_check_snapshot("failure")
    clock.advance(15)
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    payload = json.loads(completed.validated_result_json)
    assert completed.semantic_outcome.value == "ISSUES_FOUND"
    assert payload["blocking_issues"] == []
    assert payload["nits"] == [nit]
    assert [gate["check_name"] for gate in payload["blocking_gate_conditions"]] == [
        "unit-tests"
    ]
    assert "## Blocking Gate Conditions" in github.reviews[admitted.request_id].body
    assert "## Nits" in github.reviews[admitted.request_id].body


@pytest.mark.parametrize(
    "crash_target",
    (
        WorkflowState.STATUS_MESSAGE_PENDING,
        WorkflowState.STARTING_REVIEW,
        WorkflowState.REVIEWING,
        WorkflowState.PUBLICATION_PENDING,
        WorkflowState.DELIVERY_PENDING,
        WorkflowState.COMPLETE,
    ),
)
def test_crash_after_persisted_transition_resumes_from_the_committed_state(
    tmp_path, monkeypatch, crash_target
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)
    transition = store.transition
    crashed = False

    def crash_after_commit(request_id, expected, target, **fields):
        nonlocal crashed
        changed = transition(request_id, expected, target, **fields)
        if changed and target is crash_target and not crashed:
            crashed = True
            raise InjectedCrash(target.value)
        return changed

    monkeypatch.setattr(store, "transition", crash_after_commit)
    for _ in range(20):
        try:
            tick(store, deps)
        except InjectedCrash:
            pass
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        if request.workflow_state is WorkflowState.COMPLETE:
            break
        clock.advance()

    assert crashed is True
    assert (
        store.get_request(admitted.request_id).workflow_state is WorkflowState.COMPLETE
    )
    assert slack.posts == 1
    assert kanban.creations == 1
    assert github.publications == 1
    assert len([text for text in slack.updates if "REVIEWING" in text]) == 1
    assert len([text for text in slack.updates if "READY" in text]) == 1


def test_existing_request_marker_with_a_different_result_hash_is_not_adopted(tmp_path):
    from boris_intake.reconciler import PublishedReview, ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    judgment = validate_judgment(valid_payload())
    store.submit_judgment(
        admitted.request_id,
        canonicalize_judgment(judgment),
        clock(),
    )
    tick(store, deps)
    github.reviews[admitted.request_id] = PublishedReview(
        "review-conflict",
        "https://github.example/reviews/conflict",
        (
            "conflicting\n<!-- boris-review request_id="
            f"{admitted.request_id} result_hash=sha256:{'f' * 64} -->"
        ),
    )

    attempted_at = clock()
    tick(store, deps)

    blocked = store.get_request(admitted.request_id)
    assert blocked.workflow_state is WorkflowState.PUBLICATION_PENDING
    assert blocked.github_review_id is None
    assert blocked.last_error.startswith("publication:")
    assert blocked.next_attempt_at == attempted_at + 5
    assert github.publications == 0


def test_retry_backoff_is_exponential_capped_and_alerts_once_at_threshold(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    judgment = validate_judgment(valid_payload())
    store.submit_judgment(
        admitted.request_id,
        canonicalize_judgment(judgment),
        clock(),
    )
    tick(store, deps)
    github.fail_publish = 8

    delays = []
    for _ in range(8):
        attempted_at = clock()
        tick(store, deps)
        failed = store.get_request(admitted.request_id)
        delays.append(failed.next_attempt_at - attempted_at)
        clock.value = failed.next_attempt_at

    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]
    alerts = [
        event
        for event in store.request_events(admitted.request_id)
        if event["kind"] == "retry_alert"
    ]
    assert len(alerts) == 1
    assert alerts[0]["detail"] == {"attempt": 5, "stage": "publication"}


def test_root_deleted_before_child_creation_reaches_slack_unavailable_without_post(
    tmp_path,
):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    slack.root_available = False
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    for _ in range(4):
        tick(store, deps)
        clock.advance()

    terminal = store.get_request(admitted.request_id)
    assert terminal.workflow_state is WorkflowState.SLACK_UNAVAILABLE
    assert terminal.semantic_outcome.value == "REVIEW_INCOMPLETE"
    assert slack.posts == 0
    assert kanban.creations == 0


def test_head_change_while_waiting_on_gates_is_superseded_not_retried(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    pending = required_check_snapshot("queued")
    store, admitted = admit_request(tmp_path, snapshot=pending)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(pending)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    while (
        store.get_request(admitted.request_id).workflow_state
        is not WorkflowState.REVIEWING
    ):
        tick(store, deps)
        clock.advance()
    judgment = validate_judgment(valid_payload())
    store.submit_judgment(
        admitted.request_id,
        canonicalize_judgment(judgment),
        clock(),
    )
    tick(store, deps)
    assert (
        store.get_request(admitted.request_id).workflow_state
        is WorkflowState.GATE_WAIT_PENDING
    )

    github.head_sha = "b" * 40
    for _ in range(4):
        tick(store, deps)
        if (
            store.get_request(admitted.request_id).workflow_state
            is WorkflowState.COMPLETE
        ):
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert "HEAD_SUPERSEDED" in completed.validated_result_json
    assert completed.last_error is None
    assert github.publications == 0


def test_uneditable_child_is_replaced_in_the_original_thread(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)

    for _ in range(10):
        tick(store, deps)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload())
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(judgment),
                clock(),
            )
        if request.workflow_state is WorkflowState.DELIVERY_PENDING:
            break
        clock.advance()

    original_ts = store.get_request(admitted.request_id).status_message_ts
    slack.uneditable = True
    tick(store, deps)

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    assert completed.status_message_ts != original_ts
    assert slack.posts == 2
    assert "READY" in slack.messages[admitted.request_id][1]


def test_shadow_runner_only_refreshes_heartbeat(tmp_path):
    from boris_intake.config import Config
    from boris_intake.reconciler import ReconcilerDeps, run_once

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    deps = ReconcilerDeps(
        slack=SlackFake(),
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )
    heartbeat = tmp_path / "reconciler-heartbeat"

    assert run_once(
        store,
        deps,
        Config(mode="shadow"),
        heartbeat_path=heartbeat,
        limit=1,
    ) == 0
    assert heartbeat.read_text(encoding="utf-8") == "1000000\n"
    assert store.get_request(admitted.request_id).workflow_state is WorkflowState.ADMITTED


def test_live_runner_processes_only_the_bounded_batch(tmp_path):
    from boris_intake.config import Config
    from boris_intake.reconciler import ReconcilerDeps, run_once

    store, admitted = admit_batch(tmp_path)
    clock = Clock()
    deps = ReconcilerDeps(
        slack=SlackFake(),
        github=GitHubFake(admitted[0].gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )

    assert run_once(
        store,
        deps,
        Config(mode="live"),
        heartbeat_path=tmp_path / "reconciler-heartbeat",
        limit=1,
    ) == 1
    states = [store.get_request(request.request_id).workflow_state for request in admitted]
    assert states.count(WorkflowState.STATUS_MESSAGE_PENDING) == 1
    assert states.count(WorkflowState.ADMITTED) == len(admitted) - 1


def test_service_loop_stops_after_signal_event(tmp_path):
    from boris_intake.config import Config
    from boris_intake.reconciler import ReconcilerDeps, run_service

    store, admitted = admit_request(tmp_path)
    stop = threading.Event()

    def stop_on_first_iteration():
        stop.set()
        return 1_000_000.0

    deps = ReconcilerDeps(
        slack=SlackFake(),
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=stop_on_first_iteration,
    )
    heartbeat = tmp_path / "reconciler-heartbeat"

    run_service(
        store,
        deps,
        Config(mode="shadow"),
        stop=stop,
        heartbeat_path=heartbeat,
        interval_seconds=60,
    )

    assert heartbeat.read_text(encoding="utf-8") == "1000000\n"
    assert store.get_request(admitted.request_id).workflow_state is WorkflowState.ADMITTED


@pytest.mark.allow_real_home_io
def test_real_dependency_builder_uses_live_lifecycle_adapters(tmp_path):
    from boris_intake.config import Config
    from boris_intake.dispatch import KanbanIO
    from boris_intake.github import GitHubIO
    from boris_intake.reconciler import build_deps
    from boris_intake.slack_io import SlackIO

    token_path = tmp_path / "github-token"
    cfg = Config(mode="live", github_token_path=token_path)

    deps = build_deps(
        cfg,
        "slack-token",
        slack_caller=lambda *_args, **_kwargs: {"ok": True},
        kanban_db=tmp_path / "kanban.db",
        now=lambda: 123.0,
    )

    assert isinstance(deps.slack, SlackIO)
    assert deps.slack.dry_run is False
    assert isinstance(deps.github, GitHubIO)
    assert isinstance(deps.kanban, KanbanIO)
    assert deps.now() == 123.0


def test_main_fails_closed_when_slack_token_is_missing(monkeypatch, tmp_path):
    from boris_intake import reconciler
    from boris_intake.config import Config

    monkeypatch.setattr(reconciler, "load_config", lambda: Config(mode="live"))
    monkeypatch.setattr(reconciler, "parse_env", lambda _path: {})
    monkeypatch.setattr(reconciler, "DB_PATH", tmp_path / "must-not-be-created.db")
    monkeypatch.setattr(reconciler, "HEARTBEAT", tmp_path / "must-not-be-written")

    assert reconciler.main() == 2
    assert not Path(reconciler.DB_PATH).exists()
    assert not Path(reconciler.HEARTBEAT).exists()


def test_slack_only_routing_sends_findings_to_github_and_bare_incompletes_to_slack(tmp_path):
    from boris_intake.reconciler import _slack_only
    from boris_intake import result as result_module

    _store, admitted = admit_request(tmp_path)

    def assemble(**payload_changes):
        judgment = validate_judgment(valid_payload(**payload_changes))
        return result_module.assemble_result(
            admitted, judgment, admitted.gate_snapshot, None, "2026-08-14T12:01:00Z"
        )

    core_gap = {
        "title": "Cross-repo states unassessed",
        "unassessed_claim": "The linked PR states are accurate.",
        "evidence_inspected": [{"kind": "diff", "reference": "PR diff"}],
        "missing_evidence": "Snapshots of each linked repository.",
        "prevents_core_assessment": True,
        "next_step": "Attach immutable snapshots for each linked PR.",
        "reason_code": "CROSS_REPO_STATUS_EVIDENCE_UNAVAILABLE",
    }
    bare = assemble(verification_gaps=[core_gap])
    with_findings = assemble(
        verification_gaps=[core_gap],
        nits=[{"title": "Rename var", "location": "src/a.py:1", "suggestion": "Use owner_id."}],
    )

    assert _slack_only(bare) is True
    assert _slack_only(with_findings) is False
    assert _slack_only(assemble()) is False


def test_the_delivered_verdict_carries_no_gate_door_marker(tmp_path):
    """`cfg` reaches `_reconcile_delivery` for exactly one reason: the terminal
    Slack text is the one message this service composes out of model-authored
    text about an untrusted pull request, and it is posted into a thread under
    the same bot user `gate.py::_is_loop_repost` admits as Chad. Stop threading
    `cfg` -- the state this code was in -- and the marker rides out into the
    channel with every PR URL the reviewer wrote next to it.

    Drives the real `tick` with a real `Config`, so it pins the wiring and not
    just `render_slack`'s own behaviour (which `test_result.py` covers)."""
    from boris_intake.config import Config
    from boris_intake.reconciler import ReconcilerDeps, tick

    loop_marker = "[fixed it. probably. v1]"
    discovery_marker = "[swept this one up. v1]"
    cfg = Config(
        mode="live",
        channels=("C1",),
        passive_channels=("C1",),
        loop_bot_user_id="U0B9TK6B41M",
        loop_marker=loop_marker,
        loop_attributed_user_id="U07E3AHV686",
        discovery_bot_user_id="U0B9TK6B41M",
        discovery_marker=discovery_marker,
    )
    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    deps = ReconcilerDeps(
        slack=slack,
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )
    finding = {
        "title": f"Branch name quotes {loop_marker} and {discovery_marker}",
        "expected_behavior": "Only the owner may update the record.",
        "changed_mechanism": "The ownership predicate was removed.",
        "trigger": "A signed-in non-owner submits the update endpoint.",
        "consequence": "The non-owner changes another user's record.",
        "impact_scope": "Any record addressable by ID.",
        "evidence_anchors": [{"kind": "diff", "reference": "src/update.py:42"}],
        "action": "Restore the ownership predicate before the write.",
    }

    for _ in range(12):
        tick(store, deps, cfg)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            judgment = validate_judgment(valid_payload(blocking_issues=[finding]))
            store.submit_judgment(
                request.request_id, canonicalize_judgment(judgment), clock()
            )
        if request.workflow_state is WorkflowState.COMPLETE:
            break
        clock.advance()

    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    delivered = slack.messages[admitted.request_id][1]
    assert "ISSUES FOUND" in delivered
    assert loop_marker not in delivered
    assert discovery_marker not in delivered
    # Neutralised, not dropped: the finding is still readable.
    assert "Branch name quotes" in delivered


@pytest.mark.parametrize("module", ("reconciler", "daemon", "replay"))
def test_importing_these_modules_does_not_rebind_sibling_packages(module):
    """All three reach DEPLOYED_SCRIPTS for `slack_read`, which lives only
    there. Each must APPEND that root, never prepend it: all three are imported
    at collection time by this suite, so a prepend puts the whole deployed tree
    ahead of the repo checkout for the rest of the process and every sibling
    package not yet imported resolves from `~/.hermes/scripts`. That silently
    disabled pr_discovery's regression tests in a combined
    `tests/cron/boris_intake/ tests/cron/pr_discovery/` run -- mutating
    `pr_discovery/main.py::AUTHOR` survived it and failed a solo run.

    The identical mistake is documented at length in
    `pr_remediation/daemon.py::_bootstrap_sys_path`, which made it against
    boris_intake and fixed it the same way.

    Parametrized because one fixed module does not help if a sibling still
    prepends -- fixing `reconciler` alone left the AUTHOR mutation invisible,
    because `daemon` and `replay` were doing it too.

    Run in a subprocess: the damage is to a process-wide import table, so it
    cannot be observed from inside a session that has already imported both."""
    import subprocess
    import sys as _sys
    from pathlib import Path

    repo_scripts = Path(__file__).resolve().parents[3] / "cron" / "scripts"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(repo_scripts)!r})\n"
        f"import boris_intake.{module}\n"
        "import pr_discovery.main\n"
        "print(pr_discovery.main.__file__)\n"
    )
    done = subprocess.run([_sys.executable, "-B", "-c", code],
                          capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith(str(repo_scripts)), (
        f"importing boris_intake.{module} rebound pr_discovery to the "
        f"deployed tree: {done.stdout.strip()}")


@pytest.mark.parametrize("module", ("reconciler", "daemon", "replay"))
def test_these_modules_still_reach_the_deployed_slack_transport(module):
    """The other direction, so the isolation fix above cannot be "fixed" into
    three modules that no longer start. `slack_read` lives ONLY under
    `/Users/ctao/.hermes/scripts` and each of these imports it at module scope,
    so the appended root has to actually supply it -- appending is sufficient
    precisely because nothing earlier on the path provides that name, which is
    the same property that makes it unable to rebind anything else."""
    import json
    import subprocess
    import sys as _sys
    from pathlib import Path

    repo_scripts = Path(__file__).resolve().parents[3] / "cron" / "scripts"
    code = (
        "import json, sys\n"
        f"sys.path[:] = [{str(repo_scripts)!r}] + [p for p in sys.path if p]\n"
        f"import boris_intake.{module} as m\n"
        "import slack_read\n"
        "print(json.dumps({'module': m.__file__,\n"
        "                  'slack_read': slack_read.__file__}))\n"
    )
    done = subprocess.run([_sys.executable, "-B", "-c", code],
                          capture_output=True, text=True, timeout=60, check=False)

    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen["module"].startswith(str(repo_scripts)), (
        "the module under test must be the repo copy")
    assert seen["slack_read"].startswith("/Users/ctao/.hermes/scripts"), (
        "the appended root no longer supplies slack_read, so this module "
        "would fail to start")


@pytest.mark.allow_real_home_io
def test_real_dependency_builder_owns_reactions_only_in_passive_channels(tmp_path):
    """Mention-only channels (just-do-it) share `blinkapproved` with the
    jdi_autoapprove stamp; the live SlackIO must know which channels are Boris's."""
    from boris_intake.config import Config
    from boris_intake.reconciler import build_deps

    cfg = Config(
        mode="live",
        github_token_path=tmp_path / "github-token",
        channels=("C_OWNED", "C_MENTION_ONLY"),
        passive_channels=("C_OWNED",),
    )

    deps = build_deps(
        cfg,
        "slack-token",
        slack_caller=lambda *_args, **_kwargs: {"ok": True},
        kanban_db=tmp_path / "kanban.db",
        now=lambda: 123.0,
    )

    assert deps.slack._owned_channels == frozenset({"C_OWNED"})


def test_store_memo_keeps_the_aggregate_when_slack_shows_the_substitute(tmp_path):
    """In an unowned channel SlackIO shows `doge_notes` for a clean verdict, but the
    store memo keeps the aggregate `blinkapproved` so an unchanged aggregate is
    still recognised on the next tick instead of re-issuing the Slack calls."""
    from boris_intake.reconciler import ReconcilerDeps, _reconcile_source_reaction, tick
    from boris_intake.slack_io import SlackIO

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    fake = SlackFake()
    deps = ReconcilerDeps(
        slack=fake,
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )
    while store.get_request(admitted.request_id).workflow_state is not WorkflowState.REVIEWING:
        tick(store, deps)
        clock.advance()
    store.submit_judgment(
        admitted.request_id, canonicalize_judgment(validate_judgment(valid_payload())), clock()
    )
    while store.get_request(admitted.request_id).workflow_state is not WorkflowState.COMPLETE:
        tick(store, deps)
        clock.advance()
    assert fake.source_reactions[-1] == "blinkapproved"
    request = store.get_request(admitted.request_id)
    # Rewind the memo to the pre-terminal state so the terminal reconcile runs again,
    # this time through a real SlackIO that does not own the channel.
    store.record_source_reaction(request.request_id, "rubbing", clock())
    calls = []

    def call(token, method, **kw):
        calls.append((method, kw))
        return {"ok": True}

    deps = ReconcilerDeps(
        slack=SlackIO(call, "t", dry_run=False, owned_channels=frozenset()),
        github=GitHubFake(admitted.gate_snapshot),
        kanban=KanbanFake(),
        now=clock,
    )
    _reconcile_source_reaction(store, request, deps, delivered_request_id=request.request_id)
    _reconcile_source_reaction(store, request, deps, delivered_request_id=request.request_id)

    added = [kw["name"] for method, kw in calls if method == "reactions.add"]
    assert added == ["doge_notes"]
    assert store.source_reaction(request.channel_id, request.message_ts) == "blinkapproved"


def test_authentication_failure_is_actionable_even_after_a_quota_hold(tmp_path):
    from boris_intake.reconciler import tick

    store, admitted, clock, _slack, github, kanban, deps = _hold_until_reviewing(
        tmp_path, lane_status="in_progress"
    )
    kanban.status = "blocked"
    kanban.auth_failed = True
    kanban.quota_hold_at = clock()
    for _ in range(5):
        tick(store, deps)
        clock.advance()
    completed = store.get_request(admitted.request_id)
    assert completed.workflow_state is WorkflowState.COMPLETE
    result = json.loads(completed.validated_result_json)
    assert result["incomplete"]["reason_codes"] == ["PROVIDER_AUTHENTICATION_REQUIRED"]
    assert "hermes" in result["incomplete"]["retry_guidance"]
    assert "boris_on_demand" in result["incomplete"]["retry_guidance"]
    assert github.publications == 0
    assert kanban.retired == list(kanban.lanes.values())
