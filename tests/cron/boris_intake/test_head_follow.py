from __future__ import annotations

from dataclasses import replace

from boris_intake.models import NormalizedEvent, ReviewCommand, WorkflowState
from boris_intake.result import canonicalize_judgment, validate_judgment
from tests.cron.boris_intake.test_reconciler import (
    Clock,
    GitHubFake,
    KanbanFake,
    SlackFake,
    admit_request,
    required_check_snapshot,
    valid_payload,
)

SOURCE = ("C1", "1.0")


def _harness(tmp_path):
    from boris_intake.reconciler import ReconcilerDeps

    store, admitted = admit_request(tmp_path)
    clock = Clock()
    slack = SlackFake()
    github = GitHubFake(admitted.gate_snapshot)
    kanban = KanbanFake()
    kanban.status = "running"
    deps = ReconcilerDeps(slack=slack, github=github, kanban=kanban, now=clock)
    return store, admitted, clock, slack, github, deps


def _tick_until(store, deps, clock, done, limit=20):
    from boris_intake.reconciler import tick

    for _ in range(limit):
        if done():
            return
        tick(store, deps)
        clock.advance()
    assert done()


def _follow(store, request, head, seconds_later):
    return store.follow_head(
        request,
        head,
        request.policy,
        request.gate_snapshot,
        request.created_at + seconds_later,
        3,
    )


def _current(store):
    (request,) = store.requests_for_source(*SOURCE)
    return request


def _reviewing(store, head):
    def done():
        request = _current(store)
        return (
            request.head_sha == head
            and request.workflow_state is WorkflowState.REVIEWING
        )

    return done


def test_push_during_review_restarts_on_the_new_head_without_a_repost(tmp_path):
    store, original, clock, slack, github, deps = _harness(tmp_path)
    _tick_until(store, deps, clock, _reviewing(store, "a" * 40))

    github.head_sha = "b" * 40
    _tick_until(store, deps, clock, _reviewing(store, "b" * 40))

    superseded = store.get_request(original.request_id)
    assert superseded.workflow_state is WorkflowState.COMPLETE
    assert "HEAD_SUPERSEDED" in superseded.validated_result_json
    notice = slack.messages[original.request_id][1]
    assert "restarted automatically on bbbbbbb" in notice
    assert "Repost" not in notice

    successor = _current(store)
    assert successor.request_id != original.request_id
    store.submit_judgment(
        successor.request_id,
        canonicalize_judgment(validate_judgment(valid_payload())),
        clock(),
    )
    _tick_until(
        store,
        deps,
        clock,
        lambda: _current(store).workflow_state is WorkflowState.COMPLETE,
    )
    assert _current(store).semantic_outcome.value == "READY_FOR_HUMAN_REVIEW"
    # The interrupted attempt must not leave its warning on the source message.
    assert slack.source_reactions[-1] == "blinkapproved"


def test_a_fourth_push_ends_the_automatic_restarts_and_asks_for_a_repost(tmp_path):
    store, _original, clock, slack, github, deps = _harness(tmp_path)
    _tick_until(store, deps, clock, _reviewing(store, "a" * 40))

    for head in ("b" * 40, "c" * 40, "d" * 40):
        github.head_sha = head
        _tick_until(store, deps, clock, _reviewing(store, head))

    last = _current(store)
    github.head_sha = "e" * 40
    _tick_until(
        store,
        deps,
        clock,
        lambda: store.get_request(last.request_id).workflow_state
        is WorkflowState.COMPLETE,
    )

    assert _current(store).request_id == last.request_id
    assert len(slack.messages) == 4
    assert "Repost the PR link" in slack.messages[last.request_id][1]


def test_following_the_same_head_twice_yields_one_successor(tmp_path):
    store, original = admit_request(tmp_path)

    first = _follow(store, original, "b" * 40, 1)
    second = _follow(store, original, "b" * 40, 2)

    assert first is not None and second is not None
    assert first.request_id == second.request_id
    assert first.head_sha == "b" * 40
    assert _current(store).request_id == first.request_id


def test_a_predecessor_following_a_later_push_keeps_the_source_on_the_newest_attempt(
    tmp_path,
):
    store, original = admit_request(tmp_path)

    second = _follow(store, original, "b" * 40, 1)
    third = _follow(store, original, "c" * 40, 2)
    assert _follow(store, second, "c" * 40, 3).request_id == third.request_id

    assert _current(store).request_id == third.request_id


def test_a_waiver_given_only_because_the_old_head_had_no_checks_is_not_inherited(
    tmp_path,
):
    from boris_intake.reconciler import tick

    store, original, clock, _slack, github, deps = _harness(tmp_path)
    store._db.execute(
        "UPDATE review_requests SET policy_json=?",
        (replace(original.policy, ignore_ci=True).canonical_json(),),
    )
    store._db.commit()
    _tick_until(store, deps, clock, _reviewing(store, "a" * 40))

    github.snapshot = required_check_snapshot("failure")
    github.head_sha = "b" * 40
    for _ in range(3):
        tick(store, deps)
        clock.advance()

    assert _current(store).head_sha == "b" * 40
    assert _current(store).policy.ignore_ci is False


def test_a_head_already_requested_elsewhere_is_not_restarted_here(tmp_path):
    store, original = admit_request(tmp_path)
    elsewhere = ReviewCommand(
        owner=original.owner,
        repo=original.repo,
        pr_number=original.pr_number,
        head_sha="b" * 40,
        pr_author=original.pr_author,
        requester_user_id="U2",
        requester_email="b@ylopo.com",
        channel_id="C1",
        message_ts="5.0",
        root_thread_ts="5.0",
        permalink="https://ylopo.slack.com/archives/C1/p50",
        policy=original.policy,
    )
    event = NormalizedEvent("C1", "channel", "U2", "5.0", None, "x", False, None)
    store.admit_event(
        event, (elsewhere,), (original.gate_snapshot,), original.created_at + 1
    )

    # Checks have moved on since the repost was admitted, so its fingerprint differs.
    later = required_check_snapshot("success")
    assert (
        store.follow_head(
            original, "b" * 40, original.policy, later, original.created_at + 2, 3
        )
        is None
    )
    assert _current(store).request_id == original.request_id
