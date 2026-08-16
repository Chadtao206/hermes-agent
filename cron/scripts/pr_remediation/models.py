"""Types for the Boris remediation loop."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LoopState(str, Enum):
    OBSERVED = "observed"
    REMEDIATING = "remediating"
    AWAITING_HEAD_MOVE = "awaiting_head_move"
    REREVIEW_POSTED = "rereview_posted"
    READY = "ready"
    ESCALATED = "escalated"
    # The pull request stopped being open -- merged or closed by a human --
    # so there is nothing left to remediate and nobody to tell. Terminal, and
    # deliberately neither READY (which notifies) nor ESCALATED (which claims a
    # human must intervene); a merge is the ordinary end of a pull request, not
    # an incident.
    STALE = "stale"


class EscalationReason(str, Enum):
    ROUND_CAP = "round_cap"
    NO_OP_LANE = "no_op_lane"
    MODEL_INCOMPLETE = "model_incomplete"
    CONFLICT_NON_TRIVIAL = "conflict_non_trivial"
    LANE_FAILED = "lane_failed"


TERMINAL_STATES = frozenset({LoopState.READY, LoopState.ESCALATED,
                             LoopState.STALE})


@dataclass(frozen=True)
class LoopRequest:
    loop_id: str
    owner: str
    repo: str
    pr_number: int
    generation: int
    head_sha: str
    boris_request_id: str
    root_thread_ts: str
    # Where Boris's status message lives -- the anchor for the thinkies
    # reaction. Distinct from root_thread_ts (see loop.py's
    # _clear_running_signal / _open_lane for why the two must never be
    # unified). Nullable: the source column is nullable, and every loop
    # opened before this field existed carries no anchor either.
    status_message_ts: str | None
    state: LoopState
    round_count: int
    lane_id: str | None
    escalation_reason: str | None
    created_at: float
    updated_at: float

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"
