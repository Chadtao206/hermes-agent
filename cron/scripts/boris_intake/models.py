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
    #: A GitHub read did not answer. Distinct from PR_NOT_FOUND on purpose: that
    #: one asserts the pull request does not exist, which is terminal, and
    #: asserting it about a PR that is open strands whoever asked (see
    #: GitHubReadStatus).
    GITHUB_UNAVAILABLE = "github_unavailable"
    GATE_SNAPSHOT_UNAVAILABLE = "gate_snapshot_unavailable"
    #: GitHub answered, authoritatively, that the pull request's branch has no
    #: checks at all, so no exact-head Gate Snapshot exists to capture. Terminal
    #: and NOT retryable, and deliberately not GITHUB_UNAVAILABLE: GitHub
    #: answered perfectly, and saying otherwise is the exact lie this whole
    #: classification exists to remove. Whether a CI-less repository should be
    #: reviewable at all is a product decision, so the request stays rejected --
    #: see `SnapshotStatus.NO_CHECKS`.
    PR_NO_CHECKS = "pr_no_checks"
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

#: Rejections that must be delivered even inside the reply cooldown window.
#:
#: These say "the machinery failed", not "your request was wrong", and nobody
#: can act on one they never see: on 2026-08-17 a GitHub partial outage rejected
#: a live re-review request and the one reply that would have shown it was
#: swallowed by a cooldown row keyed on the bot's own user id, so three
#: remediation loops sat waiting for a verdict for hours. They are self-limiting
#: -- they only occur while GitHub is failing, and the loop's own re-post budget
#: is 12 attempts 10 minutes apart -- so bypassing the throttle cannot turn into
#: a spam channel the way a human pasting bad links could.
#:
#: GATE_SNAPSHOT_UNAVAILABLE is deliberately NOT here, and was removed once the
#: three-state capture split its two meanings apart. Its only remaining producer
#: is the moved-head refusal (`daemon.py`, `replay.py`), which is not an
#: infrastructure failure at all: the requester pushed while requesting, so it
#: is "your request was wrong" in the most literal way this service sees. An
#: un-throttled reply on every post for a self-inflicted race is the anti-noise
#: property this cooldown exists to protect, so a moved head is throttled like
#: any other ordinary rejection. Everything a requester caused belongs outside
#: this set; `test_gate.py` pins the membership in both directions.
ALWAYS_REPLY_REASONS = frozenset({
    ReasonCode.GITHUB_UNAVAILABLE,
})


class RequestOrigin(str, Enum):
    """Who a message is admitted AS -- not who posted it.

    The remediation loop and the discovery cron both post as the same bot user
    and are both attributed to `cfg.loop_attributed_user_id` (Chad) for quotas
    and audit. That attribution alone cannot key the rejection-reply cooldown:
    it would put the loop's outage rejections in the same row as Chad's own
    typos, so one bad link he pastes would silence the loop for an hour. The
    origin is the other half of that key.
    """

    HUMAN = "human"
    LOOP = "loop"
    DISCOVERY = "discovery"


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
    # Only used to find the ticket a pull request claims to implement, so both
    # stay optional: an older caller that omits them just finds no ticket.
    title: str = ""
    head_ref: str = ""


class GitHubReadStatus(str, Enum):
    """Why a GitHub read answered the way it did. Total and distinct.

    The whole point of this type is the difference between the last two. Before
    it existed, every failure of `github.fetch_pr` -- a 503, a 30s timeout, a
    wrapper refusal, unparseable JSON -- collapsed into the same `None` the gate
    then reported as `PR_NOT_FOUND`, i.e. "that pull request does not exist". On
    2026-08-17 GitHub answered 503 on roughly half of all calls for several
    hours and that is exactly what happened to `Ylopo/forge-content-service#111`
    while it was open: a terminal rejection asserting something false about the
    world, which stranded the remediation loop waiting for a verdict that could
    never arrive.

    ABSENT is therefore reserved for a POSITIVE match on GitHub's own not-found
    shape, and everything unclassifiable is UNAVAILABLE. A false UNAVAILABLE
    only says "could not check" -- honest, non-terminal, recoverable by
    re-posting the link. A false ABSENT lies and strands. When in doubt, be
    UNAVAILABLE; do not tidy this the other way.

    NO_CHECKS is the one answer that is neither a payload nor a failure: GitHub
    said, authoritatively, that the branch has no checks. Only a `pr checks`
    read can produce it (`pr view` is never asked the question), and it is
    UNAVAILABLE's opposite -- a perfectly good answer that simply is not a
    snapshot. It is kept out of FOUND on purpose: `FOUND, value=[]` would build
    a Gate Snapshot with zero checks, and nothing blocking plus nothing pending
    would admit a pull request whose CI never ran.
    """

    FOUND = "found"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    NO_CHECKS = "no_checks"


@dataclass(frozen=True)
class PRLookup:
    """The three-state result of one `pr view` read.

    Retry-eligibility is a second axis, and it lives where it is acted on --
    `github._Read.retryable`, which is the only thing `github._read_json`
    consults. It is deliberately NOT restated here: no consumer of this type
    ever read it, and on two of the paths that used to set it (a body that is
    not a pull request object, a body with no usable head SHA) it was also
    false, because those checks run after the retry loop has already returned.
    A flag that is unread and wrong is worse than no flag.
    """

    status: GitHubReadStatus
    info: PRInfo | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.info is not None) is not (self.status is GitHubReadStatus.FOUND):
            raise ValueError("a PRLookup carries a PRInfo exactly when it is FOUND")
        if self.status is GitHubReadStatus.NO_CHECKS:
            # `pr view` is never asked about checks, so this status cannot be
            # reached here -- and a PR lookup that claimed it would mean the
            # no-checks prefix had been wired onto the wrong read.
            raise ValueError("a pull request view read is never NO_CHECKS")

    @property
    def found(self) -> bool:
        return self.status is GitHubReadStatus.FOUND

    @property
    def absent(self) -> bool:
        return self.status is GitHubReadStatus.ABSENT

    @property
    def unavailable(self) -> bool:
        return self.status is GitHubReadStatus.UNAVAILABLE


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


class SnapshotStatus(str, Enum):
    """Why a Gate Snapshot capture answered the way it did.

    HEAD_MOVED is its own state because it is the one `None` in
    `github.fetch_gate_snapshot` that was never an outage: the pull request
    moved under the request, which is terminal, correct, retry-proof, and the
    commonest legitimate reason a capture refuses. Reporting it as GitHub being
    unavailable would be a fresh lie pointing the opposite way, and retrying it
    would slow the loop's commonest legitimate path down for nothing.

    NO_CHECKS is the second refusal that was never an outage, and it used to be
    reported as one: `gh pr checks --json ...` answers exit 1 with empty stdout
    and `no checks reported on the '<branch>' branch` for a pull request whose
    repository has no CI, which fell through to `json.loads("")` and read as
    UNAVAILABLE-retryable. That said something false about the world (GitHub
    answered), replied on every post (GITHUB_UNAVAILABLE bypasses the reply
    cooldown), and spent three spawns retrying a deterministic answer. It is
    terminal, never retried, and keeps the request rejected: admitting it would
    build a snapshot with zero checks, which nothing blocking and nothing
    pending would publish as READY_FOR_HUMAN_REVIEW on a pull request whose CI
    never ran. Whether a CI-less repository is reviewable is a product decision,
    not a side effect of classifying a read.
    """

    CAPTURED = "captured"
    HEAD_MOVED = "head_moved"
    NO_CHECKS = "no_checks"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class GateSnapshotLookup:
    """The four-state result of one exact-head Gate Snapshot capture.

    Retry-eligibility is not restated here either, for the reason `PRLookup`
    gives: `github._read_json` acts on `_Read.retryable` and nothing outside it
    ever read a lookup's copy.
    """

    status: SnapshotStatus
    snapshot: GateSnapshot | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.snapshot is not None) is not (self.status is SnapshotStatus.CAPTURED):
            raise ValueError(
                "a GateSnapshotLookup carries a snapshot exactly when it is CAPTURED"
            )

    @property
    def captured(self) -> bool:
        return self.status is SnapshotStatus.CAPTURED

    @property
    def head_moved(self) -> bool:
        return self.status is SnapshotStatus.HEAD_MOVED

    @property
    def no_checks(self) -> bool:
        return self.status is SnapshotStatus.NO_CHECKS

    @property
    def unavailable(self) -> bool:
        return self.status is SnapshotStatus.UNAVAILABLE


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
    terminal_status_confirmed: bool
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
class SlackStatus:
    message_ts: str
    text: str


@dataclass(frozen=True)
class PublishedReview:
    review_id: str
    url: str
    body: str


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
    #: Who this message is admitted AS, and through which door -- the same
    #: attribution every audit row and every quota already uses. Surfaced here
    #: because `daemon._maybe_reply_rejections` needs it to key the
    #: rejection-reply cooldown on the requester rather than on whichever Slack
    #: account happened to post (for a loop re-post, that is the bot itself, so
    #: every loop rejection for every PR shared one row).
    #:
    #: Both default to a bare human with no id, which is what the rejections
    #: raised before attribution is computed (mode, not-human, channel) are --
    #: all three of which are silent and never reach the throttle.
    requester_user_id: str = ""
    origin: RequestOrigin = RequestOrigin.HUMAN

    @property
    def accepted(self) -> bool:
        return bool(self.commands)

    @property
    def reply_cooldown_key(self) -> str:
        """The row a rejection reply about THIS message throttles against.

        Origin AND requester, because either alone reintroduces cross-talk:

        * `event.user_id` alone was the bug -- the loop posts as the bot, so
          every loop rejection for every pull request shared one row with every
          other bot rejection, and one reply silenced them all for an hour.
        * the attributed requester alone puts the loop in Chad's row, so a bad
          link he pastes silences the loop's outage rejection instead.

        Deliberately NOT per pull request. Only one reply is sent per message
        whatever it contains, so a per-PR key would not make a single message
        louder -- it would let one 40-link paste earn 40 replies an hour, which
        is the spam this cooldown exists to prevent. Infrastructure rejections
        do not consult this key at all (see `ALWAYS_REPLY_REASONS`).
        """
        return f"{self.origin.value}:{self.requester_user_id}"
