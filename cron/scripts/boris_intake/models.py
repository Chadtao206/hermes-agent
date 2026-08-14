"""Typed values shared across the intake pipeline.

Nothing here holds Slack free text except NormalizedEvent.text, which the gate
reads and never forwards. ReviewCommand is deliberately all-validated-scalars:
it is the only thing that crosses into lane construction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReasonCode(str, Enum):
    OK = "ok"
    NOT_HUMAN = "not_human"
    THREAD_REPLY_NO_MENTION = "thread_reply_no_mention"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    NO_PR_URL = "no_pr_url"
    MENTION_REQUIRED = "mention_required"
    MODE_OFF = "mode_off"
    IDENTITY_LOOKUP_FAILED = "identity_lookup_failed"
    IDENTITY_NOT_TRUSTED = "identity_not_trusted"
    ORG_NOT_ALLOWED = "org_not_allowed"
    PR_NOT_FOUND = "pr_not_found"
    PR_NOT_OPEN = "pr_not_open"
    UNKNOWN_FLAG = "unknown_flag"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"


#: Rejections that must NOT produce a Slack reply. Replying to these would make
#: the bot noisy in channels it should not act in, or confirm its presence to a
#: prober. Everything else gets one throttled in-thread explanation.
SILENT_REASONS = frozenset({
    ReasonCode.NOT_HUMAN,
    ReasonCode.THREAD_REPLY_NO_MENTION,
    ReasonCode.CHANNEL_NOT_ALLOWED,
    ReasonCode.NO_PR_URL,
    ReasonCode.MENTION_REQUIRED,
    ReasonCode.MODE_OFF,
})


@dataclass(frozen=True)
class NormalizedEvent:
    channel_id: str
    channel_type: str          # "channel" | "group" | "im"
    user_id: str | None
    ts: str
    thread_ts: str | None
    text: str
    is_bot: bool
    subtype: str | None


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str
    team_id: str
    is_bot: bool
    deleted: bool
    is_restricted: bool
    is_ultra_restricted: bool


@dataclass(frozen=True)
class PRInfo:
    head_sha: str
    author: str
    state: str                 # "OPEN" | "CLOSED" | "MERGED"


@dataclass(frozen=True)
class ReviewPolicy:
    ignore_ci: bool
    rubric_version: str = "2"

    def canonical_json(self) -> str:
        return json.dumps(
            {"ignore_ci": self.ignore_ci, "rubric_version": self.rubric_version},
            separators=(",", ":"),
        )

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode()).hexdigest()
        return f"sha256:{digest}"


@dataclass(frozen=True)
class GateCheck:
    check_name: str
    check_class: str
    status: str
    conclusion: str
    url: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "class": self.check_class,
            "status": self.status,
            "conclusion": self.conclusion,
            "url": self.url,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GateCheck:
        return cls(
            check_name=str(value["check_name"]),
            check_class=str(value["class"]),
            status=str(value["status"]),
            conclusion=str(value["conclusion"]),
            url=str(value["url"]),
            required=bool(value.get("required", True)),
        )


@dataclass(frozen=True)
class GateSnapshot:
    fingerprint: str
    captured_at: str
    checks: tuple[GateCheck, ...] = ()

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "captured_at": self.captured_at,
                "checks": [check.to_dict() for check in self.checks],
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> GateSnapshot:
        raw = json.loads(value)
        return cls(
            fingerprint=str(raw["fingerprint"]),
            captured_at=str(raw["captured_at"]),
            checks=tuple(GateCheck.from_dict(check) for check in raw["checks"]),
        )


class WorkflowState(str, Enum):
    ADMITTED = "admitted"
    STATUS_MESSAGE_PENDING = "status_message_pending"
    STARTING_REVIEW = "starting_review"
    REVIEWING = "reviewing"
    GATE_WAIT_PENDING = "gate_wait_pending"
    PUBLICATION_PENDING = "publication_pending"
    DELIVERY_PENDING = "delivery_pending"
    COMPLETE = "complete"
    SLACK_UNAVAILABLE = "slack_unavailable"


class SemanticOutcome(str, Enum):
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    ISSUES_FOUND = "ISSUES_FOUND"
    REVIEW_INCOMPLETE = "REVIEW_INCOMPLETE"


@dataclass(frozen=True)
class ReviewCommand:
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    pr_author: str
    requester_user_id: str
    requester_email: str
    channel_id: str
    message_ts: str
    root_thread_ts: str
    permalink: str
    policy: ReviewPolicy

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"

    @property
    def idempotency_key(self) -> str:
        return (
            f"borisslack:{self.owner}/{self.repo}#{self.pr_number}@{self.head_sha}"
            f":policy={self.policy.fingerprint()}"
        )


@dataclass(frozen=True)
class LifecycleRequest:
    request_id: str
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    pr_author: str
    policy: ReviewPolicy
    policy_fingerprint: str
    gate_snapshot: GateSnapshot
    requester_user_id: str
    requester_email: str
    channel_id: str
    message_ts: str
    root_thread_ts: str
    permalink: str
    workflow_state: WorkflowState
    semantic_outcome: SemanticOutcome | None
    status_message_ts: str | None
    lane_id: str | None
    validated_result_json: str | None
    github_review_id: str | None
    github_review_url: str | None
    publication_attempts: int
    delivery_attempts: int
    next_attempt_at: float
    last_error: str | None
    created_at: float
    updated_at: float
    completed_at: float | None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"

    @property
    def review_identity(self) -> str:
        return (
            f"borisslack:{self.owner}/{self.repo}#{self.pr_number}@{self.head_sha}"
            f":policy={self.policy_fingerprint}"
        )


@dataclass(frozen=True)
class AdmissionResult:
    admitted: tuple[LifecycleRequest, ...] = ()
    duplicates: tuple[LifecycleRequest, ...] = ()
    redelivered: bool = False


@dataclass(frozen=True)
class Rejection:
    reason: ReasonCode
    detail: str = ""

    @property
    def silent(self) -> bool:
        return self.reason in SILENT_REASONS


@dataclass(frozen=True)
class GateResult:
    commands: tuple[ReviewCommand, ...] = ()
    rejections: tuple[Rejection, ...] = ()

    @property
    def accepted(self) -> bool:
        return bool(self.commands)
