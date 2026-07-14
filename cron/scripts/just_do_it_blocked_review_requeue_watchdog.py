"""Deterministic helpers for blocked just-do-it review discovery."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ALLOWED_CHANNELS = {"C08K8FQKX9S"}
GHX = Path.home() / ".hermes" / "scripts" / "ghx.sh"
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


def run(command: list[str]) -> str:
    """Run an adapter command and return its stdout."""
    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"command failed: {command[0]}") from error


def _json_object(output: str, boundary: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid JSON from {boundary}") from error
    if not isinstance(value, dict) or "error" in value:
        raise RuntimeError(f"invalid response from {boundary}")
    return value


def fetch_live_pr(pr_url: str) -> dict[str, Any]:
    """Fetch the current GitHub state needed for safe re-review dispatch."""
    live = _json_object(
        run([
            str(GHX),
            "pr",
            "view",
            pr_url,
            "--json",
            "state,isDraft,headRefOid,author,headRepositoryOwner,headRefName",
        ]),
        "live PR lookup",
    )
    author = live.get("author")
    head_owner = live.get("headRepositoryOwner")
    if not (
        live.get("state") in {"OPEN", "CLOSED", "MERGED"}
        and isinstance(live.get("isDraft"), bool)
        and isinstance(live.get("headRefOid"), str)
        and SHA_RE.fullmatch(live["headRefOid"])
        and isinstance(author, dict)
        and isinstance(author.get("login"), str)
        and bool(author["login"].strip())
        and isinstance(head_owner, dict)
        and isinstance(head_owner.get("login"), str)
        and bool(head_owner["login"].strip())
        and isinstance(live.get("headRefName"), str)
        and bool(live["headRefName"].strip())
    ):
        raise RuntimeError("invalid response from live PR lookup")
    return live


def should_dispatch(candidate: dict[str, Any], live: dict[str, Any]) -> bool:
    """Return whether the live PR is a changed, open third-party contribution."""
    author = live.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    return (
        live.get("state") == "OPEN"
        and isinstance(live.get("headRefOid"), str)
        and bool(SHA_RE.fullmatch(live["headRefOid"]))
        and live["headRefOid"].lower()
        != str(candidate.get("reviewed_head", "")).lower()
        and isinstance(author_login, str)
        and bool(author_login.strip())
        and author_login.lower() != "chadtao206"
    )


def target_lane_exists(
    tasks: list[dict[str, Any]], pr_url: str, target_head: str
) -> str | None:
    """Return the existing lane ID for this PR and target head, if present."""
    canonical_url = canonical_pr_url(pr_url)
    if not canonical_url:
        return None
    canonical_url = canonical_url.lower()
    for task in tasks:
        metadata = task.get("metadata")
        metadata_match = isinstance(metadata, dict) and (
            (canonical_pr_url(str(metadata.get("pr_url", ""))) or "").lower()
            == canonical_url
            and str(metadata.get("target_head", "")).lower() == target_head.lower()
        )
        body = task.get("body")
        body_match = isinstance(body, str) and (
            (canonical_pr_url(body) or "").lower() == canonical_url
            and f"New live head: {target_head}".lower() in body.lower()
        )
        if metadata_match or body_match:
            task_id = task.get("id") or task.get("task_id")
            if isinstance(task_id, str):
                return task_id
    return None


def build_rereview_body(candidate: dict[str, Any], live: dict[str, Any]) -> str:
    """Build the read-only instructions for a third-party PR re-review lane."""
    findings = json.dumps(candidate.get("findings", []), indent=2, sort_keys=True)
    return "\n".join([
        "Re-review this third-party pull request. This task is STRICTLY READ-ONLY.",
        "Do NOT edit code, push commits, or modify the pull request.",
        "",
        f"PR: {candidate['pr_url']}",
        f"Old reviewed head: {candidate['reviewed_head']}",
        f"New live head: {live['headRefOid']}",
        f"Source review: {candidate['source_permalink']}",
        "",
        "Prior blocking findings:",
        findings,
    ])


def create_rereview(candidate: dict[str, Any], live: dict[str, Any]) -> str:
    """Create and verify a reviewer lane for the candidate's live head."""
    match = PR_RE.fullmatch(candidate["pr_url"])
    if not match:
        raise RuntimeError("invalid candidate PR URL")
    owner, repo, number = (match.group(name) for name in ("owner", "repo", "number"))
    target_head = live["headRefOid"]
    title = f"Re-review {owner}/{repo}#{number} at {target_head[:12]}"
    created = _json_object(
        run([
            "hermes",
            "kanban",
            "create",
            title,
            "--body",
            build_rereview_body(candidate, live),
            "--assignee",
            "reviewer",
            "--workspace",
            "scratch",
            "--priority",
            "110",
            "--created-by",
            "just-do-it-blocked-review-requeue-watchdog",
            "--idempotency-key",
            f"just-do-it-rereview:{owner.lower()}/{repo.lower()}#{number}:{target_head.lower()}",
            "--json",
        ]),
        "re-review lane creation",
    )
    task_id = created.get("id") or created.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("invalid response from re-review lane creation")

    shown = _json_object(
        run(["hermes", "kanban", "show", task_id, "--json"]),
        "created re-review lane verification",
    )
    if (
        shown.get("status") not in {"ready", "todo", "running"}
        or shown.get("assignee") != "reviewer"
    ):
        raise RuntimeError("created re-review lane failed verification")
    return task_id
