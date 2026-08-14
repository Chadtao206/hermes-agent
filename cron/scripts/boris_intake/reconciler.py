"""Advance durable Boris review requests one lifecycle state at a time."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from boris_intake.config import Config
from boris_intake.models import (
    GateSnapshot,
    LifecycleRequest,
    PRInfo,
    PublishedReview,
    SemanticOutcome,
    SlackStatus,
    WorkflowState,
)
from boris_intake.result import (
    StructuredResult,
    SystemFailure,
    apply_gate_policy,
    assemble_result,
    canonicalize_result,
    render_github,
    render_slack,
    validate_judgment,
)
from boris_intake.store import Store
from boris_intake.slack_io import SlackRootUnavailable, SlackStatusMissing

GATE_WAIT_SECONDS = 30 * 60
GATE_POLL_SECONDS = 15
REVIEW_POLL_SECONDS = 15
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 5 * 60


class SlackLifecycleAdapter(Protocol):
    def find_status(self, request: LifecycleRequest) -> SlackStatus | None: ...
    def post_status(self, request: LifecycleRequest, text: str) -> str: ...
    def update_status(
        self, request: LifecycleRequest, message_ts: str, text: str
    ) -> None: ...
    def replace_status(self, request: LifecycleRequest, text: str) -> str: ...
    def reconcile_source_reaction(
        self, request: LifecycleRequest, reaction: str
    ) -> None: ...


class GitHubLifecycleAdapter(Protocol):
    def current_pr(self, request: LifecycleRequest) -> PRInfo | None: ...
    def capture_gate_snapshot(
        self, request: LifecycleRequest, captured_at: str
    ) -> GateSnapshot | None: ...
    def find_review(self, request: LifecycleRequest) -> PublishedReview | None: ...
    def publish_review(
        self, request: LifecycleRequest, body: str
    ) -> PublishedReview: ...


class KanbanAdapter(Protocol):
    def find_lane(self, request: LifecycleRequest) -> str | None: ...
    def create_lane(self, request: LifecycleRequest) -> str: ...
    def lane_status(self, lane_id: str) -> str | None: ...


@dataclass(frozen=True)
class ReconcilerDeps:
    slack: SlackLifecycleAdapter
    github: GitHubLifecycleAdapter
    kanban: KanbanAdapter
    now: Callable[[], float]


def _timestamp(now: float) -> str:
    return (
        datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _active_text(request: LifecycleRequest, label: str) -> str:
    lines = [f"{label} — {request.slug} @ {request.head_sha[:7]}"]
    if request.policy.ignore_ci:
        lines.append("Policy: CI gate waived")
    return "\n".join(lines)


def _load_result(request: LifecycleRequest) -> StructuredResult:
    if not request.validated_result_json:
        raise ValueError("review request has no validated result")
    raw = json.loads(request.validated_result_json)
    snapshot = GateSnapshot.from_json(json.dumps(raw["gate_snapshot"]))
    incomplete = raw.get("incomplete")
    if isinstance(incomplete, dict) and incomplete.get("origin") == "system":
        result = assemble_result(
            request,
            None,
            snapshot,
            SystemFailure(
                str(incomplete["reason_codes"][0]),
                str(incomplete["detail"]),
                str(incomplete["retry_guidance"]),
            ),
            str(raw["assembled_at"]),
        )
    else:
        judgment = validate_judgment({
            field: raw[field]
            for field in (
                "summary",
                "evidence_reviewed",
                "verification_limits",
                "blocking_issues",
                "non_blocking_issues",
                "risk_hypotheses",
                "verification_gaps",
                "release_conditions",
                "nits",
            )
        })
        result = assemble_result(
            request, judgment, snapshot, None, str(raw["assembled_at"])
        )
    if canonicalize_result(result) != request.validated_result_json:
        raise ValueError("stored result does not match the durable review identity")
    return result


def _semantic(result: StructuredResult) -> SemanticOutcome:
    return SemanticOutcome(result.derived.outcome)


def _aggregate_source_reaction(
    requests: tuple[LifecycleRequest, ...],
    delivered_request_id: str | None = None,
) -> str:
    delivered = tuple(
        request
        for request in requests
        if request.workflow_state is WorkflowState.COMPLETE
        or getattr(request, "terminal_status_confirmed", False)
        or request.request_id == delivered_request_id
    )
    outcomes = {request.semantic_outcome for request in delivered}
    if SemanticOutcome.ISSUES_FOUND in outcomes:
        return "face_vomiting"
    if SemanticOutcome.REVIEW_INCOMPLETE in outcomes:
        return "warning"
    if len(delivered) != len(requests):
        return "rubbing"
    presentations = set()
    for request in delivered:
        if request.semantic_outcome is not SemanticOutcome.READY_FOR_HUMAN_REVIEW:
            raise ValueError("delivered request has no aggregate reaction outcome")
        if not request.validated_result_json:
            raise ValueError("ready request has no validated result")
        raw = json.loads(request.validated_result_json)
        derived = raw.get("derived")
        presentation = derived.get("presentation") if isinstance(derived, dict) else None
        if presentation not in {"READY_CLEAN", "READY_WITH_COMMENTS"}:
            raise ValueError("ready request has no valid presentation")
        presentations.add(presentation)
    return "doge_notes" if "READY_WITH_COMMENTS" in presentations else "blinkapproved"


def _reconcile_source_reaction(
    store: Store,
    request: LifecycleRequest,
    deps: ReconcilerDeps,
    delivered_request_id: str | None = None,
) -> None:
    if not store.acquire_source_reaction_lease(
        request.channel_id, request.message_ts, deps.now()
    ):
        raise RuntimeError("source reaction is already claimed")
    try:
        requests = store.requests_for_source(request.channel_id, request.message_ts)
        if not requests:
            raise ValueError("review request has no source batch")
        deps.slack.reconcile_source_reaction(
            request,
            _aggregate_source_reaction(requests, delivered_request_id),
        )
    finally:
        store.release_source_reaction_lease(request.channel_id, request.message_ts)


def _transition_system_incomplete(
    store: Store,
    request: LifecycleRequest,
    expected: WorkflowState,
    snapshot: GateSnapshot,
    failure: SystemFailure,
    deps: ReconcilerDeps,
) -> None:
    result = assemble_result(request, None, snapshot, failure, _timestamp(deps.now()))
    store.transition(
        request.request_id,
        expected,
        WorkflowState.DELIVERY_PENDING,
        now=deps.now(),
        gate_snapshot_json=snapshot,
        semantic_outcome=_semantic(result),
        validated_result_json=canonicalize_result(result),
        next_attempt_at=deps.now(),
        last_error=None,
    )


def _head_failure(request: LifecycleRequest, current: PRInfo | None) -> SystemFailure:
    if current is not None and current.head_sha != request.head_sha:
        return SystemFailure(
            "HEAD_SUPERSEDED",
            (
                f"The requested head {request.head_sha} was superseded by "
                f"{current.head_sha} before this review could finish."
            ),
            "Repost the PR link to review the current head.",
        )
    if current is not None and current.state != "OPEN":
        return SystemFailure(
            "PR_NOT_OPEN",
            f"The pull request is now {current.state.lower()}.",
            "Reopen the pull request or post a different open PR.",
        )
    return SystemFailure(
        "PR_UNAVAILABLE",
        "The pull request could not be revalidated at its requested head.",
        "Retry after GitHub is available.",
    )


def _reconcile_status_pending(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps
) -> None:
    try:
        status = deps.slack.find_status(request)
        if status is None:
            status_ts = deps.slack.post_status(
                request, _active_text(request, "STARTING REVIEW")
            )
        else:
            status_ts = status.message_ts
    except SlackRootUnavailable:
        _transition_system_incomplete(
            store,
            request,
            WorkflowState.STATUS_MESSAGE_PENDING,
            request.gate_snapshot,
            SystemFailure(
                "SLACK_ROOT_UNAVAILABLE",
                "The source Slack thread is no longer available.",
                "Post a new review request in an available Slack thread.",
            ),
            deps,
        )
        return
    store.transition(
        request.request_id,
        WorkflowState.STATUS_MESSAGE_PENDING,
        WorkflowState.STARTING_REVIEW,
        now=deps.now(),
        status_message_ts=status_ts,
        next_attempt_at=deps.now(),
        last_error=None,
    )


def _reconcile_starting(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps
) -> None:
    current = deps.github.current_pr(request)
    if current is None:
        raise RuntimeError("pull request could not be revalidated before dispatch")
    if current.state != "OPEN" or current.head_sha != request.head_sha:
        _transition_system_incomplete(
            store,
            request,
            WorkflowState.STARTING_REVIEW,
            request.gate_snapshot,
            _head_failure(request, current),
            deps,
        )
        return
    lane_id = deps.kanban.find_lane(request)
    if lane_id is None:
        lane_id = deps.kanban.create_lane(request)
    if request.status_message_ts is None:
        raise ValueError("starting request has no Slack status identity")
    reviewing_text = _active_text(request, "REVIEWING")
    try:
        status = deps.slack.find_status(request)
        if status is None:
            status_ts = deps.slack.post_status(request, reviewing_text)
        else:
            status_ts = status.message_ts
            if status.text != reviewing_text:
                try:
                    deps.slack.update_status(request, status.message_ts, reviewing_text)
                except SlackStatusMissing:
                    status_ts = deps.slack.replace_status(request, reviewing_text)
    except SlackRootUnavailable:
        _transition_system_incomplete(
            store,
            request,
            WorkflowState.STARTING_REVIEW,
            request.gate_snapshot,
            SystemFailure(
                "SLACK_ROOT_UNAVAILABLE",
                "The source Slack thread disappeared before reviewer dispatch.",
                "Post a new review request in an available Slack thread.",
            ),
            deps,
        )
        return
    store.transition(
        request.request_id,
        WorkflowState.STARTING_REVIEW,
        WorkflowState.REVIEWING,
        now=deps.now(),
        lane_id=lane_id,
        status_message_ts=status_ts,
        next_attempt_at=deps.now(),
        last_error=None,
    )


def _reconcile_reviewing(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps
) -> None:
    canonical_judgment = store.judgment_for_request(request.request_id)
    if canonical_judgment is None:
        if request.lane_id is None:
            raise ValueError("reviewing request has no Kanban lane identity")
        status = deps.kanban.lane_status(request.lane_id)
        if status in {"done", "archived"}:
            _transition_system_incomplete(
                store,
                request,
                WorkflowState.REVIEWING,
                request.gate_snapshot,
                SystemFailure(
                    "LANE_NO_VALID_RESULT",
                    "The reviewer lane finished without a valid structured result.",
                    "Repost the PR link to start a new review attempt.",
                ),
                deps,
            )
            return
        if status is None:
            raise RuntimeError("reviewer lane could not be found")
        store.checkpoint(
            request.request_id,
            WorkflowState.REVIEWING,
            now=deps.now(),
            next_attempt_at=deps.now() + REVIEW_POLL_SECONDS,
            last_error=None,
        )
        return
    try:
        judgment = validate_judgment(json.loads(canonical_judgment))
    except (TypeError, ValueError):
        _transition_system_incomplete(
            store,
            request,
            WorkflowState.REVIEWING,
            request.gate_snapshot,
            SystemFailure(
                "INVALID_REVIEW_RESULT",
                "The reviewer submission did not satisfy the structured result contract.",
                "Repost the PR link to start a new review attempt.",
            ),
            deps,
        )
        return
    current = deps.github.current_pr(request)
    if current is None:
        raise RuntimeError("pull request could not be revalidated after judgment")
    if current.state != "OPEN" or current.head_sha != request.head_sha:
        _transition_system_incomplete(
            store,
            request,
            WorkflowState.REVIEWING,
            request.gate_snapshot,
            _head_failure(request, current),
            deps,
        )
        return
    snapshot = deps.github.capture_gate_snapshot(request, _timestamp(deps.now()))
    if snapshot is None:
        raise RuntimeError("could not capture the exact-head Gate Snapshot")
    if apply_gate_policy(snapshot, request.policy).pending_checks:
        target = WorkflowState.GATE_WAIT_PENDING
        fields = {"gate_snapshot_json": snapshot}
    else:
        result = assemble_result(
            request, judgment, snapshot, None, _timestamp(deps.now())
        )
        target = (
            WorkflowState.DELIVERY_PENDING
            if result.derived.outcome == SemanticOutcome.REVIEW_INCOMPLETE.value
            else WorkflowState.PUBLICATION_PENDING
        )
        fields = {
            "gate_snapshot_json": snapshot,
            "semantic_outcome": _semantic(result),
            "validated_result_json": canonicalize_result(result),
        }
    store.transition(
        request.request_id,
        WorkflowState.REVIEWING,
        target,
        now=deps.now(),
        **fields,
        next_attempt_at=deps.now(),
        last_error=None,
    )


def _reconcile_gate_wait(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps
) -> None:
    canonical_judgment = store.judgment_for_request(request.request_id)
    accepted_at = store.judgment_accepted_at(request.request_id)
    if canonical_judgment is None or accepted_at is None:
        raise ValueError("gate-wait request has no accepted judgment")
    current = deps.github.current_pr(request)
    if current is None:
        raise RuntimeError(
            "pull request could not be revalidated while waiting on gates"
        )
    if current.state != "OPEN" or current.head_sha != request.head_sha:
        _transition_system_incomplete(
            store,
            request,
            WorkflowState.GATE_WAIT_PENDING,
            request.gate_snapshot,
            _head_failure(request, current),
            deps,
        )
        return
    snapshot = deps.github.capture_gate_snapshot(request, _timestamp(deps.now()))
    if snapshot is None:
        raise RuntimeError("could not refresh the exact-head Gate Snapshot")
    assessment = apply_gate_policy(snapshot, request.policy)
    deadline = accepted_at + GATE_WAIT_SECONDS
    if assessment.pending_checks and deps.now() < deadline:
        store.checkpoint(
            request.request_id,
            WorkflowState.GATE_WAIT_PENDING,
            now=deps.now(),
            gate_snapshot_json=snapshot,
            next_attempt_at=min(deps.now() + GATE_POLL_SECONDS, deadline),
            last_error=None,
        )
        return
    if assessment.pending_checks:
        result = assemble_result(
            request,
            None,
            snapshot,
            SystemFailure(
                "GATE_WAIT_TIMEOUT",
                "Required checks did not finish within 30 minutes of judgment acceptance.",
                "Repost the PR link after the required checks finish.",
            ),
            _timestamp(deps.now()),
        )
        target = WorkflowState.DELIVERY_PENDING
    else:
        judgment = validate_judgment(json.loads(canonical_judgment))
        result = assemble_result(
            request, judgment, snapshot, None, _timestamp(deps.now())
        )
        target = (
            WorkflowState.DELIVERY_PENDING
            if result.derived.outcome == SemanticOutcome.REVIEW_INCOMPLETE.value
            else WorkflowState.PUBLICATION_PENDING
        )
    store.transition(
        request.request_id,
        WorkflowState.GATE_WAIT_PENDING,
        target,
        now=deps.now(),
        gate_snapshot_json=snapshot,
        semantic_outcome=_semantic(result),
        validated_result_json=canonicalize_result(result),
        next_attempt_at=deps.now(),
        last_error=None,
    )


def _reconcile_publication(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps
) -> None:
    prepared = False
    if request.publication_attempts == 0:
        current = deps.github.current_pr(request)
        if current is None:
            raise RuntimeError(
                "pull request could not be revalidated before publication"
            )
        if current.state != "OPEN" or current.head_sha != request.head_sha:
            _transition_system_incomplete(
                store,
                request,
                WorkflowState.PUBLICATION_PENDING,
                request.gate_snapshot,
                _head_failure(request, current),
                deps,
            )
            return
        canonical_judgment = store.judgment_for_request(request.request_id)
        if canonical_judgment is None:
            raise ValueError("publication request has no accepted judgment")
        snapshot = deps.github.capture_gate_snapshot(request, _timestamp(deps.now()))
        if snapshot is None:
            raise RuntimeError("could not refresh the exact-head Gate Snapshot")
        assessment = apply_gate_policy(snapshot, request.policy)
        if assessment.pending_checks:
            accepted_at = store.judgment_accepted_at(request.request_id)
            if accepted_at is None:
                raise ValueError("publication request has no judgment acceptance time")
            deadline = accepted_at + GATE_WAIT_SECONDS
            if deps.now() < deadline:
                store.checkpoint(
                    request.request_id,
                    WorkflowState.PUBLICATION_PENDING,
                    now=deps.now(),
                    gate_snapshot_json=snapshot,
                    next_attempt_at=min(deps.now() + GATE_POLL_SECONDS, deadline),
                    last_error=None,
                )
                return
            _transition_system_incomplete(
                store,
                request,
                WorkflowState.PUBLICATION_PENDING,
                snapshot,
                SystemFailure(
                    "GATE_WAIT_TIMEOUT",
                    "Required checks did not finish within 30 minutes of judgment acceptance.",
                    "Repost the PR link after the required checks finish.",
                ),
                deps,
            )
            return
        judgment = validate_judgment(json.loads(canonical_judgment))
        result = assemble_result(
            request, judgment, snapshot, None, _timestamp(deps.now())
        )
        if not store.checkpoint(
            request.request_id,
            WorkflowState.PUBLICATION_PENDING,
            now=deps.now(),
            gate_snapshot_json=snapshot,
            semantic_outcome=_semantic(result),
            validated_result_json=canonicalize_result(result),
            publication_attempts=1,
            next_attempt_at=deps.now(),
            last_error=None,
        ):
            return
        request = store.get_request(request.request_id)
        if request is None:
            return
        prepared = True
    result = _load_result(request)
    published = deps.github.find_review(request)
    if published is None:
        if not prepared:
            if not store.checkpoint(
                request.request_id,
                WorkflowState.PUBLICATION_PENDING,
                now=deps.now(),
                publication_attempts=request.publication_attempts + 1,
            ):
                return
            request = store.get_request(request.request_id)
            if request is None:
                return
        published = deps.github.publish_review(request, render_github(result))
    marker = (
        f"<!-- boris-review request_id={request.request_id} "
        f"result_hash={result.result_hash} -->"
    )
    if marker not in published.body:
        raise RuntimeError("marker-matched GitHub review has a conflicting result hash")
    store.transition(
        request.request_id,
        WorkflowState.PUBLICATION_PENDING,
        WorkflowState.DELIVERY_PENDING,
        now=deps.now(),
        github_review_id=published.review_id,
        github_review_url=published.url,
        next_attempt_at=deps.now(),
        last_error=None,
    )


def _reconcile_delivery(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps
) -> None:
    result = _load_result(request)
    if result.derived.outcome != SemanticOutcome.REVIEW_INCOMPLETE.value:
        if not request.github_review_url:
            raise ValueError("judgment-bearing delivery has no confirmed GitHub review")
    terminal_text = render_slack(result, request.github_review_url)
    try:
        status = deps.slack.find_status(request)
        if status is None:
            if not store.checkpoint(
                request.request_id,
                WorkflowState.DELIVERY_PENDING,
                now=deps.now(),
                delivery_attempts=request.delivery_attempts + 1,
            ):
                return
            status_ts = deps.slack.replace_status(request, terminal_text)
        else:
            status_ts = status.message_ts
            if status.text != terminal_text:
                if not store.checkpoint(
                    request.request_id,
                    WorkflowState.DELIVERY_PENDING,
                    now=deps.now(),
                    delivery_attempts=request.delivery_attempts + 1,
                ):
                    return
                try:
                    deps.slack.update_status(request, status.message_ts, terminal_text)
                except SlackStatusMissing:
                    status_ts = deps.slack.replace_status(request, terminal_text)
        if (
            not request.terminal_status_confirmed
            or request.status_message_ts != status_ts
        ) and not store.checkpoint(
            request.request_id,
            WorkflowState.DELIVERY_PENDING,
            now=deps.now(),
            status_message_ts=status_ts,
            terminal_status_confirmed=True,
        ):
            return
        _reconcile_source_reaction(
            store,
            request,
            deps,
            delivered_request_id=request.request_id,
        )
    except SlackRootUnavailable:
        store.transition(
            request.request_id,
            WorkflowState.DELIVERY_PENDING,
            WorkflowState.SLACK_UNAVAILABLE,
            now=deps.now(),
            next_attempt_at=deps.now(),
            last_error="delivery: source Slack thread unavailable",
        )
        return
    store.transition(
        request.request_id,
        WorkflowState.DELIVERY_PENDING,
        WorkflowState.COMPLETE,
        now=deps.now(),
        status_message_ts=status_ts,
        next_attempt_at=deps.now(),
        last_error=None,
    )


def reconcile_one(
    store: Store,
    request_id: str,
    deps: ReconcilerDeps,
    cfg: Config | None = None,
) -> None:
    del cfg
    request = store.get_request(request_id)
    if request is None:
        return
    delivery = request.workflow_state is WorkflowState.DELIVERY_PENDING
    if request.workflow_state is WorkflowState.ADMITTED:
        store.transition(
            request.request_id,
            WorkflowState.ADMITTED,
            WorkflowState.STATUS_MESSAGE_PENDING,
            now=deps.now(),
            next_attempt_at=deps.now(),
        )
    elif request.workflow_state is WorkflowState.STATUS_MESSAGE_PENDING:
        _reconcile_status_pending(store, request, deps)
    elif request.workflow_state is WorkflowState.STARTING_REVIEW:
        _reconcile_starting(store, request, deps)
    elif request.workflow_state is WorkflowState.REVIEWING:
        _reconcile_reviewing(store, request, deps)
    elif request.workflow_state is WorkflowState.GATE_WAIT_PENDING:
        _reconcile_gate_wait(store, request, deps)
    elif request.workflow_state is WorkflowState.PUBLICATION_PENDING:
        _reconcile_publication(store, request, deps)
    elif request.workflow_state is WorkflowState.DELIVERY_PENDING:
        _reconcile_delivery(store, request, deps)
    if delivery:
        return
    updated = store.get_request(request_id)
    if (
        updated is not None
        and updated.status_message_ts is not None
        and updated.workflow_state
        not in {WorkflowState.COMPLETE, WorkflowState.SLACK_UNAVAILABLE}
    ):
        _reconcile_source_reaction(store, updated, deps)


def tick(
    store: Store,
    deps: ReconcilerDeps,
    cfg: Config | None = None,
    limit: int = 25,
) -> int:
    due = store.claim_due(deps.now(), limit)
    processed = 0
    for request in due:
        if not store.renew_claim(request.request_id, deps.now()):
            continue
        processed += 1
        try:
            reconcile_one(store, request.request_id, deps, cfg)
        except Exception as error:
            stage = {
                WorkflowState.ADMITTED: "admission",
                WorkflowState.STATUS_MESSAGE_PENDING: "status",
                WorkflowState.STARTING_REVIEW: "dispatch",
                WorkflowState.REVIEWING: "review",
                WorkflowState.GATE_WAIT_PENDING: "gate_wait",
                WorkflowState.PUBLICATION_PENDING: "publication",
                WorkflowState.DELIVERY_PENDING: "delivery",
            }.get(request.workflow_state, "lifecycle")
            attempt = store.error_count(request.request_id, stage) + 1
            backoff = min(
                RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                RETRY_MAX_SECONDS,
            )
            store.record_error(
                request.request_id,
                stage,
                str(error),
                deps.now() + backoff,
                now=deps.now(),
                attempt=attempt,
            )
        finally:
            store.release_claim(request.request_id)
    return processed
