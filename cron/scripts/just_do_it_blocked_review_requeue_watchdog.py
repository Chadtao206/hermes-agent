"""Deterministic helpers for blocked just-do-it review discovery."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


DEFAULT_ALLOWED_CHANNELS = {"C08K8FQKX9S"}
PR_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
PERMALINK_RE = re.compile(
    r"https://ylopo\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p\d{16}",
    re.I,
)


def canonical_pr_url(value: str) -> str | None:
    """Return the canonical GitHub pull-request URL found in *value*."""
    if not isinstance(value, str):
        return None
    match = PR_RE.search(value)
    if not match:
        return None
    return (
        f"https://github.com/{match.group('owner')}/{match.group('repo')}"
        f"/pull/{match.group('number')}"
    )


def _first_string(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _normalized_findings(value: Any) -> list[dict[str, Any] | str]:
    values = value if isinstance(value, list) else [value]
    normalized: list[dict[str, Any] | str] = []
    for finding in values:
        if isinstance(finding, str):
            normalized.append(finding)
        elif isinstance(finding, dict):
            try:
                json.dumps(finding)
            except (TypeError, ValueError):
                continue
            normalized.append(finding)
    return normalized


def _completion_timestamp(run: dict[str, Any]) -> str | None:
    timestamp = _first_string(run, "ended_at", "completed_at")
    if not timestamp:
        return None
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None

    for field in ("status", "outcome"):
        if field in run and (
            not isinstance(run[field], str)
            or run[field].strip().lower() not in {"completed", "done"}
        ):
            return None
    return timestamp


def blocked_candidate(
    show: dict[str, Any], allowed_channels: set[str]
) -> dict[str, Any] | None:
    """Return the newest structured blocking-review run, if one exists."""
    runs = show.get("runs")
    if not isinstance(runs, list):
        return None

    newest_first = sorted(
        (run for run in runs if isinstance(run, dict)),
        key=lambda run: _completion_timestamp(run) or "",
        reverse=True,
    )
    for run in newest_first:
        completed_at = _completion_timestamp(run)
        if not completed_at:
            continue
        run_metadata = run.get("metadata")
        if not isinstance(run_metadata, dict):
            continue
        metadata = {**run, **run_metadata}

        pr_url = canonical_pr_url(_first_string(metadata, "pr_url") or "")
        reviewed_head = _first_string(
            metadata,
            "reviewed_head_oid",
            "reviewed_head_sha",
            "reviewed_commit",
        )
        source_permalink = _first_string(
            metadata, "source_slack_permalink", "source_permalink"
        )
        permalink_match = (
            PERMALINK_RE.fullmatch(source_permalink) if source_permalink else None
        )

        github_review = metadata.get("github_review")
        github_state = (
            github_review.get("state") if isinstance(github_review, dict) else None
        )
        outcomes = {
            value.upper()
            for value in (
                metadata.get("review_verdict"),
                metadata.get("github_review_state"),
                github_state,
            )
            if isinstance(value, str)
        }

        if not pr_url or not reviewed_head or not SHA_RE.fullmatch(reviewed_head):
            continue
        if not permalink_match or permalink_match.group("channel").upper() not in {
            channel.upper() for channel in allowed_channels
        }:
            continue
        if not outcomes.intersection({"NEEDS-FIX", "CHANGES_REQUESTED"}):
            continue

        findings = metadata.get("findings")
        if findings is None:
            findings = metadata.get("blockers")
        return {
            "task_id": show.get("id") or show.get("task_id"),
            "pr_url": pr_url,
            "reviewed_head": reviewed_head,
            "source_permalink": source_permalink,
            "findings": _normalized_findings(findings),
            "completed_at": completed_at,
            "run_id": run.get("id") or run.get("run_id"),
        }

    return None
