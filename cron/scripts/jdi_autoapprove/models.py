"""Typed values for the auto-approve pipeline.

Reuses boris_intake's NormalizedEvent / Identity for the Slack side; only the
terminal command and the reason vocabulary differ, because this pipeline ends
in a GitHub approval rather than a kanban lane.

ApproveCommand is all-validated-scalars: no Slack free text crosses into it,
which is what keeps message content structurally incapable of steering the
GitHub call.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReasonCode(str, Enum):
    OK = "ok"
    MODE_OFF = "mode_off"
    NOT_HUMAN = "not_human"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    NO_PR_URL = "no_pr_url"
    IDENTITY_LOOKUP_FAILED = "identity_lookup_failed"
    IDENTITY_NOT_TRUSTED = "identity_not_trusted"
    ORG_NOT_ALLOWED = "org_not_allowed"
    PR_NOT_FOUND = "pr_not_found"
    PR_NOT_OPEN = "pr_not_open"
    DUPLICATE = "duplicate"
    ALREADY_APPROVED = "already_approved"
    SELF_AUTHORED = "self_authored"
    RATE_LIMITED = "rate_limited"
    DAILY_CAP = "daily_cap"


@dataclass(frozen=True)
class PRFacts:
    """Authoritative PR state. head_sha never comes from Slack."""
    head_sha: str
    author: str
    state: str                  # "OPEN" | "CLOSED" | "MERGED"
    review_decision: str        # "APPROVED" | "REVIEW_REQUIRED" | "CHANGES_REQUESTED" | ""


@dataclass(frozen=True)
class ApproveCommand:
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    pr_author: str
    requester_user_id: str
    requester_email: str
    channel_id: str
    message_ts: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"

    @property
    def idempotency_key(self) -> str:
        # Scoped to the head SHA: re-posting the same link is a no-op, but a
        # genuinely new commit is a different key and earns its own stamp.
        # Namespaced so it can never collide with a boris_intake lane key.
        return f"jdiapprove:{self.owner}/{self.repo}#{self.pr_number}@{self.head_sha}"


@dataclass(frozen=True)
class Rejection:
    reason: ReasonCode
    detail: str = ""


@dataclass(frozen=True)
class GateResult:
    commands: tuple[ApproveCommand, ...] = ()
    rejections: tuple[Rejection, ...] = ()

    @property
    def accepted(self) -> bool:
        return bool(self.commands)
