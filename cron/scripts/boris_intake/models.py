"""Typed values shared across the intake pipeline.

Nothing here holds Slack free text except NormalizedEvent.text, which the gate
reads and never forwards. ReviewCommand is deliberately all-validated-scalars:
it is the only thing that crosses into lane construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
    permalink: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"

    @property
    def idempotency_key(self) -> str:
        return f"borisslack:{self.owner}/{self.repo}#{self.pr_number}@{self.head_sha}"


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
