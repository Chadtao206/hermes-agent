from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict

SUBTYPE_SUCCESS = "success"
SUBTYPE_MAX_TURNS = "error_max_turns"
SUBTYPE_BUDGET = "error_budget"
SUBTYPE_NO_TRANSCRIPT = "error_transcript_unavailable"


@dataclass
class TurnResult:
    result: str
    session_id: str
    num_turns: int
    total_cost_usd: float
    usage: Dict[str, Any]
    subtype: str = SUBTYPE_SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "result", "subtype": self.subtype, "result": self.result,
            "session_id": self.session_id, "num_turns": self.num_turns,
            "total_cost_usd": self.total_cost_usd, "usage": self.usage,
        }


@dataclass
class SessionRecord:
    name: str
    uuid: str
    pid: int
    workdir: str
    deadline: float
    turns: int
    cost: float
    started_at: float
    log_path: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionRecord":
        return cls(**d)
