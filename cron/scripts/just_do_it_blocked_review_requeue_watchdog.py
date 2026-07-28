#!/usr/bin/env python3
"""Persistently monitor reviewed pull requests and enqueue exact-head re-reviews."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ALLOWED_CHANNELS = {
    "C08K8FQKX9S",
    "C07BU8AAPB3",
    "C09339EGZ9U",
}
HOME = Path.home() / ".hermes"
STATE_PATH = HOME / "state" / "just_do_it_blocked_review_requeue.json"
DEFAULT_LOOKBACK_SECONDS = (
    7 * 24 * 3600
)  # CLI compatibility; registry entries do not expire by age.
FAIL_RETRY_SECONDS = 60 * 60
MAX_DISPATCHES_PER_TICK = 3
MAX_DISCOVERY_SHOWS_PER_TICK = 20
MAX_PR_REFRESHES_PER_TICK = 8
MAX_COMPLETED_LANE_SHOWS_PER_PR = 10
MAX_COMPLETED_LANE_SHOWS_PER_TICK = 12
DISCOVERY_INTERVAL_SECONDS = 5 * 60
DISCOVERY_OVERLAP_SECONDS = 60 * 60
PR_REFRESH_SECONDS = 5 * 60
TOMBSTONE_RETENTION_SECONDS = 30 * 24 * 3600
STATE_SCHEMA_VERSION = 3
ENROLLMENT_SCHEMA_VERSION = STATE_SCHEMA_VERSION
GHX = HOME / "scripts" / "pr" / "ghx.sh"
PR_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)",
    re.I,
)
PR_VALUE_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)(?:[/?#][^\s]*)?",
    re.I,
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
PERMALINK_RE = re.compile(
    r"https://ylopo\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p\d{16}",
    re.I,
)
REVIEW_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)"
    r"#pullrequestreview-(?P<review_id>\d+)",
    re.I,
)


def canonical_pr_url(value: str) -> str | None:
    """Return the first canonical GitHub pull-request URL found in *value*."""
    if not isinstance(value, str):
        return None
    match = PR_RE.search(value)
    if not match:
        return None
    raw_number = match.group("number")
    if len(raw_number) > 20:
        return None
    try:
        number = int(raw_number)
    except ValueError:
        return None
    if number <= 0:
        return None
    return (
        f"https://github.com/{match.group('owner')}/{match.group('repo')}/pull/{number}"
    )


def _first_string(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalized_outcome(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper().replace("_", "-")


def _positive_review_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        if len(value) > 20:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _review_containers(metadata: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return every supplied review container, or None when one is malformed."""
    reviews: list[dict[str, Any]] = []
    for key in ("review", "github_review"):
        if key not in metadata or metadata[key] is None:
            continue
        review = metadata.get(key)
        if not isinstance(review, dict):
            return None
        reviews.append(review)
    return reviews


def _trusted_metadata_pr(
    metadata: dict[str, Any], reviews: list[dict[str, Any]]
) -> str | None:
    """Require every supplied structured PR alias to bind the same GitHub PR."""
    supplied: list[Any] = []
    for container in (metadata, *reviews):
        supplied.extend(
            container[key]
            for key in ("pr_url", "pr", "pull_request_url")
            if key in container and container[key] is not None
        )
    if not supplied:
        return None
    canonical: list[str] = []
    for value in supplied:
        if not isinstance(value, str) or not PR_VALUE_RE.fullmatch(value.strip()):
            return None
        normalized = canonical_pr_url(value.strip())
        if normalized is None:
            return None
        canonical.append(normalized)
    if len({value.lower() for value in canonical}) != 1:
        return None
    return canonical[0]


def _body_pr_binding(task: dict[str, Any]) -> str | None:
    """Accept task-body provenance only when it names one unique canonical PR."""
    body = task.get("body")
    if not isinstance(body, str):
        return None
    canonical = {
        normalized.lower(): normalized
        for match in PR_RE.finditer(body)
        if (normalized := canonical_pr_url(match.group(0))) is not None
    }
    return next(iter(canonical.values())) if len(canonical) == 1 else None


def _reviewed_head(
    metadata: dict[str, Any], reviews: list[dict[str, Any]] | None = None
) -> str | None:
    """Require all supplied exact-head aliases and nested review commits to agree."""
    values: list[str] = []
    for key in (
        "reviewed_head_oid",
        "reviewed_head_sha",
        "reviewed_commit",
        "reviewed_head",
        "exact_head",
        "target_head",
        "head_sha",
    ):
        if key not in metadata or metadata[key] is None:
            continue
        value = metadata[key]
        if not isinstance(value, str) or not SHA_RE.fullmatch(value.strip()):
            return None
        values.append(value.strip().lower())

    if reviews is None:
        reviews = _review_containers(metadata)
    if reviews is None:
        return None
    for review in reviews:
        for key in (
            "commit",
            "commit_id",
            "commit_oid",
            "head",
            "head_oid",
            "head_sha",
            "reviewed_head",
            "reviewed_head_oid",
            "reviewed_head_sha",
        ):
            if key not in review or review[key] is None:
                continue
            value = review[key]
            if not isinstance(value, str) or not SHA_RE.fullmatch(value.strip()):
                return None
            values.append(value.strip().lower())

    if not values or len(set(values)) != 1:
        return None
    return values[0]


def _validated_review_details(
    metadata: dict[str, Any], pr_url: str, reviews: list[dict[str, Any]]
) -> tuple[bool, str | None, list[str]]:
    """Cross-validate every review URL, ID, and state alias."""
    supplied: list[str] = []
    ids: list[int] = []
    states: list[str] = []
    containers = [
        (
            metadata,
            ("review_url", "github_review_url"),
            ("review_id", "github_review_id"),
            ("github_review_state", "review_state"),
        ),
        *[
            (
                review,
                ("url", "html_url", "review_url"),
                ("id", "review_id"),
                ("state", "review_state"),
            )
            for review in reviews
        ],
    ]
    for container, url_keys, id_keys, state_keys in containers:
        for key in url_keys:
            if key not in container or container[key] is None:
                continue
            value = container[key]
            if not isinstance(value, str) or not value.strip():
                return False, None, []
            supplied.append(value.strip())
        for key in id_keys:
            if key not in container or container[key] is None:
                continue
            review_id = _positive_review_id(container[key])
            if review_id is None:
                return False, None, []
            ids.append(review_id)
        for key in state_keys:
            if key not in container or container[key] is None:
                continue
            state = _normalized_outcome(container[key])
            if state is None:
                return False, None, []
            states.append(state)

    normalized: list[str] = []
    for value in supplied:
        match = REVIEW_URL_RE.fullmatch(value)
        if not match or (canonical_pr_url(value) or "").lower() != pr_url.lower():
            return False, None, []
        review_id = _positive_review_id(match.group("review_id"))
        if review_id is None:
            return False, None, []
        ids.append(review_id)
        normalized.append(f"{pr_url}#pullrequestreview-{review_id}")
    if len(set(ids)) > 1 or len({value.upper() for value in states}) > 1:
        return False, None, []
    if len({value.lower() for value in normalized}) > 1:
        return False, None, []
    return True, normalized[0] if normalized else None, sorted(set(states))


def _watched_source_permalink(
    metadata: dict[str, Any], allowed_channels: set[str]
) -> str | None:
    """Retain valid watched Slack provenance for optional source/reaction behavior."""
    value = _first_string(metadata, "source_slack_permalink", "source_permalink")
    match = PERMALINK_RE.fullmatch(value or "")
    allowed = {channel.upper() for channel in allowed_channels}
    if not match or match.group("channel").upper() not in allowed:
        return None
    return value


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


def _timestamp(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            parsed = int(value)
        except (OverflowError, ValueError):
            return None
        return parsed if parsed > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            )
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _completion_timestamp(run: dict[str, Any]) -> int | None:
    completed = None
    for key in ("ended_at", "completed_at"):
        completed = _timestamp(run.get(key))
        if completed is not None:
            break
    if completed is None:
        return None
    for field in ("status", "outcome"):
        if field in run and (
            not isinstance(run[field], str)
            or run[field].strip().lower() not in {"completed", "done"}
        ):
            return None
    return completed


def _trusted_run_pr(
    task: dict[str, Any], metadata: dict[str, Any], reviews: list[dict[str, Any]]
) -> tuple[str | None, bool]:
    """Resolve a structured PR, or a task-body PR corroborated by a review URL."""
    direct_alias_supplied = any(
        key in container and container[key] is not None
        for container in (metadata, *reviews)
        for key in ("pr_url", "pr", "pull_request_url")
    )
    direct_pr = _trusted_metadata_pr(metadata, reviews)
    if direct_pr is not None:
        return direct_pr, False
    if direct_alias_supplied:
        return None, False
    body_pr = _body_pr_binding(task)
    return (body_pr, True) if body_pr is not None else (None, False)


def blocked_candidate(
    show: dict[str, Any], allowed_channels: set[str]
) -> dict[str, Any] | None:
    """Return the newest completed reviewer run with a trustworthy PR/head pair."""
    task = show.get("task")
    if (
        not isinstance(task, dict)
        or task.get("status") != "done"
        or task.get("assignee") != "reviewer"
    ):
        return None
    runs = show.get("runs")
    if not isinstance(runs, list):
        return None

    newest_first = sorted(
        (run for run in runs if isinstance(run, dict)),
        key=lambda run: _completion_timestamp(run) or 0,
        reverse=True,
    )
    for run in newest_first:
        completed_at = _completion_timestamp(run)
        if not completed_at:
            continue
        if "profile" in run and run.get("profile") != "reviewer":
            continue
        run_metadata = run.get("metadata")
        if not isinstance(run_metadata, dict):
            continue
        metadata = {**run, **run_metadata}

        reviews = _review_containers(metadata)
        if reviews is None:
            continue
        pr_url, body_fallback = _trusted_run_pr(task, metadata, reviews)
        reviewed_head = _reviewed_head(metadata, reviews)
        if not pr_url or not reviewed_head:
            continue
        review_url_valid, review_url, review_states = _validated_review_details(
            metadata, pr_url, reviews
        )
        if body_fallback and review_url is None:
            review_url_valid = False
        if not review_url_valid:
            continue

        outcomes = {
            outcome
            for outcome in (
                _normalized_outcome(metadata.get("review_verdict")),
                _normalized_outcome(metadata.get("verdict")),
                *review_states,
            )
            if outcome is not None
        }
        findings = metadata.get("findings")
        if findings is None:
            findings = metadata.get("blockers")
        task_id = task.get("id") or task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        return {
            "task_id": task_id,
            "pr_url": pr_url,
            "reviewed_head": reviewed_head,
            "source_permalink": _watched_source_permalink(metadata, allowed_channels),
            "review_url": review_url,
            "review_outcomes": sorted(outcomes),
            "findings": _normalized_findings(findings),
            "completed_at": completed_at,
            "run_id": run.get("id") or run.get("run_id"),
        }
    return None


def run(command: list[str]) -> str:
    """Run an adapter command and return stdout."""
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


def _json_list(output: str, boundary: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid JSON from {boundary}") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"invalid response from {boundary}")
    return value


def _run_boundary(command: list[str], boundary: str) -> str:
    try:
        return run(command)
    except RuntimeError as error:
        raise RuntimeError(f"{boundary} failed") from error


def fetch_live_pr(pr_url: str) -> dict[str, Any]:
    """Fetch the current GitHub state required for safe exact-head dispatch."""
    live = _json_object(
        _run_boundary(
            [
                str(GHX),
                "pr",
                "view",
                pr_url,
                "--json",
                "state,isDraft,headRefOid,author,headRepositoryOwner,headRefName",
            ],
            "live PR lookup",
        ),
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
    """Return whether a trustworthy live open PR changed since its prior review."""
    author = live.get("author")
    head_owner = live.get("headRepositoryOwner")
    return (
        live.get("state") == "OPEN"
        and isinstance(live.get("headRefOid"), str)
        and bool(SHA_RE.fullmatch(live["headRefOid"]))
        and live["headRefOid"].lower()
        != str(candidate.get("reviewed_head", "")).lower()
        and isinstance(author, dict)
        and isinstance(author.get("login"), str)
        and bool(author["login"].strip())
        and isinstance(head_owner, dict)
        and isinstance(head_owner.get("login"), str)
        and bool(head_owner["login"].strip())
    )


def target_lane_exists(
    tasks: list[dict[str, Any]], pr_url: str, target_head: str
) -> str | None:
    """Return an active reviewer lane explicitly bound to this PR and exact head."""
    canonical_url = canonical_pr_url(pr_url)
    if not canonical_url or not SHA_RE.fullmatch(target_head):
        return None
    canonical_key = canonical_url.lower()
    for task in tasks:
        if task.get("assignee") != "reviewer" or task.get("status") in {
            "archived",
            "done",
        }:
            continue
        metadata = task.get("metadata")
        metadata_match = isinstance(metadata, dict) and (
            (canonical_pr_url(str(metadata.get("pr_url", ""))) or "").lower()
            == canonical_key
            and str(metadata.get("target_head", "")).lower() == target_head.lower()
        )
        body = task.get("body")
        body_match = isinstance(body, str) and (
            (canonical_pr_url(body) or "").lower() == canonical_key
            and target_head.lower() in body.lower()
        )
        if metadata_match or body_match:
            task_id = task.get("id") or task.get("task_id")
            if isinstance(task_id, str):
                return task_id
    return None


def _completed_exact_head_lane(
    show: dict[str, Any], pr_url: str, target_head: str
) -> str | None:
    task = show.get("task")
    runs = show.get("runs")
    if (
        not isinstance(task, dict)
        or task.get("status") != "done"
        or task.get("assignee") != "reviewer"
        or not isinstance(runs, list)
        or not SHA_RE.fullmatch(target_head)
    ):
        return None
    canonical_key = (canonical_pr_url(pr_url) or "").lower()
    if not canonical_key:
        return None

    for lane_run in sorted(
        (item for item in runs if isinstance(item, dict)),
        key=lambda item: _completion_timestamp(item) or 0,
        reverse=True,
    ):
        if not _completion_timestamp(lane_run) or (
            "profile" in lane_run and lane_run.get("profile") != "reviewer"
        ):
            continue
        metadata = lane_run.get("metadata")
        if not isinstance(metadata, dict):
            continue
        reviews = _review_containers(metadata)
        if reviews is None:
            continue
        exact_head = _reviewed_head(metadata, reviews)
        metadata_pr, body_fallback = _trusted_run_pr(task, metadata, reviews)
        if metadata_pr is None:
            continue
        review_valid, review_url, _ = _validated_review_details(
            metadata, metadata_pr, reviews
        )
        if not review_valid or (body_fallback and review_url is None):
            continue
        if (
            exact_head
            and exact_head.lower() == target_head.lower()
            and (metadata_pr or "").lower() == canonical_key
        ):
            task_id = task.get("id") or task.get("task_id")
            return task_id if isinstance(task_id, str) else None
    return None


class ReconciliationDeferred(RuntimeError):
    """The tick-wide completed-lane show budget was exhausted safely."""


def find_target_lane(
    tasks: list[dict[str, Any]],
    pr_url: str,
    target_head: str,
    metrics: dict[str, int] | None = None,
    completed_show_budget: list[int] | None = None,
) -> str | None:
    """Find an active exact-head lane or a completed exact-head reviewer result."""
    active = target_lane_exists(tasks, pr_url, target_head)
    if active:
        return active
    canonical_key = (canonical_pr_url(pr_url) or "").lower()
    if not canonical_key:
        return None
    completed_candidates: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("status") != "done" or task.get("assignee") != "reviewer":
            continue
        body = task.get("body")
        metadata = task.get("metadata")
        task_pr = canonical_pr_url(body) if isinstance(body, str) else None
        if task_pr is None and isinstance(metadata, dict):
            task_pr = canonical_pr_url(str(metadata.get("pr_url", "")))
        if (task_pr or "").lower() != canonical_key:
            continue
        completed_candidates.append(task)

    completed_candidates.sort(key=_task_completed_epoch, reverse=True)
    for task in completed_candidates[:MAX_COMPLETED_LANE_SHOWS_PER_PR]:
        task_id = task.get("id") or task.get("task_id")
        if not isinstance(task_id, str):
            continue
        if completed_show_budget is not None:
            if completed_show_budget[0] <= 0:
                raise ReconciliationDeferred("completed-lane show budget exhausted")
            completed_show_budget[0] -= 1
        if metrics is not None:
            metrics["task_show_calls"] += 1
            metrics["completed_lane_show_calls"] += 1
        show = _json_object(
            _run_boundary(
                ["hermes", "kanban", "show", task_id, "--json"],
                f"completed re-review lane verification task={task_id}",
            ),
            f"completed re-review lane verification task={task_id}",
        )
        exact = _completed_exact_head_lane(show, pr_url, target_head)
        if exact:
            return exact
    return None


def build_rereview_body(candidate: dict[str, Any], live: dict[str, Any]) -> str:
    """Build exact-head, reviewer-only instructions for a re-review lane."""
    findings = json.dumps(candidate.get("findings", []), indent=2, sort_keys=True)
    author = live["author"]["login"]
    head_owner = live["headRepositoryOwner"]["login"]
    third_party = author.lower() != "chadtao206"
    safety = (
        "STRICT THIRD-PARTY GUARD: Do not make code edits, perform a branch update, commit, push, "
        "edit the PR body or metadata, or enter engineer/remediation/fix lanes. Report any "
        "remaining blockers to the author."
        if third_party
        else "Chad-authored PR: normal review constraints apply: no merge and no stale-head review."
    )
    source_permalink = candidate.get("source_permalink")
    if isinstance(source_permalink, str) and source_permalink:
        source_lines = [f"Source review: {source_permalink}"]
        source_contract = "source permalink; reaction result."
    else:
        source_lines = [
            "Source chain: structured completed reviewer run (no watched Slack source)."
        ]
        review_url = candidate.get("review_url")
        if isinstance(review_url, str) and review_url:
            source_lines.append(f"Prior GitHub review: {review_url}")
        source_contract = "source task/run; no Slack reaction is applicable."
    return "\n".join([
        "Re-review this pull request at the exact live head.",
        "This review lane is STRICTLY READ-ONLY except for GitHub review submission.",
        "Do NOT edit code or modify the pull request.",
        safety,
        "Permitted: read-only inspect, tests/validation, and GitHub review submission.",
        "Immediately before submission, re-fetch the PR and require an exact head match.",
        "Guard against a duplicate exact-head Chadtao206 approval; if one exists, do not submit another.",
        "",
        f"PR: {candidate['pr_url']}",
        f"Live author: {author}",
        f"Head owner: {head_owner}",
        f"Live state: {live['state']}",
        f"Draft: {live['isDraft']}",
        f"Old reviewed head: {candidate['reviewed_head']}",
        f"New live head: {live['headRefOid']}",
        *source_lines,
        "",
        "Prior review findings/context:",
        findings,
        "",
        "Structured completion metadata is required: pr_url; reviewed head; live author; "
        "verdict/findings; review state/id/url/commit; validation/CI; approval submitted/already "
        f"existed; {source_contract}",
    ])


def create_rereview(
    candidate: dict[str, Any],
    live: dict[str, Any],
    metrics: dict[str, int] | None = None,
) -> str:
    """Create and read back a reviewer-only exact-head lane."""
    match = PR_RE.fullmatch(candidate["pr_url"])
    if not match:
        raise RuntimeError("invalid candidate PR URL")
    owner, repo, number = (match.group(name) for name in ("owner", "repo", "number"))
    target_head = live["headRefOid"]
    title = f"Re-review {owner}/{repo}#{number} at {target_head[:12]}"
    created = _json_object(
        _run_boundary(
            [
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
            ],
            "re-review lane creation",
        ),
        "re-review lane creation",
    )
    task_id = created.get("id") or created.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("invalid response from re-review lane creation")
    if metrics is not None:
        metrics["task_show_calls"] += 1
    shown = _json_object(
        _run_boundary(
            ["hermes", "kanban", "show", task_id, "--json"],
            "created re-review lane verification",
        ),
        "created re-review lane verification",
    )
    shown_task = shown.get("task")
    if (
        not isinstance(shown_task, dict)
        or (shown_task.get("id") or shown_task.get("task_id")) != task_id
        or shown_task.get("status") not in {"ready", "todo", "running"}
        or shown_task.get("assignee") != "reviewer"
    ):
        raise RuntimeError("created re-review lane failed verification")
    return task_id


def _new_discovery_state() -> dict[str, Any]:
    return {
        "last_run_at": 0,
        "watermark_completed_at": 0,
        "backfill_complete": False,
        "seen": {},
        "pending": {},
    }


def _quarantine(state: dict[str, Any], path: str, value: Any) -> None:
    quarantine = state.setdefault("quarantine", {})
    if not isinstance(quarantine, dict):
        quarantine = {}
        state["quarantine"] = quarantine
    if path not in quarantine and len(quarantine) >= 100:
        quarantine.pop(next(iter(quarantine)))
    quarantine[path] = {"invalid_type": type(value).__name__}


def _nonnegative_int(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _canonicalize_state_maps(state: dict[str, Any]) -> None:
    enrolled: dict[str, Any] = {}
    for raw_key, raw_record in state["enrolled"].items():
        record = dict(raw_record) if isinstance(raw_record, dict) else raw_record
        source = record.get("pr_url") if isinstance(record, dict) else raw_key
        canonical = canonical_pr_url(source) if isinstance(source, str) else None
        if canonical is None:
            _quarantine(state, f"enrolled.{raw_key}", raw_record)
            continue
        key = canonical.lower()
        if isinstance(record, dict):
            record["pr_url"] = canonical
        existing = enrolled.get(key)
        existing_epoch = (
            existing.get("reviewed_at", 0) if isinstance(existing, dict) else 0
        )
        record_epoch = record.get("reviewed_at", 0) if isinstance(record, dict) else 0
        if not isinstance(existing_epoch, int):
            existing_epoch = 0
        if not isinstance(record_epoch, int):
            record_epoch = 0
        if existing is None or record_epoch >= existing_epoch:
            enrolled[key] = record
    state["enrolled"] = enrolled

    dispatched: dict[str, Any] = {}
    for raw_key, record in state["dispatched"].items():
        key = raw_key
        if isinstance(raw_key, str) and "|" in raw_key:
            raw_pr, head = raw_key.rsplit("|", 1)
            canonical = canonical_pr_url(raw_pr)
            if canonical is not None and SHA_RE.fullmatch(head):
                key = f"{canonical.lower()}|{head.lower()}"
        existing = dispatched.get(key)
        existing_epoch = (
            existing.get("dispatched_at", 0) if isinstance(existing, dict) else 0
        )
        record_epoch = record.get("dispatched_at", 0) if isinstance(record, dict) else 0
        if not isinstance(existing_epoch, int):
            existing_epoch = 0
        if not isinstance(record_epoch, int):
            record_epoch = 0
        if existing is None or record_epoch >= existing_epoch:
            dispatched[key] = record
    state["dispatched"] = dispatched

    failures: dict[str, Any] = {}
    for raw_key, failed_at in state["failures"].items():
        key = raw_key
        if isinstance(raw_key, str) and raw_key.startswith("live|"):
            canonical = canonical_pr_url(raw_key.removeprefix("live|"))
            if canonical is not None:
                key = f"live|{canonical.lower()}"
        elif isinstance(raw_key, str) and "|" in raw_key:
            raw_pr, head = raw_key.rsplit("|", 1)
            canonical = canonical_pr_url(raw_pr)
            if canonical is not None and SHA_RE.fullmatch(head):
                key = f"{canonical.lower()}|{head.lower()}"
        existing = failures.get(key)
        if not isinstance(existing, int) or (
            isinstance(failed_at, int) and failed_at >= existing
        ):
            failures[key] = failed_at
    state["failures"] = failures

    tombstones: dict[str, Any] = {}
    for raw_key, record in state["tombstones"].items():
        source = record.get("pr_url") if isinstance(record, dict) else raw_key
        canonical = canonical_pr_url(source) if isinstance(source, str) else None
        if canonical is None:
            _quarantine(state, f"tombstones.{raw_key}", record)
            continue
        key = canonical.lower()
        existing = tombstones.get(key)
        existing_epoch = (
            existing.get("pruned_at", 0) if isinstance(existing, dict) else 0
        )
        record_epoch = record.get("pruned_at", 0) if isinstance(record, dict) else 0
        if not isinstance(existing_epoch, int):
            existing_epoch = 0
        if not isinstance(record_epoch, int):
            record_epoch = 0
        if existing is None or record_epoch >= existing_epoch:
            tombstones[key] = record
    state["tombstones"] = tombstones


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        value = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("state read failed") from error
    if not isinstance(value, dict):
        raise RuntimeError("state read failed")
    previous_schema = value.get("state_schema_version", 0)
    if not isinstance(previous_schema, int) or isinstance(previous_schema, bool):
        previous_schema = 0
    for key in ("dispatched", "failures", "enrolled", "tombstones"):
        value.setdefault(key, {})
        if not isinstance(value[key], dict):
            raise RuntimeError("state read failed")
    quarantine = value.setdefault("quarantine", {})
    if not isinstance(quarantine, dict):
        value["quarantine"] = {}
    discovery = value.setdefault("discovery", _new_discovery_state())
    if not isinstance(discovery, dict):
        raise RuntimeError("state read failed")
    for key, default in _new_discovery_state().items():
        discovery.setdefault(
            key, default.copy() if isinstance(default, dict) else default
        )
    if not isinstance(discovery["seen"], dict) or not isinstance(
        discovery["pending"], dict
    ):
        raise RuntimeError("state read failed")

    for key in ("last_run_at", "watermark_completed_at"):
        normalized = _nonnegative_int(discovery[key])
        if normalized is None:
            _quarantine(value, f"discovery.{key}", discovery[key])
            normalized = 0
        discovery[key] = normalized
    if not isinstance(discovery["backfill_complete"], bool):
        _quarantine(
            value, "discovery.backfill_complete", discovery["backfill_complete"]
        )
        discovery["backfill_complete"] = False

    normalized_seen: dict[str, int] = {}
    for task_id, completed_at in discovery["seen"].items():
        normalized = _nonnegative_int(completed_at)
        if not isinstance(task_id, str) or normalized is None:
            _quarantine(value, f"discovery.seen.{task_id}", completed_at)
            continue
        normalized_seen[task_id] = normalized
    discovery["seen"] = normalized_seen

    normalized_pending: dict[str, dict[str, int]] = {}
    for task_id, pending in discovery["pending"].items():
        completed_at = (
            pending.get("completed_at") if isinstance(pending, dict) else None
        )
        normalized = _nonnegative_int(completed_at)
        if not isinstance(task_id, str) or normalized is None:
            _quarantine(value, f"discovery.pending.{task_id}", pending)
            continue
        normalized_pending[task_id] = {"completed_at": normalized}
    discovery["pending"] = normalized_pending

    _canonicalize_state_maps(value)
    if previous_schema < STATE_SCHEMA_VERSION:
        for ledger in value["dispatched"].values():
            task_id = ledger.get("task_id") if isinstance(ledger, dict) else None
            if isinstance(task_id, str) and task_id:
                discovery["seen"].pop(task_id, None)
                discovery["pending"][task_id] = {"completed_at": 0}
        discovery["last_run_at"] = 0
    value["state_schema_version"] = STATE_SCHEMA_VERSION
    value["enrollment_schema_version"] = ENROLLMENT_SCHEMA_VERSION
    return value


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
        os.replace(temporary, STATE_PATH)
    except OSError as error:
        raise RuntimeError("state write failed") from error


def _task_list() -> list[dict[str, Any]]:
    """List completed reviewer tasks for bounded completion discovery."""
    return _json_list(
        _run_boundary(
            [
                "hermes",
                "kanban",
                "list",
                "--assignee",
                "reviewer",
                "--status",
                "done",
                "--sort",
                "updated",
                "--json",
            ],
            "Kanban list",
        ),
        "Kanban list",
    )


def _reviewer_task_list() -> list[dict[str, Any]]:
    """List reviewer lanes once when exact-head reconciliation is needed."""
    return _json_list(
        _run_boundary(
            ["hermes", "kanban", "list", "--assignee", "reviewer", "--json"],
            "reviewer lane list",
        ),
        "reviewer lane list",
    )


def _completed_epoch(candidate: dict[str, Any]) -> int:
    return int(candidate["completed_at"])


def _task_completed_epoch(task: dict[str, Any]) -> int:
    """Return the newest list-row completion/update epoch for run-aware discovery."""
    return max(
        (
            _timestamp(task.get(key)) or 0
            for key in ("completed_at", "updated_at", "created_at")
        ),
        default=0,
    )


def _task_needs_show(task: dict[str, Any], seen: dict[str, int]) -> bool:
    task_id = str(task.get("id") or task.get("task_id"))
    return _task_completed_epoch(task) > seen.get(task_id, -1)


def _enrollment_record(
    candidate: dict[str, Any], now: int, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    enrolled_at = (
        existing.get("enrolled_at", now) if isinstance(existing, dict) else now
    )
    return {
        "pr_url": candidate["pr_url"],
        "reviewed_head": candidate["reviewed_head"],
        "source_task_id": candidate["task_id"],
        "source_run_id": candidate.get("run_id"),
        "source_permalink": candidate.get("source_permalink"),
        "review_url": candidate.get("review_url"),
        "review_outcomes": candidate.get("review_outcomes", []),
        "findings": candidate.get("findings", []),
        "reviewed_at": _completed_epoch(candidate),
        "enrolled_at": enrolled_at,
        "last_seen_at": now,
        "last_checked_at": existing.get("last_checked_at", 0)
        if isinstance(existing, dict)
        else 0,
        "next_check_at": 0,
    }


def _valid_enrollment(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("pr_url"), str)
        and canonical_pr_url(value["pr_url"]) == value["pr_url"]
        and isinstance(value.get("reviewed_head"), str)
        and bool(SHA_RE.fullmatch(value["reviewed_head"]))
        and isinstance(value.get("reviewed_at"), int)
        and value["reviewed_at"] > 0
    )


def _prune_enrollment(
    state: dict[str, Any], pr_key: str, now: int, live: dict[str, Any] | None = None
) -> None:
    record = state["enrolled"].pop(pr_key, None)
    if live is not None:
        state["tombstones"][pr_key] = {
            "pr_url": record.get("pr_url", pr_key)
            if isinstance(record, dict)
            else pr_key,
            "state": live.get("state"),
            "head": live.get("headRefOid"),
            "pruned_at": now,
        }
    for key in list(state["dispatched"]):
        if key.startswith(f"{pr_key}|"):
            state["dispatched"].pop(key, None)
    for key in list(state["failures"]):
        if key == f"live|{pr_key}" or key.startswith(f"{pr_key}|"):
            state["failures"].pop(key, None)


def _compact_tombstones(state: dict[str, Any], now: int) -> None:
    for key, value in list(state["tombstones"].items()):
        pruned_at = value.get("pruned_at") if isinstance(value, dict) else None
        if (
            not isinstance(pruned_at, int)
            or now - pruned_at > TOMBSTONE_RETENTION_SECONDS
        ):
            state["tombstones"].pop(key, None)


def _compact_failures(state: dict[str, Any], now: int) -> None:
    """Drop expired failure backoff markers so legacy scans cannot bloat state."""
    for key, failed_at in list(state["failures"].items()):
        if not isinstance(failed_at, int) or now - failed_at >= FAIL_RETRY_SECONDS:
            state["failures"].pop(key, None)


def _discover(
    state: dict[str, Any],
    args: argparse.Namespace,
    now: int,
    output: list[str],
    metrics: dict[str, int],
) -> tuple[set[str], list[dict[str, Any]]]:
    discovery = state["discovery"]
    forced = bool(
        getattr(args, "only_task", None) or getattr(args, "backfill_existing", False)
    )
    due = (
        forced
        or discovery["last_run_at"] <= 0
        or (now - discovery["last_run_at"] >= DISCOVERY_INTERVAL_SECONDS)
    )
    if not due:
        return set(), []

    list_failure_key = "discovery-list"
    failed_at = state["failures"].get(list_failure_key)
    if isinstance(failed_at, int) and now - failed_at < FAIL_RETRY_SECONDS:
        return set(), []
    metrics["task_list_calls"] += 1
    try:
        tasks = _task_list()
    except RuntimeError as error:
        state["failures"][list_failure_key] = now
        output.append(f"JUST_DO_IT_BLOCKED_REREVIEW error={error}")
        return set(), []
    state["failures"].pop(list_failure_key, None)
    eligible = [
        task
        for task in tasks
        if task.get("status") == "done"
        and task.get("assignee") == "reviewer"
        and isinstance(task.get("id") or task.get("task_id"), str)
    ]
    rows = {str(task.get("id") or task.get("task_id")): task for task in eligible}
    seen = discovery["seen"]
    pending = discovery["pending"]
    only_task = getattr(args, "only_task", None)
    selected: list[dict[str, Any]] = []
    pending_due = []
    for task_id in list(pending):
        if only_task and task_id != only_task:
            continue
        task = rows.get(task_id)
        if task is None:
            pending.pop(task_id, None)
            continue
        listed_epoch = _task_completed_epoch(task)
        pending_epoch = pending[task_id].get("completed_at", 0)
        if listed_epoch > pending_epoch:
            pending[task_id] = {"completed_at": listed_epoch}
            state["failures"].pop(f"show|{task_id}", None)
        failed_at = state["failures"].get(f"show|{task_id}")
        if not isinstance(failed_at, int) or now - failed_at >= FAIL_RETRY_SECONDS:
            pending_due.append(task)
    pending_due.sort(key=_task_completed_epoch, reverse=True)
    selected.extend(pending_due[:MAX_DISCOVERY_SHOWS_PER_TICK])

    remaining = MAX_DISCOVERY_SHOWS_PER_TICK - len(selected)
    if remaining > 0:
        if only_task:
            candidates = [rows[only_task]] if only_task in rows else []
        elif not discovery.get("backfill_complete", False) or getattr(
            args, "backfill_existing", False
        ):
            candidates = [
                task
                for task in eligible
                if _task_needs_show(task, seen)
                and str(task.get("id") or task.get("task_id")) not in pending
            ]
            candidates.sort(key=_task_completed_epoch, reverse=True)
        else:
            floor = max(
                0,
                int(discovery.get("watermark_completed_at", 0))
                - DISCOVERY_OVERLAP_SECONDS,
            )
            candidates = [
                task
                for task in eligible
                if _task_completed_epoch(task) >= floor
                and _task_needs_show(task, seen)
                and str(task.get("id") or task.get("task_id")) not in pending
            ]
            candidates.sort(key=_task_completed_epoch)
        selected.extend(candidates[:remaining])

    selected_prs: set[str] = set()
    selected_ids: set[str] = set()
    for task in selected:
        task_id = str(task.get("id") or task.get("task_id"))
        if task_id in selected_ids:
            continue
        selected_ids.add(task_id)
        try:
            metrics["task_show_calls"] += 1
            show = _json_object(
                _run_boundary(
                    ["hermes", "kanban", "show", task_id, "--json"],
                    f"Kanban show task={task_id}",
                ),
                f"Kanban show task={task_id}",
            )
            item = blocked_candidate(
                show, set(getattr(args, "channel", None) or DEFAULT_ALLOWED_CHANNELS)
            )
            if item is not None:
                pr_key = item["pr_url"].lower()
                selected_prs.add(pr_key)
                existing = state["enrolled"].get(pr_key)
                existing_reviewed_at = (
                    existing.get("reviewed_at", 0) if isinstance(existing, dict) else 0
                )
                if _completed_epoch(item) >= existing_reviewed_at:
                    state["enrolled"][pr_key] = _enrollment_record(item, now, existing)
                    state["tombstones"].pop(pr_key, None)
                    metrics["enrollments_updated"] += 1
            shown_epoch = _completion_timestamp(
                max(
                    (
                        lane_run
                        for lane_run in show.get("runs", [])
                        if isinstance(lane_run, dict)
                    ),
                    key=lambda lane_run: _completion_timestamp(lane_run) or 0,
                    default={},
                )
            )
            seen[task_id] = max(shown_epoch or 0, _task_completed_epoch(task))
            pending.pop(task_id, None)
            state["failures"].pop(f"show|{task_id}", None)
        except (RuntimeError, KeyError, ValueError) as error:
            pending[task_id] = {"completed_at": _task_completed_epoch(task)}
            state["failures"][f"show|{task_id}"] = now
            output.append(f"JUST_DO_IT_BLOCKED_REREVIEW error task={task_id}: {error}")

    unprocessed = [
        task_id
        for task_id, task in rows.items()
        if _task_needs_show(task, seen)
        and task_id not in pending
        and task_id not in selected_ids
    ]
    if not unprocessed and not pending:
        discovery["backfill_complete"] = True
        discovery["watermark_completed_at"] = max([
            int(discovery.get("watermark_completed_at", 0)),
            *(_task_completed_epoch(task) for task in eligible),
        ])
    elif selected:
        discovery["watermark_completed_at"] = max(
            int(discovery.get("watermark_completed_at", 0)),
            *(_task_completed_epoch(task) for task in selected),
        )
    discovery["last_run_at"] = now

    if discovery.get("backfill_complete", False):
        floor = max(
            0,
            int(discovery.get("watermark_completed_at", 0)) - DISCOVERY_OVERLAP_SECONDS,
        )
        discovery["seen"] = {
            task_id: completed
            for task_id, completed in seen.items()
            if isinstance(completed, int) and completed >= floor
        }
    return selected_prs, tasks


def _metrics_line(metrics: dict[str, int], state: dict[str, Any]) -> str:
    payload = {
        **metrics,
        "registry_size": len(state["enrolled"]),
        "pending_discovery": len(state["discovery"]["pending"]),
        "backfill_complete": bool(state["discovery"]["backfill_complete"]),
    }
    return "METRICS " + json.dumps(payload, sort_keys=True)


def process_tick(args: argparse.Namespace, now: int) -> tuple[int, list[str]]:
    """Run one bounded discovery, registry refresh, reconciliation, and dispatch tick."""
    output: list[str] = []
    metrics = {
        "task_list_calls": 0,
        "task_show_calls": 0,
        "lane_list_calls": 0,
        "github_calls": 0,
        "enrollments_updated": 0,
        "dispatches": 0,
        "completed_lane_show_calls": 0,
        "reconciliation_deferred": 0,
    }
    try:
        state = _load_state()
    except RuntimeError as error:
        return 1, [f"JUST_DO_IT_BLOCKED_REREVIEW error={error}"]

    _compact_failures(state, now)
    try:
        selected_prs, discovery_tasks = _discover(state, args, now, output, metrics)
    except RuntimeError as error:
        return 1, [f"JUST_DO_IT_BLOCKED_REREVIEW error={error}"]

    _compact_tombstones(state, now)
    dry_run = bool(getattr(args, "dry_run", False))
    if not dry_run:
        try:
            _save_state(state)
        except RuntimeError as error:
            output.append(f"JUST_DO_IT_BLOCKED_REREVIEW error={error}")

    only_task = getattr(args, "only_task", None)
    if only_task:
        monitored = {
            key: value
            for key, value in state["enrolled"].items()
            if key in selected_prs
            or (isinstance(value, dict) and value.get("source_task_id") == only_task)
        }
    else:
        monitored = dict(state["enrolled"])

    forced_refresh = bool(only_task or getattr(args, "backfill_existing", False))
    ordered = sorted(
        monitored.items(),
        key=lambda pair: (
            _nonnegative_int(pair[1].get("next_check_at", 0)) or 0
            if isinstance(pair[1], dict)
            else 0,
            _nonnegative_int(pair[1].get("last_checked_at", 0)) or 0
            if isinstance(pair[1], dict)
            else 0,
            -(_nonnegative_int(pair[1].get("reviewed_at", 0)) or 0)
            if isinstance(pair[1], dict)
            else 0,
        ),
    )
    reviewer_tasks: list[dict[str, Any]] | None = None
    dispatches = 0
    refreshes = 0
    completed_show_budget = [MAX_COMPLETED_LANE_SHOWS_PER_TICK]

    for pr_key, item in ordered:
        if (
            dispatches >= MAX_DISPATCHES_PER_TICK
            or refreshes >= MAX_PR_REFRESHES_PER_TICK
        ):
            break
        if not _valid_enrollment(item):
            if not dry_run:
                _prune_enrollment(state, pr_key, now)
            continue
        next_check_at = _nonnegative_int(item.get("next_check_at", 0)) or 0
        if not forced_refresh and now < next_check_at:
            continue

        task_id = item.get("source_task_id")
        failure_key = f"live|{pr_key}"
        target_key: str | None = None
        live: dict[str, Any] | None = None
        try:
            failed_at = state["failures"].get(failure_key)
            if isinstance(failed_at, int) and now - failed_at < FAIL_RETRY_SECONDS:
                continue
            metrics["github_calls"] += 1
            refreshes += 1
            live = fetch_live_pr(item["pr_url"])
            if live["state"] != "OPEN":
                if not dry_run:
                    _prune_enrollment(state, pr_key, now, live)
                    _save_state(state)
                continue

            item["last_checked_at"] = now
            item["next_check_at"] = now + PR_REFRESH_SECONDS
            item["last_live_state"] = live["state"]
            item["last_live_head"] = live["headRefOid"]
            item["last_live_author"] = live["author"]["login"]
            item["last_live_head_owner"] = live["headRepositoryOwner"]["login"]
            state["failures"].pop(failure_key, None)
            if live["headRefOid"].lower() == item["reviewed_head"].lower():
                continue

            target_key = f"{pr_key}|{live['headRefOid'].lower()}"
            if target_key in state["dispatched"]:
                if dry_run:
                    ledger = state["dispatched"].get(target_key)
                    existing_task = (
                        ledger.get("task_id") if isinstance(ledger, dict) else None
                    )
                    output.append(
                        f"WOULD_SKIP pr={item['pr_url']} head={live['headRefOid']} "
                        f"existing_lane={existing_task or 'ledger'} ledger=true"
                    )
                continue
            if reviewer_tasks is None:
                metrics["lane_list_calls"] += 1
                reviewer_tasks = _reviewer_task_list()
            try:
                existing = find_target_lane(
                    reviewer_tasks,
                    item["pr_url"],
                    live["headRefOid"],
                    metrics,
                    completed_show_budget,
                )
            except ReconciliationDeferred:
                item["next_check_at"] = now
                metrics["reconciliation_deferred"] += 1
                continue
            if existing:
                if dry_run:
                    output.append(
                        f"WOULD_SKIP pr={item['pr_url']} head={live['headRefOid']} existing_lane={existing}"
                    )
                else:
                    state["dispatched"][target_key] = {
                        "task_id": existing,
                        "dispatched_at": now,
                    }
                    state["failures"].pop(target_key, None)
                    _save_state(state)
                continue
            if not should_dispatch(item, live):
                continue
            failed_at = state["failures"].get(target_key)
            if isinstance(failed_at, int) and now - failed_at < FAIL_RETRY_SECONDS:
                continue

            if dry_run:
                output.append(
                    f"WOULD_DISPATCH pr={item['pr_url']} head={live['headRefOid']}"
                )
                dispatches += 1
                metrics["dispatches"] += 1
                continue
            new_task = create_rereview(item, live, metrics)
            state["dispatched"][target_key] = {
                "task_id": new_task,
                "dispatched_at": now,
            }
            state["failures"].pop(target_key, None)
            dispatches += 1
            metrics["dispatches"] += 1
            output.append(
                f"JUST_DO_IT_BLOCKED_REREVIEW dispatched pr={item['pr_url']} "
                f"head={live['headRefOid'][:7]} task={new_task}"
            )
            _save_state(state)
        except (RuntimeError, KeyError, ValueError) as error:
            reconciled = None
            if target_key and live is not None:
                try:
                    metrics["lane_list_calls"] += 1
                    reviewer_tasks = _reviewer_task_list()
                    matches = [
                        task.get("id") or task.get("task_id")
                        for task in reviewer_tasks
                        if target_lane_exists(
                            [task], item["pr_url"], live["headRefOid"]
                        )
                    ]
                    matches = [match for match in matches if isinstance(match, str)]
                    if len(set(matches)) == 1:
                        reconciled = matches[0]
                except RuntimeError:
                    pass
            key = target_key or failure_key
            if reconciled:
                state["dispatched"][key] = {"task_id": reconciled, "dispatched_at": now}
                state["failures"].pop(key, None)
            else:
                state["failures"][key] = now
            if not dry_run:
                try:
                    _save_state(state)
                except RuntimeError:
                    pass
            if reconciled and live is not None:
                output.append(
                    f"JUST_DO_IT_BLOCKED_REREVIEW dispatched pr={item['pr_url']} "
                    f"head={live['headRefOid'][:7]} task={reconciled} reconciled=true"
                )
            else:
                output.append(
                    f"JUST_DO_IT_BLOCKED_REREVIEW error task={task_id}: {error}"
                )

    if not dry_run:
        try:
            _save_state(state)
        except RuntimeError as error:
            output.append(f"JUST_DO_IT_BLOCKED_REREVIEW error={error}")
    if getattr(args, "metrics", False):
        output.append(_metrics_line(metrics, state))
    return (1 if any(" error" in line for line in output) else 0), output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-existing", action="store_true")
    parser.add_argument(
        "--lookback-seconds",
        type=int,
        default=DEFAULT_LOOKBACK_SECONDS,
        help="Deprecated compatibility flag; enrolled open PRs persist regardless of review age.",
    )
    parser.add_argument("--only-task")
    parser.add_argument("--channel", action="append", default=[])
    parser.add_argument("--metrics", action="store_true")
    args = parser.parse_args(argv)
    rc, lines = process_tick(args, int(datetime.now().timestamp()))
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
