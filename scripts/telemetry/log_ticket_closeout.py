#!/usr/bin/env python3
"""First-class telemetry closeout for direct Jira/ticket-hub lanes.

Use this when a ticket was executed through direct spawned Hermes/Claude
processes instead of a kanban task. Kanban work should still prefer:

  hermes kanban closeout <task_id> ...

This helper writes the same task-closeout shape as log_task_closeout.py, then
adds ticket-specific PR/review events so ready-for-human-review handoffs can be
guarded by structured telemetry rather than session/log history alone.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import log_task_closeout  # noqa: E402
from common import canonical_profile, canonical_profiles, json_dumps, resolve_telemetry_root, utc_now  # noqa: E402

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
SOURCE = "ticket_closeout"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log a direct Jira/ticket-hub lane into Hermes telemetry."
    )
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--ticket-key", required=True, help="Jira key, e.g. PLAT-1898")
    parser.add_argument("--title", required=True, help="Human-readable ticket/PR title")
    parser.add_argument("--summary", required=True, help="Task outcome summary")
    parser.add_argument("--owner-profile", default="engineer", help="Final implementation owner profile")
    parser.add_argument("--initial-owner", default="default", help="Initial routing owner, usually default/Jensen")
    parser.add_argument("--current-owner", default="engineer", help="Final/local owner profile")
    parser.add_argument("--assisting-profiles", default="default,reviewer", help="Comma-separated assisting profiles")
    parser.add_argument("--repo", required=True, help="GitHub repo slug, e.g. Ylopo/chameleon")
    parser.add_argument("--repo-hint", default="", help="Short repo name for metrics/reporting")
    parser.add_argument("--workdir", default="", help="Ticket worktree/workdir")
    parser.add_argument("--branch", default="", help="PR branch name")
    parser.add_argument("--pr-url", required=True, help="Pull request URL")
    parser.add_argument("--pr-number", type=int, help="Pull request number")
    parser.add_argument("--reviewed-commit", required=True, help="Final reviewed commit SHA")
    parser.add_argument("--acceptance-artifact-url", required=True, help="Boris acceptance comment/session/artifact URL")
    parser.add_argument("--verification", required=True, help="Verification evidence checked")
    parser.add_argument("--opened-at", help="ISO timestamp when orchestration/ticket work started")
    parser.add_argument("--pr-created-at", help="ISO timestamp when PR opened")
    parser.add_argument("--closed-at", help="ISO timestamp of final closeout; defaults to now")
    parser.add_argument("--verification-strength", default="strong", choices=("none", "weak", "moderate", "strong"))
    parser.add_argument("--outcome", default="success", choices=("success", "partial", "fail"))
    parser.add_argument("--status", default="completed", choices=("open", "completed", "partial", "failed", "cancelled"))
    parser.add_argument("--correction-state", choices=("present", "none", "unknown"), help="Correction state. Required for blocked reviews unless --review-block-not-correction-reason is provided.")
    parser.add_argument("--review-block-not-correction-reason", default="", help="Explain why a blocked review should not count as a correction.")
    parser.add_argument("--learning-artifact-state", default="none", choices=("present", "none", "unknown"))
    parser.add_argument("--correct-owner", default="true", choices=("true", "false"))
    parser.add_argument("--user-corrected", default="false", choices=("true", "false"))
    parser.add_argument(
        "--blocked-review",
        action="append",
        default=[],
        metavar="ISO|URL|SUMMARY",
        help="Blocked Boris review event. Repeatable.",
    )
    parser.add_argument(
        "--remediation",
        action="append",
        default=[],
        metavar="ISO|URL|SUMMARY",
        help="Engineer remediation event. Repeatable.",
    )
    parser.add_argument("--notes-json", help="Extra notes JSON object")
    parser.add_argument("--dry-run", action="store_true", help="Print the closeout payload without writing")
    return parser.parse_args()


def split_csv(value: str) -> list[str]:
    return canonical_profiles(part.strip() for part in (value or "").split(",") if part.strip())


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_event(raw: str) -> dict[str, str]:
    parts = raw.split("|", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise SystemExit(f"Invalid event {raw!r}; expected ISO|URL|SUMMARY")
    return {"at": parts[0].strip(), "url": parts[1].strip(), "summary": parts[2].strip()}


def ticket_task_id(ticket_key: str) -> str:
    key = ticket_key.strip().upper()
    if not TICKET_RE.fullmatch(key):
        raise SystemExit(f"Invalid --ticket-key {ticket_key!r}; expected e.g. PLAT-1898")
    return f"jira:{key}"


def insert_task_event(conn: sqlite3.Connection, task_id: str, at: str, event_type: str, profile: str, payload: dict[str, Any]) -> None:
    body = {"source": SOURCE, **payload}
    conn.execute(
        "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json, provenance) VALUES (?, ?, ?, ?, ?, 'real')",
        (task_id, at, event_type, profile, json_dumps(body)),
    )


def insert_review_event(
    conn: sqlite3.Connection,
    task_id: str,
    at: str,
    status: str,
    url: str,
    details: dict[str, Any],
    findings: list[str] | None = None,
) -> None:
    cur = conn.execute(
        """
        INSERT INTO review_events(task_id, occurred_at, run_id, reviewer_profile, review_type, status, details_json, source)
        VALUES (?, ?, ?, 'reviewer', 'boris_pr_review', ?, ?, ?)
        """,
        (task_id, at, f"review:{task_id}:{at}", status, json_dumps({"url": url, **details}), SOURCE),
    )
    review_event_id = cur.lastrowid
    for summary in findings or []:
        conn.execute(
            """
            INSERT INTO review_findings(task_id, review_event_id, severity, finding_type, file_path, line, summary, details_json, source)
            VALUES (?, ?, 'blocker', 'acceptance_or_verification_gap', NULL, NULL, ?, ?, ?)
            """,
            (task_id, review_event_id, summary, json_dumps({"url": url}), SOURCE),
        )


def insert_review_block_corrections(conn: sqlite3.Connection, task_id: str, events: list[dict[str, str]]) -> None:
    conn.execute(
        "DELETE FROM corrections WHERE task_id = ? AND source = 'reviewer' AND correction_type IN ('bad_handoff', 'weak_verification')",
        (task_id,),
    )
    for event in events:
        summary = event["summary"]
        lowered = summary.lower()
        correction_type = "weak_verification" if "verification" in lowered or "test" in lowered else "bad_handoff"
        conn.execute(
            """
            INSERT INTO corrections(task_id, occurred_at, source, profile, correction_type, severity, summary, provenance, resolved)
            VALUES (?, ?, 'reviewer', 'reviewer', ?, 'high', ?, 'real', 1)
            """,
            (task_id, event["at"], correction_type, summary),
        )


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    task_id = ticket_task_id(args.ticket_key)
    ticket_key = args.ticket_key.strip().upper()
    owner_profile = canonical_profile(args.owner_profile)
    initial_owner = canonical_profile(args.initial_owner)
    current_owner = canonical_profile(args.current_owner)
    opened_at = args.opened_at or utc_now()
    closed_at = args.closed_at or utc_now()
    pr_created_at = args.pr_created_at or opened_at
    blocked_reviews = [parse_event(value) for value in args.blocked_review]
    remediations = [parse_event(value) for value in args.remediation]
    correction_state = args.correction_state
    if blocked_reviews and correction_state is None:
        if args.review_block_not_correction_reason:
            correction_state = "none"
        else:
            raise SystemExit(
                "--blocked-review requires --correction-state present, or --review-block-not-correction-reason "
                "to explicitly declare why the review block is not a correction"
            )
    if correction_state is None:
        correction_state = "none"
    extra_notes = json.loads(args.notes_json) if args.notes_json else {}
    if not isinstance(extra_notes, dict):
        raise SystemExit("--notes-json must decode to a JSON object")

    notes = {
        "source": SOURCE,
        "ticket_key": ticket_key,
        "repo": args.repo,
        "pr_url": args.pr_url,
        "pr_number": args.pr_number,
        "branch": args.branch,
        "final_commit": args.reviewed_commit,
        "accepted_comment_url": args.acceptance_artifact_url,
        "verification_checked": args.verification,
        "review_required": True,
        "reviewer": "Boris/reviewer",
        "initial_owner": initial_owner,
        "current_owner": current_owner,
        "correct_owner": parse_bool(args.correct_owner),
        "user_corrected": parse_bool(args.user_corrected),
        "correction_state": correction_state,
        "learning_artifact_state": args.learning_artifact_state,
        "blocked_reviews": blocked_reviews,
        "remediations": remediations,
        "review_block_not_correction_reason": args.review_block_not_correction_reason,
    }
    notes.update(extra_notes)

    payload = log_task_closeout.validate({
        "task_id": task_id,
        "title": args.title,
        "user_goal_summary": args.summary,
        "owner_profile": owner_profile,
        "surface": "direct_ticket_hub",
        "status": args.status,
        "outcome": args.outcome,
        "verification_strength": args.verification_strength,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "task_type": "jira_ticket",
        "workdir": args.workdir or None,
        "repo_hint": args.repo_hint or args.repo.split("/")[-1],
        "reopened": bool(blocked_reviews),
        "final_confidence": 0.95 if args.outcome == "success" else None,
        "assisting_profiles": split_csv(args.assisting_profiles),
        "notes": notes,
        "correct_owner": parse_bool(args.correct_owner),
        "user_corrected": parse_bool(args.user_corrected),
        "correction_state": correction_state,
        "learning_artifact_state": args.learning_artifact_state,
    })

    if args.dry_run:
        print(json.dumps({"telemetry_root": str(telemetry_root), "payload": payload}, indent=2, sort_keys=True))
        return 0

    log_task_closeout.upsert_task(payload, telemetry_root)

    with sqlite3.connect(telemetry_root / "events.db") as conn:
        conn.execute("DELETE FROM review_findings WHERE task_id = ? AND source = ?", (task_id, SOURCE))
        conn.execute("DELETE FROM review_events WHERE task_id = ? AND source = ?", (task_id, SOURCE))
        conn.execute("DELETE FROM task_events WHERE task_id = ? AND json_extract(payload_json, '$.source') = ?", (task_id, SOURCE))

        if correction_state == "present" and blocked_reviews:
            insert_review_block_corrections(conn, task_id, blocked_reviews)

        insert_task_event(conn, task_id, opened_at, "owner_assigned", initial_owner, {
            "ticket_key": ticket_key,
            "from": initial_owner,
            "to": current_owner,
            "reason": "direct ticket-hub implementation lane",
        })
        insert_task_event(conn, task_id, pr_created_at, "pr_opened", owner_profile, {
            "ticket_key": ticket_key,
            "repo": args.repo,
            "pr_url": args.pr_url,
            "branch": args.branch,
        })
        insert_task_event(conn, task_id, pr_created_at, "handoff_started", owner_profile, {
            "ticket_key": ticket_key,
            "repo": args.repo,
            "pr_url": args.pr_url,
            "branch": args.branch,
            "handoff_target": "human_engineering_review",
            "meaning": "PR handoff package opened; final send remains gated on Boris acceptance and telemetry closeout",
        })
        insert_task_event(conn, task_id, pr_created_at, "owner_assigned", owner_profile, {
            "ticket_key": ticket_key,
            "from": args.owner_profile,
            "to": "reviewer",
            "reason": "Boris final review gate",
        })
        for event in blocked_reviews:
            insert_task_event(conn, task_id, event["at"], "review_blocked", "reviewer", {
                "ticket_key": ticket_key,
                "url": event["url"],
                "finding": event["summary"],
            })
            insert_task_event(conn, task_id, event["at"], "handoff_blocked", "reviewer", {
                "ticket_key": ticket_key,
                "url": event["url"],
                "blocker": event["summary"],
                "blocked_stage": "boris_final_review",
            })
            insert_review_event(conn, task_id, event["at"], "blocked", event["url"], {"ticket_key": ticket_key}, [event["summary"]])
        for event in remediations:
            insert_task_event(conn, task_id, event["at"], "remediation_pushed", owner_profile, {
                "ticket_key": ticket_key,
                "url": event["url"],
                "summary": event["summary"],
            })
        insert_task_event(conn, task_id, closed_at, "review_accepted", "reviewer", {
            "ticket_key": ticket_key,
            "url": args.acceptance_artifact_url,
            "commit": args.reviewed_commit,
            "verification_checked": args.verification,
        })
        insert_task_event(conn, task_id, closed_at, "handoff_accepted", "reviewer", {
            "ticket_key": ticket_key,
            "url": args.acceptance_artifact_url,
            "pr_url": args.pr_url,
            "commit": args.reviewed_commit,
            "verification_checked": args.verification,
            "accepted_by": "Boris/reviewer",
        })
        insert_task_event(conn, task_id, closed_at, "handoff_resolved", initial_owner, {
            "ticket_key": ticket_key,
            "pr_url": args.pr_url,
            "commit": args.reviewed_commit,
            "resolution": "ready_for_human_review_notification",
            "next_event": "handoff_sent",
        })
        insert_review_event(conn, task_id, closed_at, "accepted_for_human_review", args.acceptance_artifact_url, {
            "ticket_key": ticket_key,
            "pr_url": args.pr_url,
            "reviewed_commit": args.reviewed_commit,
            "verification_checked": args.verification,
            "blockers": "none",
        })
        conn.execute(
            """
            UPDATE tasks
            SET review_required = 1,
                provenance = 'real',
                substantiality = 'substantial',
                telemetry_complete = 1,
                telemetry_gaps_json = '[]',
                created_by_profile = COALESCE(created_by_profile, ?)
            WHERE task_id = ?
            """,
            (initial_owner, task_id),
        )

    print(json.dumps({
        "success": True,
        "task_id": task_id,
        "telemetry_root": str(telemetry_root),
        "telemetry_complete": True,
        "acceptance_artifact_url": args.acceptance_artifact_url,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
