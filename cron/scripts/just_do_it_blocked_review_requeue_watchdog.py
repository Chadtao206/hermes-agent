#!/usr/bin/env python3
"""Deterministic helpers for blocked just-do-it review discovery."""

from __future__ import annotations

import json
import argparse
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ALLOWED_CHANNELS = {"C08K8FQKX9S"}
HOME = Path.home() / ".hermes"
STATE_PATH = HOME / "state" / "just_do_it_blocked_review_requeue.json"
DEFAULT_LOOKBACK_SECONDS = 7 * 24 * 3600
FAIL_RETRY_SECONDS = 60 * 60
MAX_DISPATCHES_PER_TICK = 2
GHX = HOME / "scripts" / "pr" / "ghx.sh"
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


def _completion_timestamp(run: dict[str, Any]) -> int | None:
    timestamp = run.get("ended_at", run.get("completed_at"))
    if isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, (int, float)):
        completed = int(timestamp) if timestamp > 0 else None
    elif isinstance(timestamp, str):
        try:
            completed = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
        except ValueError:
            completed = None
    else:
        completed = None
    if completed is None:
        return None

    for field in ("status", "outcome"):
        if field in run and (
            not isinstance(run[field], str)
            or run[field].strip().lower() not in {"completed", "done"}
        ):
            return None
    return completed


def blocked_candidate(
    show: dict[str, Any], allowed_channels: set[str]
) -> dict[str, Any] | None:
    """Return the newest structured blocking-review run, if one exists."""
    task = show.get("task")
    if not isinstance(task, dict) or task.get("status") != "done" or task.get("assignee") != "reviewer":
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
            "task_id": task.get("id") or task.get("task_id"),
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


def _run_boundary(command: list[str], boundary: str) -> str:
    try:
        return run(command)
    except RuntimeError as error:
        raise RuntimeError(f"{boundary} failed") from error


def fetch_live_pr(pr_url: str) -> dict[str, Any]:
    """Fetch the current GitHub state needed for safe re-review dispatch."""
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
    """Return whether the live PR is open and changed since its prior review."""
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
        if task.get("assignee") != "reviewer":
            continue
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
    """Build complete, head-bound instructions for a re-review lane."""
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
        f"Source review: {candidate['source_permalink']}",
        "",
        "Prior blocking findings:",
        findings,
        "",
        "Structured completion metadata is required: pr_url; reviewed head; live author; "
        "verdict/findings; review state/id/url/commit; validation/CI; approval submitted/already "
        "existed; source permalink; reaction result.",
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


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        value = {}
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("state read failed") from error
    if not isinstance(value, dict):
        raise RuntimeError("state read failed")
    value.setdefault("dispatched", {})
    value.setdefault("failures", {})
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
    try:
        output = run(["hermes", "kanban", "list", "--json"])
    except RuntimeError as error:
        raise RuntimeError("Kanban list failed") from error
    try:
        tasks = json.loads(output)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid JSON from Kanban list") from error
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise RuntimeError("invalid response from Kanban list")
    return tasks


def _completed_epoch(candidate: dict[str, Any]) -> int:
    return int(candidate["completed_at"])


def process_tick(args: argparse.Namespace, now: int) -> tuple[int, list[str]]:
    """Run one bounded discovery and dispatch tick."""
    output: list[str] = []
    try:
        state = _load_state()
    except RuntimeError as error:
        return 1, [f"JUST_DO_IT_BLOCKED_REREVIEW error={error}"]

    if "baseline_completed_at" not in state:
        state["baseline_completed_at"] = now
        try:
            _save_state(state)
        except RuntimeError as error:
            return 1, [f"JUST_DO_IT_BLOCKED_REREVIEW error={error}"]
        if not args.backfill_existing:
            return 0, []

    try:
        tasks = _task_list()
    except RuntimeError as error:
        return 1, [f"JUST_DO_IT_BLOCKED_REREVIEW error={error}"]

    dispatches = 0
    for task in tasks:
        key: str | None = None
        task_id = task.get("id") or task.get("task_id")
        if not isinstance(task_id, str) or (
            args.only_task and task_id != args.only_task
        ):
            continue
        if task.get("status") != "done" or task.get("assignee") != "reviewer":
            continue
        try:
            key = f"show|{task_id}"
            failed_at = state["failures"].get(key)
            if isinstance(failed_at, int) and now - failed_at < FAIL_RETRY_SECONDS:
                continue
            show = _json_object(
                _run_boundary(
                    ["hermes", "kanban", "show", task_id, "--json"],
                    f"Kanban show task={task_id}",
                ),
                f"Kanban show task={task_id}",
            )
            item = blocked_candidate(
                show, set(args.channel or DEFAULT_ALLOWED_CHANNELS)
            )
            if item is None:
                continue
            completed = _completed_epoch(item)
            if completed < now - args.lookback_seconds:
                continue
            if (
                not args.backfill_existing
                and completed <= state["baseline_completed_at"]
            ):
                continue
            key = f"live|{item['pr_url'].lower()}"
            failed_at = state["failures"].get(key)
            if isinstance(failed_at, int) and now - failed_at < FAIL_RETRY_SECONDS:
                continue
            live = fetch_live_pr(item["pr_url"])
            if not should_dispatch(item, live):
                continue
            key = f"{item['pr_url'].lower()}|{live['headRefOid'].lower()}"
            existing = target_lane_exists(tasks, item["pr_url"], live["headRefOid"])
            if key in state["dispatched"] or existing:
                if args.dry_run and existing:
                    output.append(f"WOULD_SKIP pr={item['pr_url']} head={live['headRefOid']} existing_lane={existing}")
                continue
            failed_at = state["failures"].get(key)
            if isinstance(failed_at, int) and now - failed_at < FAIL_RETRY_SECONDS:
                continue
            if dispatches >= MAX_DISPATCHES_PER_TICK:
                continue
            if args.dry_run:
                output.append(f"WOULD_DISPATCH pr={item['pr_url']} head={live['headRefOid']}")
                continue
            new_task = create_rereview(item, live)
            state["dispatched"][key] = {"task_id": new_task, "dispatched_at": now}
            state["failures"].pop(key, None)
            dispatches += 1
            output.append(
                f"JUST_DO_IT_BLOCKED_REREVIEW dispatched pr={item['pr_url']} "
                f"head={live['headRefOid'][:7]} task={new_task}"
            )
            _save_state(state)
        except (RuntimeError, KeyError, ValueError) as error:
            if isinstance(key, str):
                try:
                    tasks = _task_list()
                except RuntimeError:
                    pass
                reconciled = None
                if "|" in key:
                    pr_url, target_head = key.rsplit("|", 1)
                    matches = [
                        task.get("id") or task.get("task_id") for task in tasks
                        if target_lane_exists([task], pr_url, target_head)
                    ]
                    matches = [match for match in matches if isinstance(match, str)]
                    if len(matches) == 1:
                        reconciled = matches[0]
                if reconciled:
                    state["dispatched"][key] = {"task_id": reconciled, "dispatched_at": now}
                    state["failures"].pop(key, None)
                else:
                    state["failures"][key] = now
                try:
                    _save_state(state)
                except RuntimeError:
                    pass
                if reconciled:
                    output.append(
                        f"JUST_DO_IT_BLOCKED_REREVIEW dispatched pr={item['pr_url']} "
                        f"head={live['headRefOid'][:7]} task={reconciled} reconciled=true"
                    )
                    continue
            output.append(f"JUST_DO_IT_BLOCKED_REREVIEW error task={task_id}: {error}")

    return (1 if any(" error" in line for line in output) else 0), output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill-existing", action="store_true")
    parser.add_argument(
        "--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS
    )
    parser.add_argument("--only-task")
    parser.add_argument("--channel", action="append", default=[])
    args = parser.parse_args(argv)
    rc, lines = process_tick(args, int(datetime.now().timestamp()))
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
