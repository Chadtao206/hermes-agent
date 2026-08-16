"""Read-only view of Boris verdicts.

Opened with SQLite's read-only URI so this process cannot write to the review
database even by accident — the review path's integrity does not depend on this
service behaving.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pr_remediation.config import RemediationConfig


@dataclass(frozen=True)
class Verdict:
    request_id: str
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    root_thread_ts: str
    outcome: str
    incomplete_origin: str | None
    blocking_issues: tuple[str, ...]

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"


class BorisSource:
    def __init__(self, db_path: Path, cfg: RemediationConfig):
        self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self._cfg = cfg

    def close(self) -> None:
        self._db.close()

    def new_verdicts(self, limit: int = 50) -> tuple[Verdict, ...]:
        cur = self._db.execute(
            "SELECT request_id, owner, repo, pr_number, head_sha, root_thread_ts, "
            "semantic_outcome, validated_result_json FROM review_requests "
            "WHERE workflow_state='complete' AND semantic_outcome IS NOT NULL "
            "AND requester_user_id=? AND pr_author=? "
            "ORDER BY completed_at DESC LIMIT ?",
            (self._cfg.chad_slack_id, self._cfg.chad_github_login, limit),
        )
        return tuple(self._verdict(row) for row in cur.fetchall())

    def _verdict(self, row: sqlite3.Row) -> Verdict:
        try:
            payload = json.loads(row["validated_result_json"] or "{}")
        except ValueError:
            payload = {}
        incomplete = payload.get("incomplete") or None
        issues = payload.get("blocking_issues") or []
        return Verdict(
            request_id=row["request_id"], owner=row["owner"], repo=row["repo"],
            pr_number=row["pr_number"], head_sha=row["head_sha"],
            root_thread_ts=row["root_thread_ts"],
            outcome=row["semantic_outcome"],
            incomplete_origin=(incomplete or {}).get("origin"),
            blocking_issues=tuple(
                str(i.get("title", "")) for i in issues if isinstance(i, dict)
            ),
        )
