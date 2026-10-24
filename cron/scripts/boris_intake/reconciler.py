"""Advance durable Boris review requests one lifecycle state at a time."""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Callable, Protocol

# APPENDED, never prepended. `slack_read` lives only under this directory and
# nowhere in the repo, so appending is enough to find it -- while prepending
# put the whole DEPLOYED tree ahead of the repo checkout for the rest of the
# process, and this module is imported at collection time by the boris_intake
# suite. Every sibling package not yet imported at that moment then resolved
# from `~/.hermes/scripts` instead of `cron/scripts`: in a combined
# `tests/cron/boris_intake/ tests/cron/pr_discovery/` run, pr_discovery's
# tests were exercising the DEPLOYED copy, and a defect introduced in the
# repo's copy could not fail them (verified: mutating
# `pr_discovery/main.py::AUTHOR` survived that run and failed a solo one).
# That is the identical mistake `pr_remediation/daemon.py::_bootstrap_sys_path`
# documents at length after it silently disabled boris_intake's own regression
# tests the same way. An appended entry can only supply a module nothing
# earlier on the path already provides, so it can never rebind a package a
# test runner has pointed somewhere else.
sys.path.append("/Users/ctao/.hermes/scripts")

from slack_read import ENV_PATH, parse_env, slack_call

from boris_intake.acceptance import JiraAcceptanceAdapter, helper_is_installed
from boris_intake.config import Config, load_config
from boris_intake.dispatch import KANBAN_DB, TERMINAL_STATUSES, KanbanIO
from boris_intake.github import GitHubIO
from boris_intake.models import (
    GateSnapshot,
    LifecycleRequest,
    PRInfo,
    PriorReview,
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
from boris_intake.slack_io import (
    SlackIO,
    SlackRootUnavailable,
    SlackStatusMissing,
)

GATE_WAIT_SECONDS = 30 * 60
GATE_POLL_SECONDS = 15
# A reviewer lane that stops making progress otherwise polls forever: eight
# requests once sat in REVIEWING for 162-330 minutes before a manual sweep.
# Successful reviews have never taken longer than 32 minutes end to end, so 90
# bounds the damage without cutting a slow-but-working reviewer short.
REVIEW_WAIT_SECONDS = 90 * 60
# A worker that bounces off the provider quota is requeued by the dispatcher, not
# finished, so the 90-minute clock restarts from the latest bounce. This caps how
# long one request can wait through a quota wall before failing honestly.
QUOTA_HOLD_MAX_SECONDS = 24 * 60 * 60
REVIEW_POLL_SECONDS = 15
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 5 * 60
LOOP_SECONDS = 5
STATE_DIR = Path("/Users/ctao/.hermes/state")
DB_PATH = STATE_DIR / "boris_intake.db"
HEARTBEAT = STATE_DIR / "boris_review_reconciler_heartbeat"


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
    def find_lane(
        self, request: LifecycleRequest, *, retry: bool = False
    ) -> str | None: ...
    def create_lane(
        self,
        request: LifecycleRequest,
        *,
        retry: bool = False,
        prior: PriorReview | None = None,
    ) -> str: ...
    def lane_status(self, lane_id: str) -> str | None: ...
    def lane_quota_hold(self, lane_id: str) -> float | None: ...
    def lane_resource_window(self, lane_id: str) -> tuple[float, float | None] | None: ...
    def lane_authentication_failed(self, lane_id: str) -> bool: ...
    def retire_lane(self, lane_id: str) -> None: ...


@dataclass(frozen=True)
class ReconcilerDeps:
    slack: SlackLifecycleAdapter
    github: GitHubLifecycleAdapter
    kanban: KanbanAdapter
    now: Callable[[], float]
    # Optional on purpose: with no adapter the reviewer simply sees no
    # acceptance evidence, which is how every review ran before this existed.
    acceptance: object | None = None


def _timestamp(now: float) -> str:
    return (
        datetime.fromtimestamp(now, tz=UTC).isoformat().replace("+00:00", "Z")
    )


def _active_text(request: LifecycleRequest, label: str) -> str:
    lines = [f"{label} — {request.slug} @ {request.head_sha[:7]}"]
    if request.policy.ignore_ci:
        lines.append("Policy: CI gate waived")
    return "\n".join(lines)


def _quota_hold_text(request: LifecycleRequest) -> str:
    return (
        _active_text(request, "WAITING ON MODEL QUOTA")
        + "\nThe reviewer's model provider is rate limited. The review resumes"
        " automatically when quota returns; there is no need to repost."
    )


def _refresh_active_status(
    request: LifecycleRequest, deps: ReconcilerDeps, text: str
) -> None:
    """Best-effort edit of the live status message while a request keeps waiting."""
    status = deps.slack.find_status(request)
    if status is None or status.text == text:
        return
    try:
        deps.slack.update_status(request, status.message_ts, text)
    except SlackStatusMissing:
        # The terminal update re-posts a missing status; a progress note is not
        # worth a second message in the thread.
        return


def _announce_quota_hold(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps, held_at: float
) -> None:
    """Tell the thread once per request that the review is waiting on quota. The
    status is edited before the note is recorded, so a Slack failure retries on
    the next poll while a success never turns the 15-second poll into a
    conversations.replies read per poll."""
    events = store.request_events(request.request_id)
    if any(event["kind"] == "quota_hold" for event in events):
        return
    try:
        _refresh_active_status(request, deps, _quota_hold_text(request))
    except SlackRootUnavailable:
        # The thread is gone; delivery deals with that when the request finishes.
        # Recording the note anyway keeps this from retrying on every poll.
        pass
    store.record_note(request.request_id, "quota_hold", {"held_at": held_at}, deps.now())


def _finish_reviewing(
    store: Store,
    request: LifecycleRequest,
    failure: SystemFailure,
    deps: ReconcilerDeps,
    lane_status: str,
) -> None:
    """End a review that produced no judgment, then retire its lane: a finished
    request must not leave a lane queued or bouncing (it would keep spending worker
    slots and land a judgment nobody consumes). Retirement is best effort, and a
    lane with a live worker is left to the dispatcher: archiving it would delete the
    worker's workspace under it."""
    _transition_system_incomplete(
        store, request, WorkflowState.REVIEWING, request.gate_snapshot, failure, deps
    )
    if request.lane_id is None or lane_status in ("running", "archived"):
        return
    try:
        deps.kanban.retire_lane(request.lane_id)
    except Exception as error:
        store.record_note(
            request.request_id,
            "lane_retire_failed",
            {"lane_id": request.lane_id, "error": str(error)[:300]},
            deps.now(),
        )


def _quota_failure(detail: str) -> SystemFailure:
    return SystemFailure(
        "PROVIDER_QUOTA_EXHAUSTED",
        detail,
        "Repost the PR link after the provider quota resets.",
    )


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


def _slack_only(result: StructuredResult) -> bool:
    """Incomplete reviews skip GitHub unless they carry findings worth publishing."""
    return (
        result.derived.outcome == SemanticOutcome.REVIEW_INCOMPLETE.value
        and result.derived.presentation != "INCOMPLETE_WITH_FINDINGS"
    )


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
        reaction = _aggregate_source_reaction(requests, delivered_request_id)
        if store.source_reaction(request.channel_id, request.message_ts) == reaction:
            return
        deps.slack.reconcile_source_reaction(
            request,
            reaction,
        )
        # The memo keys on the aggregate, not on what Slack shows: in a channel
        # Boris does not own SlackIO may substitute a Boris-only emoji for a
        # shared one, and the memo must still match the next tick's aggregate.
        store.record_source_reaction(request.request_id, reaction, deps.now())
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


def _authorize_acceptance(
    store: Store,
    request: LifecycleRequest,
    current: PRInfo,
    deps: ReconcilerDeps,
) -> None:
    """Pre-authorize the ticket this pull request claims to implement.

    Strictly best effort. Dispatch has already been decided by the time this
    runs, so no failure here may change whether the review happens.
    """
    if deps.acceptance is None:
        return
    try:
        for reference, evidence in deps.acceptance.fetch(
            current.title, current.head_ref
        ):
            store.authorize_acceptance_evidence(
                request.request_id, reference, evidence
            )
    except Exception as error:
        store.record_note(
            request.request_id,
            "acceptance_evidence_unavailable",
            {"error": str(error)[:200]},
            deps.now(),
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
    _authorize_acceptance(store, request, current, deps)
    lane_id = deps.kanban.find_lane(request)
    if lane_id is None:
        lane_id = deps.kanban.create_lane(request, prior=store.prior_review(request))
    elif store.request_for_lane(lane_id) is not None:
        lane_id = deps.kanban.find_lane(request, retry=True)
        if lane_id is None:
            lane_id = deps.kanban.create_lane(
                request, retry=True, prior=store.prior_review(request)
            )
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
        if status is None:
            raise RuntimeError("reviewer lane could not be found")
        if deps.kanban.lane_authentication_failed(request.lane_id):
            _finish_reviewing(
                store, request,
                SystemFailure(
                    "PROVIDER_AUTHENTICATION_REQUIRED",
                    "The reviewer could not authenticate with its model provider.",
                    "Reauthenticate with `hermes --profile boris_on_demand model`, "
                    "then repost the PR link.",
                ),
                deps, status,
            )
            return
        held_at = deps.kanban.lane_quota_hold(request.lane_id)
        recently_held = (
            held_at is not None and deps.now() - held_at < REVIEW_WAIT_SECONDS
        )
        if status in TERMINAL_STATUSES:
            if recently_held:
                # A bouncing lane can still be pushed to blocked (a lost exit status
                # books a bounce as a crash); telling the thread to repost would only
                # queue another bounce against the same wall.
                failure = _quota_failure(
                    "The reviewer lane stopped while the model provider quota was"
                    " exhausted."
                )
            else:
                failure = SystemFailure(
                    "LANE_NO_VALID_RESULT",
                    "The reviewer lane finished without a valid structured result.",
                    "Repost the PR link to start a new review attempt.",
                )
            _finish_reviewing(store, request, failure, deps, status)
            return
        deadline = request.created_at + REVIEW_WAIT_SECONDS
        if held_at is not None:
            # Requeued after a quota bounce: wait from the latest bounce, within the
            # cap. Later crashes do not end the hold (see lane_quota_hold); recency
            # bounds it instead.
            deadline = min(
                max(deadline, held_at + REVIEW_WAIT_SECONDS),
                request.created_at + QUOTA_HOLD_MAX_SECONDS,
            )
        resource_window = deps.kanban.lane_resource_window(request.lane_id)
        resource_waiting = resource_window is not None and resource_window[1] is None
        if resource_window is not None:
            resumed_at = resource_window[1]
            deadline = min(
                max(deadline, (deps.now() if resumed_at is None else resumed_at) + REVIEW_WAIT_SECONDS),
                request.created_at + QUOTA_HOLD_MAX_SECONDS,
            )
        if deps.now() >= deadline:
            if resource_waiting:
                failure = SystemFailure(
                    "LOCAL_RESOURCE_WAIT_EXPIRED",
                    "The reviewer could not start within the 24-hour local capacity wait limit.",
                    "Restore local disk capacity, then repost the PR link.",
                )
            elif recently_held:
                failure = _quota_failure(
                    "The reviewer's model provider quota stayed exhausted for the"
                    " whole hold window."
                )
            else:
                failure = SystemFailure(
                    "REVIEW_LANE_TIMEOUT",
                    "The reviewer lane did not produce a result within 90 minutes.",
                    "Repost the PR link to start a new review attempt.",
                )
            _finish_reviewing(store, request, failure, deps, status)
            return
        if held_at is not None:
            # After the deadline check, so a Slack problem can never keep a request
            # from finishing; tells the thread not to repost.
            _announce_quota_hold(store, request, deps, held_at)
        store.checkpoint(
            request.request_id,
            WorkflowState.REVIEWING,
            now=deps.now(),
            next_attempt_at=min(deps.now() + REVIEW_POLL_SECONDS, deadline),
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
            if _slack_only(result)
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
            if _slack_only(result)
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


def _door_markers(cfg: Config | None) -> tuple[str, ...]:
    """Every marker literal `gate.py` admits a bot message on.

    One list, because the thing that makes a marker dangerous in a verdict is
    that the gate keys a door on it -- so anything `_is_loop_repost` or
    `_is_discovery_post` tests against belongs here, and a new door added to
    the gate without a line here is a door Boris's own text can knock on. See
    `result.neutralize_door_markers`.

    `None` yields `()`: a caller with no configuration has no configured
    marker, hence no door, hence nothing to neutralise.
    """
    if cfg is None:
        return ()
    return (cfg.loop_marker, cfg.discovery_marker)


def _reconcile_delivery(
    store: Store, request: LifecycleRequest, deps: ReconcilerDeps,
    cfg: Config | None = None,
) -> None:
    result = _load_result(request)
    if result.derived.outcome != SemanticOutcome.REVIEW_INCOMPLETE.value:
        if not request.github_review_url:
            raise ValueError("judgment-bearing delivery has no confirmed GitHub review")
    # The one message this service composes out of model-authored text about an
    # untrusted pull request, posted into a thread by the same bot user the
    # gate's loop door admits. `cfg` is threaded this far for that reason
    # alone -- see `_door_markers`.
    terminal_text = render_slack(result, request.github_review_url,
                                 _door_markers(cfg))
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
        _reconcile_delivery(store, request, deps, cfg)
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


def build_deps(
    cfg: Config,
    token: str,
    *,
    slack_caller: Callable = slack_call,
    kanban_db: Path = Path(KANBAN_DB),
    now: Callable[[], float] = time.time,
) -> ReconcilerDeps:
    return ReconcilerDeps(
        # Boris fully owns source-message reactions only in its passive channels;
        # elsewhere (just-do-it, mention-only) `blinkapproved` is also the
        # auto-approve stamp and must survive reconciliation.
        slack=SlackIO(
            slack_caller,
            token,
            dry_run=False,
            owned_channels=frozenset(cfg.passive_channels),
        ),
        github=GitHubIO(cfg.github_token_path),
        kanban=KanbanIO(kanban_db),
        now=now,
        # Absent helper means absent acceptance evidence, not a broken daemon.
        acceptance=JiraAcceptanceAdapter() if helper_is_installed() else None,
    )


def run_once(
    store: Store,
    deps: ReconcilerDeps,
    cfg: Config,
    *,
    heartbeat_path: Path = HEARTBEAT,
    limit: int = 25,
) -> int:
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(f"{deps.now():.0f}\n", encoding="utf-8")
    if cfg.mode != "live":
        return 0
    return tick(store, deps, cfg, limit=limit)


def run_service(
    store: Store,
    deps: ReconcilerDeps,
    cfg: Config,
    *,
    stop: threading.Event,
    heartbeat_path: Path = HEARTBEAT,
    interval_seconds: float = LOOP_SECONDS,
) -> None:
    while not stop.is_set():
        run_once(store, deps, cfg, heartbeat_path=heartbeat_path)
        stop.wait(interval_seconds)


def main() -> int:
    cfg = load_config()
    token = parse_env(ENV_PATH).get("TICKET_HUB_SLACK_BOT_TOKEN", "")
    if not token:
        sys.stderr.write(
            "boris_review_reconciler: TICKET_HUB_SLACK_BOT_TOKEN missing from .env\n"
        )
        return 2

    store = Store(DB_PATH)
    deps = build_deps(cfg, token)
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    for caught_signal in (signal.SIGTERM, signal.SIGINT):
        signal.signal(caught_signal, request_stop)
    sys.stderr.write(f"boris_review_reconciler: started, mode={cfg.mode}\n")
    sys.stderr.flush()
    run_service(store, deps, cfg, stop=stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
