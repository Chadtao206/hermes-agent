#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from common import canonical_profile, canonical_profiles, events_connection, resolve_telemetry_root
from telemetry_evidence import safe_div

DEFAULT_HOME = Path.home() / ".hermes"
PROFILE_DIR = DEFAULT_HOME / "profiles"

DEFAULT_PROFILES = {"default", "engineer", "researcher", "reviewer", "ops", "designer"}
MEMORY_LIKE_TYPES = ("memory", "fact")
SKILL_TYPES = ("skill",)
ROUTING_CONFIDENCE_MIN_SAMPLE = 5
ELIGIBLE_PROVENANCE = "real"
ELIGIBLE_SUBSTANTIALITY = "substantial"


EVIDENCE_INVENTORY_KEYS = (
    "eligible_real_substantial_tasks",
    "real_lightweight_tasks",
    "bootstrap_tasks",
    "synthetic_tasks",
    "seed_tasks",
    "unknown_classification_tasks",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily Hermes self-improvement metrics.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--date", help="Single UTC date to compute (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=1, help="Number of UTC days to compute ending at --date or today")
    return parser.parse_args()


def utc_date(raw: str | None) -> datetime.date:
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return datetime.utcnow().date()


def date_range(end_date, days: int):
    start = end_date - timedelta(days=days - 1)
    current = start
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def parse_payload_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_between(start_raw: str | None, end_raw: str | None) -> float | None:
    start_dt = parse_iso(start_raw)
    end_dt = parse_iso(end_raw)
    if not start_dt or not end_dt:
        return None
    return (end_dt - start_dt).total_seconds()


def routing_accuracy_confidence(sample_size: int) -> str | None:
    if sample_size <= 0:
        return None
    return "provisional" if sample_size < ROUTING_CONFIDENCE_MIN_SAMPLE else "established"


def is_review_required_blocked(event: sqlite3.Row) -> bool:
    if event["event_type"] != "blocked":
        return False
    reason = str(parse_payload_json(event["payload_json"]).get("reason") or "").lower()
    return "review-required" in reason


def count_review_blocked_unblocked_cycles(events: list[sqlite3.Row]) -> int:
    pending_review_blocks = 0
    cycles = 0
    sorted_events = sorted(events, key=lambda event: (event["occurred_at"], event["id"]))
    for event in sorted_events:
        if is_review_required_blocked(event):
            pending_review_blocks += 1
        elif event["event_type"] == "unblocked" and pending_review_blocks:
            cycles += 1
            pending_review_blocks -= 1
    return cycles


def classify_workflow(row: sqlite3.Row) -> str:
    if row["surface"] == "kanban" or row["task_type"] == "kanban":
        return "kanban"
    if row["task_type"]:
        return f"direct:{row['task_type']}"
    return f"direct:{row['surface']}"


def classify_evidence(task_row: sqlite3.Row) -> tuple[str, bool]:
    provenance = (task_row["provenance"] or "").strip().lower()
    substantiality = (task_row["substantiality"] or "").strip().lower()

    if provenance == ELIGIBLE_PROVENANCE and substantiality == ELIGIBLE_SUBSTANTIALITY:
        return ("eligible_real_substantial_tasks", True)
    if provenance == "real" and substantiality == "lightweight":
        return ("real_lightweight_tasks", False)
    if provenance == "bootstrap":
        return ("bootstrap_tasks", False)
    if provenance == "synthetic":
        return ("synthetic_tasks", False)
    if provenance == "seed":
        return ("seed_tasks", False)
    return ("unknown_classification_tasks", False)


def list_profile_roots() -> dict[str, Path]:
    roots = {"default": DEFAULT_HOME}
    if PROFILE_DIR.exists():
        for child in PROFILE_DIR.iterdir():
            if child.is_dir():
                roots[child.name] = child
    return roots


def count_sessions_by_profile(day: str) -> dict[str, int]:
    counts = defaultdict(int)
    for profile, root in list_profile_roots().items():
        state_db = root / "state.db"
        if state_db.exists():
            try:
                with sqlite3.connect(state_db) as state_conn:
                    counts[profile] = int(
                        state_conn.execute(
                            "SELECT COUNT(*) FROM sessions WHERE date(started_at, 'unixepoch') = ?",
                            (day,),
                        ).fetchone()[0]
                        or 0
                    )
                continue
            except sqlite3.Error:
                # Fall through to the legacy JSON-file approximation if a profile
                # state DB is unavailable or from an incompatible schema.
                pass

        sessions_dir = root / "sessions"
        if not sessions_dir.exists():
            continue
        for session_file in sessions_dir.glob("session_*.json"):
            modified_day = datetime.utcfromtimestamp(session_file.stat().st_mtime).date().isoformat()
            if modified_day == day:
                counts[profile] += 1
    return counts


def load_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.row_factory = None
    return rows


def parse_assisting(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return canonical_profiles(str(item).strip() for item in raw if item and str(item).strip())
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return canonical_profiles(chunk.strip() for chunk in str(raw).split(",") if chunk.strip())
    if isinstance(parsed, list):
        return canonical_profiles(str(item).strip() for item in parsed if item and str(item).strip())
    return []


def task_is_direct(task_row, events_for_task, routing_for_task) -> bool:
    owner = canonical_profile(task_row["owner_profile"])
    assisting = [p for p in parse_assisting(task_row["assisting_profiles"]) if p and p != owner]
    if assisting:
        return False
    for event in events_for_task:
        if event["event_type"] == "owner_rerouted":
            return False
    for route in routing_for_task:
        if route["initial_owner"] != route["current_owner"]:
            return False
        if route["was_initial_owner_correct"] == 0:
            return False
    return True


def cumulative_reuse_rate(
    conn: sqlite3.Connection,
    day: str,
    artifact_types: tuple[str, ...],
    source_profile: str | None = None,
) -> float | None:
    placeholders = ",".join(["?"] * len(artifact_types))
    base_args: list[Any] = [*artifact_types, day]
    profile_clause = ""
    if source_profile is not None:
        profile_clause = " AND source_profile = ?"
        base_args.append(source_profile)
    denom = conn.execute(
        f"""
        SELECT COUNT(*) FROM learning_artifacts
        WHERE artifact_type IN ({placeholders})
          AND substr(created_at, 1, 10) <= ?
          AND archived = 0
          {profile_clause}
        """,
        base_args,
    ).fetchone()[0]
    if not denom:
        return None
    numer = conn.execute(
        f"""
        SELECT COUNT(*) FROM learning_artifacts
        WHERE artifact_type IN ({placeholders})
          AND substr(created_at, 1, 10) <= ?
          AND archived = 0
          AND last_reused_at IS NOT NULL
          AND substr(last_reused_at, 1, 10) <= ?
          {profile_clause}
        """,
        [*artifact_types, day, day, *([source_profile] if source_profile is not None else [])],
    ).fetchone()[0]
    return numer / denom


def compute_for_day(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    tasks = load_rows(
        conn,
        "SELECT * FROM tasks WHERE closed_at IS NOT NULL AND substr(closed_at, 1, 10) = ?",
        (day,),
    )
    corrections = load_rows(
        conn,
        "SELECT * FROM corrections WHERE substr(occurred_at, 1, 10) = ?",
        (day,),
    )
    routing = load_rows(
        conn,
        "SELECT * FROM routing_events WHERE substr(occurred_at, 1, 10) = ?",
        (day,),
    )
    canonical_routing_rows = load_rows(
        conn,
        """
        SELECT r1.*
        FROM routing_events r1
        WHERE r1.was_initial_owner_correct IS NOT NULL
          AND substr(r1.occurred_at, 1, 10) = ?
          AND NOT EXISTS (
              SELECT 1
              FROM routing_events r2
              WHERE r2.task_id = r1.task_id
                AND r2.was_initial_owner_correct IS NOT NULL
                AND (
                    r2.occurred_at < r1.occurred_at
                    OR (r2.occurred_at = r1.occurred_at AND r2.id < r1.id)
                )
          )
        """,
        (day,),
    )
    artifacts_created = load_rows(
        conn,
        "SELECT * FROM learning_artifacts WHERE substr(created_at, 1, 10) = ?",
        (day,),
    )
    artifacts_reused = load_rows(
        conn,
        "SELECT * FROM learning_artifacts WHERE last_reused_at IS NOT NULL AND substr(last_reused_at, 1, 10) = ?",
        (day,),
    )
    task_events = load_rows(
        conn,
        "SELECT * FROM task_events WHERE substr(occurred_at, 1, 10) = ?",
        (day,),
    )
    execution_runs = load_rows(
        conn,
        "SELECT * FROM execution_runs WHERE substr(COALESCE(started_at, ended_at), 1, 10) = ?",
        (day,),
    )
    run_state_events = load_rows(
        conn,
        "SELECT * FROM run_state_events WHERE substr(occurred_at, 1, 10) = ?",
        (day,),
    )
    routing_decisions = load_rows(
        conn,
        "SELECT * FROM routing_decisions WHERE substr(occurred_at, 1, 10) = ?",
        (day,),
    )
    task_participants = load_rows(
        conn,
        "SELECT * FROM task_participants WHERE substr(COALESCE(last_seen_at, first_seen_at), 1, 10) = ?",
        (day,),
    )
    review_events = load_rows(
        conn,
        "SELECT * FROM review_events WHERE substr(occurred_at, 1, 10) = ?",
        (day,),
    )
    review_findings = load_rows(
        conn,
        "SELECT rf.* FROM review_findings rf JOIN review_events re ON re.id = rf.review_event_id WHERE substr(re.occurred_at, 1, 10) = ?",
        (day,),
    )

    sessions_by_profile = count_sessions_by_profile(day)

    evidence_inventory = {key: 0 for key in EVIDENCE_INVENTORY_KEYS}
    eligible_tasks = []
    for row in tasks:
        bucket, is_eligible = classify_evidence(row)
        evidence_inventory[bucket] += 1
        if is_eligible:
            eligible_tasks.append(row)

    eligible_task_ids = {row["task_id"] for row in eligible_tasks}
    excluded_task_count = len(tasks) - len(eligible_tasks)

    bench = {
        "date": day,
        "tasks_completed": len(tasks),
        "task_success_rate": None,
        "user_correction_rate": None,
        "reopened_task_rate": None,
        "turns_to_completion": None,
        "tool_calls_per_success": None,
        "tokens_per_success": None,
        "memory_reuse_rate": None,
        "skill_reuse_rate": None,
        "first_owner_routing_accuracy": None,
        "first_owner_routing_accuracy_sample_size": 0,
        "first_owner_routing_accuracy_confidence": None,
        "task_success_num": 0,
        "task_success_den": len(eligible_tasks),
        "user_correction_num": 0,
        "user_correction_den": len(eligible_tasks),
        "reopened_task_num": 0,
        "reopened_task_den": len(eligible_tasks),
        "first_owner_routing_num": 0,
        "first_owner_routing_den": 0,
        "first_owner_routing_coverage_num": 0,
        "first_owner_routing_coverage_den": 0,
        "turns_total": 0.0,
        "turns_n": 0,
        "tool_calls_total": 0.0,
        "tool_calls_n": 0,
        "tokens_total": 0.0,
        "tokens_n": 0,
        "run_attempts_per_task": None,
        "failed_run_rate": None,
        "crash_rate_per_run": None,
        "give_up_rate_per_run": None,
        "median_time_to_first_action": None,
        "median_run_duration": None,
        "reroute_rate": None,
        "reviewer_engagement_rate": None,
        "review_findings_per_review": None,
        "high_severity_finding_rate": None,
        "telemetry_completeness_rate": None,
        **evidence_inventory,
    }

    by_profile: dict[str, dict[str, Any]] = {}
    by_workflow: dict[str, dict[str, Any]] = {}

    corrected_task_ids = {
        row["task_id"]
        for row in corrections
        if row["task_id"] in eligible_task_ids and str(row["provenance"] or "").strip().lower() == ELIGIBLE_PROVENANCE
    }
    success_rows = [row for row in eligible_tasks if row["outcome"] == "success"]
    reopened_rows = [row for row in eligible_tasks if row["reopened"]]

    legacy_canonical_routing_rows = [row for row in canonical_routing_rows if row["task_id"] in eligible_task_ids]
    v2_canonical_routing_by_task: dict[str, dict[str, Any]] = {}
    for row in sorted(routing_decisions, key=lambda item: ((item["sequence_index"] or 0), item["occurred_at"], item["id"])):
        if row["task_id"] not in eligible_task_ids:
            continue
        correctness = row["was_initial_owner_correct"]
        if correctness is None:
            continue
        if row["task_id"] in v2_canonical_routing_by_task:
            continue
        v2_canonical_routing_by_task[row["task_id"]] = {
            "task_id": row["task_id"],
            "initial_owner": canonical_profile(row["initial_owner"]),
            "current_owner": canonical_profile(row["decided_owner"]),
            "was_initial_owner_correct": correctness,
        }
    canonical_routing_rows = list(v2_canonical_routing_by_task.values())
    for row in legacy_canonical_routing_rows:
        if row["task_id"] not in v2_canonical_routing_by_task:
            canonical_routing_rows.append(row)
    successful_routing_decisions = [row for row in canonical_routing_rows if row["was_initial_owner_correct"] == 1]

    task_events_by_task = defaultdict(list)
    for row in task_events:
        task_events_by_task[row["task_id"]].append(row)

    routing_by_task = defaultdict(list)
    for row in routing:
        routing_by_task[row["task_id"]].append(row)

    all_task_events = load_rows(conn, "SELECT task_id, event_type FROM task_events", ())
    events_for_routing_by_task = defaultdict(list)
    for row in all_task_events:
        events_for_routing_by_task[row["task_id"]].append(row)

    all_routing = load_rows(conn, "SELECT task_id, initial_owner, current_owner, was_initial_owner_correct FROM routing_events", ())
    routing_for_directness_by_task = defaultdict(list)
    v2_routing_rows = load_rows(conn, "SELECT task_id, initial_owner, decided_owner, was_initial_owner_correct FROM routing_decisions", ())
    v2_routing_task_ids = {row["task_id"] for row in v2_routing_rows}
    for row in v2_routing_rows:
        routing_for_directness_by_task[row["task_id"]].append({
            "task_id": row["task_id"],
            "initial_owner": canonical_profile(row["initial_owner"]),
            "current_owner": canonical_profile(row["decided_owner"]),
            "was_initial_owner_correct": row["was_initial_owner_correct"],
        })
    for row in all_routing:
        if row["task_id"] in v2_routing_task_ids:
            continue
        routing_for_directness_by_task[row["task_id"]].append({
            "task_id": row["task_id"],
            "initial_owner": canonical_profile(row["initial_owner"]),
            "current_owner": canonical_profile(row["current_owner"]),
            "was_initial_owner_correct": row["was_initial_owner_correct"],
        })

    if eligible_tasks:
        bench["task_success_num"] = len(success_rows)
        bench["task_success_rate"] = safe_div(bench["task_success_num"], bench["task_success_den"])

        bench["user_correction_num"] = len(corrected_task_ids)
        bench["user_correction_rate"] = safe_div(bench["user_correction_num"], bench["user_correction_den"])

        bench["reopened_task_num"] = len(reopened_rows)
        bench["reopened_task_rate"] = safe_div(bench["reopened_task_num"], bench["reopened_task_den"])

        for row in success_rows:
            notes = json.loads(row["notes_json"] or "{}")
            if notes.get("turn_count") is not None:
                bench["turns_total"] += float(notes["turn_count"])
                bench["turns_n"] += 1
            if notes.get("tool_calls") is not None:
                bench["tool_calls_total"] += float(notes["tool_calls"])
                bench["tool_calls_n"] += 1
            if notes.get("tokens_in") is not None or notes.get("tokens_out") is not None:
                bench["tokens_total"] += float((notes.get("tokens_in") or 0) + (notes.get("tokens_out") or 0))
                bench["tokens_n"] += 1

        bench["turns_to_completion"] = safe_div(bench["turns_total"], bench["turns_n"])
        bench["tool_calls_per_success"] = safe_div(bench["tool_calls_total"], bench["tool_calls_n"])
        bench["tokens_per_success"] = safe_div(bench["tokens_total"], bench["tokens_n"])

    bench["memory_reuse_rate"] = cumulative_reuse_rate(conn, day, MEMORY_LIKE_TYPES)
    bench["skill_reuse_rate"] = cumulative_reuse_rate(conn, day, SKILL_TYPES)

    bench["first_owner_routing_den"] = len(canonical_routing_rows)
    bench["first_owner_routing_num"] = len(successful_routing_decisions)
    bench["first_owner_routing_accuracy"] = safe_div(bench["first_owner_routing_num"], bench["first_owner_routing_den"])

    eligible_routed_tasks = {
        row["task_id"]
        for row in eligible_tasks
        if routing_by_task.get(row["task_id"]) or any(decision["task_id"] == row["task_id"] for decision in routing_decisions)
    }
    bench["first_owner_routing_coverage_num"] = len(canonical_routing_rows)
    bench["first_owner_routing_coverage_den"] = len(eligible_routed_tasks)

    bench["first_owner_routing_accuracy_sample_size"] = bench["first_owner_routing_den"]
    bench["first_owner_routing_accuracy_confidence"] = routing_accuracy_confidence(bench["first_owner_routing_den"])

    eligible_execution_runs = [row for row in execution_runs if row["task_id"] in eligible_task_ids]
    run_count = len(eligible_execution_runs)
    bench["run_attempts_per_task"] = safe_div(run_count, len(eligible_tasks))

    failed_runs = [
        row
        for row in eligible_execution_runs
        if row["status"] in {"crashed", "blocked", "reclaimed"}
        or row["outcome"] in {"crashed", "timed_out", "spawn_failed", "blocked", "failed"}
    ]
    bench["failed_run_rate"] = safe_div(len(failed_runs), run_count)

    run_state_rows = [row for row in run_state_events if row["task_id"] in eligible_task_ids]
    crash_events = [row for row in run_state_rows if row["state"] == "crashed"]
    give_up_events = [row for row in run_state_rows if row["state"] == "gave_up"]
    bench["crash_rate_per_run"] = safe_div(len(crash_events), run_count)
    bench["give_up_rate_per_run"] = safe_div(len(give_up_events), run_count)

    time_to_first_action_seconds: list[float] = []
    for row in eligible_tasks:
        value = seconds_between(row["opened_at"], row["first_action_at"])
        if value is not None and value >= 0:
            time_to_first_action_seconds.append(value)
    bench["median_time_to_first_action"] = median_or_none(time_to_first_action_seconds)

    run_duration_seconds: list[float] = []
    for row in eligible_execution_runs:
        value = seconds_between(row["started_at"], row["ended_at"])
        if value is not None and value >= 0:
            run_duration_seconds.append(value)
    bench["median_run_duration"] = median_or_none(run_duration_seconds)

    eligible_decisions = [row for row in routing_decisions if row["task_id"] in eligible_task_ids]
    routed_task_ids = {row["task_id"] for row in eligible_decisions}
    rerouted_task_ids = {row["task_id"] for row in eligible_decisions if (row["sequence_index"] or 0) > 0}
    if not routed_task_ids:
        routed_task_ids = {row["task_id"] for row in canonical_routing_rows}
        rerouted_task_ids = {row["task_id"] for row in canonical_routing_rows if row["initial_owner"] != row["current_owner"]}
    bench["reroute_rate"] = safe_div(len(rerouted_task_ids), len(routed_task_ids))

    required_review_task_ids = {
        row["task_id"]
        for row in eligible_tasks
        if int(row["review_required"] or 0) == 1
    }
    required_review_task_ids.update(
        row["task_id"]
        for row in task_events
        if row["task_id"] in eligible_task_ids and is_review_required_blocked(row)
    )
    participant_review_task_ids = {
        row["task_id"]
        for row in task_participants
        if row["task_id"] in eligible_task_ids and (
            "review" in str(row["role"] or "").lower() or canonical_profile(row["profile"]) == "reviewer"
        )
    }
    review_event_task_ids = {
        row["task_id"]
        for row in review_events
        if row["task_id"] in eligible_task_ids
    }
    reviewer_owned_task_ids = {
        row["task_id"]
        for row in eligible_tasks
        if canonical_profile(row["owner_profile"]) == "reviewer"
    }
    reviewer_task_event_ids = {
        row["task_id"]
        for row in task_events
        if row["task_id"] in eligible_task_ids and canonical_profile(row["profile"]) == "reviewer"
    }
    required_review_task_ids.update(reviewer_owned_task_ids)
    engaged_review_task_ids = (
        participant_review_task_ids
        | review_event_task_ids
        | reviewer_owned_task_ids
        | reviewer_task_event_ids
    )
    bench["reviewer_engagement_rate"] = safe_div(
        len(required_review_task_ids & engaged_review_task_ids),
        len(required_review_task_ids),
    )

    bench["review_findings_per_review"] = safe_div(len(review_findings), len(review_events))
    high_severity_findings = [
        row for row in review_findings if str(row["severity"] or "").lower() in {"high", "critical", "blocker"}
    ]
    bench["high_severity_finding_rate"] = safe_div(len(high_severity_findings), len(review_findings))

    telemetry_complete_count = sum(1 for row in eligible_tasks if int(row["telemetry_complete"] or 0) == 1)
    bench["telemetry_completeness_rate"] = safe_div(telemetry_complete_count, len(eligible_tasks))

    profiles = set(DEFAULT_PROFILES)
    profiles.update(sessions_by_profile.keys())
    profiles.update(canonical_profile(row["owner_profile"]) for row in tasks if row["owner_profile"])
    profiles.update(canonical_profile(row["source_profile"]) for row in artifacts_created if row["source_profile"])

    for profile in sorted(profiles):
        profile_tasks_all = [row for row in tasks if canonical_profile(row["owner_profile"]) == profile]
        profile_tasks = [row for row in profile_tasks_all if row["task_id"] in eligible_task_ids]
        profile_task_ids = {row["task_id"] for row in profile_tasks}
        profile_success_rows = [row for row in profile_tasks if row["outcome"] == "success"]
        profile_corrections = [
            row
            for row in corrections
            if row["task_id"] in profile_task_ids and str(row["provenance"] or "").strip().lower() == ELIGIBLE_PROVENANCE
        ]
        profile_initial_routing = [
            row for row in canonical_routing_rows if canonical_profile(row["initial_owner"]) == profile
        ]
        profile_successful_routing = [row for row in profile_initial_routing if row["was_initial_owner_correct"] == 1]
        profile_artifacts_created = [row for row in artifacts_created if canonical_profile(row["source_profile"]) == profile]

        direct_count = 0
        for row in profile_tasks:
            events_for_task = events_for_routing_by_task.get(row["task_id"], [])
            routing_for_task = routing_for_directness_by_task.get(row["task_id"], [])
            if task_is_direct(row, events_for_task, routing_for_task):
                direct_count += 1

        memory_growth_count = sum(1 for row in profile_artifacts_created if row["artifact_type"] in {"memory", "fact"})

        by_profile[profile] = {
            "date": day,
            "profile": profile,
            "sessions": sessions_by_profile.get(profile, 0),
            "tasks_completed": len(profile_tasks),
            "eligible_tasks": len(profile_tasks),
            "excluded_tasks": len(profile_tasks_all) - len(profile_tasks),
            "task_success_rate": safe_div(len(profile_success_rows), len(profile_tasks)) if profile_tasks else None,
            "user_correction_rate": safe_div(len({row['task_id'] for row in profile_corrections}), len(profile_tasks)) if profile_tasks else None,
            "routing_accuracy": safe_div(len(profile_successful_routing), len(profile_initial_routing)) if profile_initial_routing else None,
            "memory_growth_count": memory_growth_count,
            "memory_reuse_rate": cumulative_reuse_rate(conn, day, MEMORY_LIKE_TYPES, source_profile=profile),
            "skill_reuse_rate": cumulative_reuse_rate(conn, day, SKILL_TYPES, source_profile=profile),
            "direct_execution_share": safe_div(direct_count, len(profile_tasks)) if profile_tasks else None,
        }

    workflows = defaultdict(list)
    for row in tasks:
        workflows[classify_workflow(row)].append(row)

    for workflow, workflow_tasks_all in workflows.items():
        workflow_tasks = [row for row in workflow_tasks_all if row["task_id"] in eligible_task_ids]
        workflow_task_ids = {row["task_id"] for row in workflow_tasks}
        workflow_success = [row for row in workflow_tasks if row["outcome"] == "success"]
        workflow_reopened = [row for row in workflow_tasks if row["reopened"]]
        workflow_corrected_count = len(workflow_task_ids & corrected_task_ids)

        handoff_counts = []
        blocked_event_counts = []
        generic_blocked_event_counts = []
        review_blocked_unblocked_cycle_counts = []
        crash_event_counts = []
        give_up_event_counts = []
        for row in workflow_tasks:
            events_for_task = task_events_by_task.get(row["task_id"], [])
            handoff_counts.append(sum(1 for event in events_for_task if str(event["event_type"] or "").startswith("handoff_")))
            blocked_event_counts.append(sum(1 for event in events_for_task if event["event_type"] == "blocked"))
            generic_blocked_event_counts.append(sum(1 for event in events_for_task if event["event_type"] == "blocked" and not is_review_required_blocked(event)))
            review_blocked_unblocked_cycle_counts.append(count_review_blocked_unblocked_cycles(events_for_task))
            crash_event_counts.append(sum(1 for event in events_for_task if event["event_type"] == "kanban_crashed"))
            give_up_event_counts.append(sum(1 for event in events_for_task if event["event_type"] == "kanban_gave_up"))

        workflow_runs = [row for row in eligible_execution_runs if row["task_id"] in workflow_task_ids]
        workflow_run_states = [row for row in run_state_rows if row["task_id"] in workflow_task_ids]
        workflow_decisions = [row for row in eligible_decisions if row["task_id"] in workflow_task_ids]
        workflow_review_required = required_review_task_ids & workflow_task_ids
        workflow_review_engaged = engaged_review_task_ids & workflow_task_ids
        workflow_telemetry_complete = sum(1 for row in workflow_tasks if int(row["telemetry_complete"] or 0) == 1)

        workflow_routed = {row["task_id"] for row in workflow_decisions}
        workflow_rerouted = {row["task_id"] for row in workflow_decisions if (row["sequence_index"] or 0) > 0}

        by_workflow[workflow] = {
            "date": day,
            "workflow": workflow,
            "tasks_completed": len(workflow_tasks),
            "eligible_tasks": len(workflow_tasks),
            "excluded_tasks": len(workflow_tasks_all) - len(workflow_tasks),
            "task_success_rate": safe_div(len(workflow_success), len(workflow_tasks)) if workflow_tasks else None,
            "user_correction_rate": safe_div(workflow_corrected_count, len(workflow_tasks)) if workflow_tasks else None,
            "reopened_task_rate": safe_div(len(workflow_reopened), len(workflow_tasks)) if workflow_tasks else None,
            "avg_handoff_count": safe_div(sum(handoff_counts), len(handoff_counts)) if handoff_counts else None,
            "avg_blocked_events": safe_div(sum(blocked_event_counts), len(blocked_event_counts)) if blocked_event_counts else None,
            "avg_generic_blocked_events": safe_div(sum(generic_blocked_event_counts), len(generic_blocked_event_counts)) if generic_blocked_event_counts else None,
            "avg_review_blocked_unblocked_cycles": safe_div(sum(review_blocked_unblocked_cycle_counts), len(review_blocked_unblocked_cycle_counts)) if review_blocked_unblocked_cycle_counts else None,
            "avg_kanban_crashed_events": safe_div(sum(crash_event_counts), len(crash_event_counts)) if crash_event_counts else None,
            "avg_kanban_gave_up_events": safe_div(sum(give_up_event_counts), len(give_up_event_counts)) if give_up_event_counts else None,
            "run_attempts_per_task": safe_div(len(workflow_runs), len(workflow_tasks)),
            "failed_run_rate": safe_div(
                len([
                    row for row in workflow_runs
                    if row["status"] in {"crashed", "blocked", "reclaimed"}
                    or row["outcome"] in {"crashed", "timed_out", "spawn_failed", "blocked", "failed"}
                ]),
                len(workflow_runs),
            ),
            "crash_rate_per_run": safe_div(
                len([row for row in workflow_run_states if row["state"] == "crashed"]),
                len(workflow_runs),
            ),
            "give_up_rate_per_run": safe_div(
                len([row for row in workflow_run_states if row["state"] == "gave_up"]),
                len(workflow_runs),
            ),
            "reroute_rate": safe_div(len(workflow_rerouted), len(workflow_routed)),
            "reviewer_engagement_rate": safe_div(len(workflow_review_required & workflow_review_engaged), len(workflow_review_required)),
            "telemetry_completeness_rate": safe_div(workflow_telemetry_complete, len(workflow_tasks)),
        }

    return {"bench": bench, "profiles": by_profile, "workflows": by_workflow, "excluded_task_count": excluded_task_count}


def write_metrics(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    bench = payload["bench"]
    # Recompute profile/workflow rows from scratch for the day so old split
    # identities such as Jensen/jensen disappear after canonicalization.
    conn.execute("DELETE FROM profile_metrics_daily WHERE date = ?", (bench["date"],))
    conn.execute("DELETE FROM workflow_metrics_daily WHERE date = ?", (bench["date"],))
    conn.execute(
        """
        INSERT INTO bench_metrics_daily(
            date, tasks_completed, task_success_rate, user_correction_rate, reopened_task_rate,
            turns_to_completion, tool_calls_per_success, tokens_per_success,
            memory_reuse_rate, skill_reuse_rate, first_owner_routing_accuracy,
            first_owner_routing_accuracy_sample_size, first_owner_routing_accuracy_confidence,
            task_success_num, task_success_den, user_correction_num, user_correction_den,
            reopened_task_num, reopened_task_den,
            first_owner_routing_num, first_owner_routing_den,
            first_owner_routing_coverage_num, first_owner_routing_coverage_den,
            turns_total, turns_n, tool_calls_total, tool_calls_n, tokens_total, tokens_n,
            eligible_real_substantial_tasks, real_lightweight_tasks, bootstrap_tasks,
            synthetic_tasks, seed_tasks, unknown_classification_tasks,
            run_attempts_per_task, failed_run_rate, crash_rate_per_run, give_up_rate_per_run,
            median_time_to_first_action, median_run_duration, reroute_rate,
            reviewer_engagement_rate, review_findings_per_review, high_severity_finding_rate,
            telemetry_completeness_rate
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            tasks_completed = excluded.tasks_completed,
            task_success_rate = excluded.task_success_rate,
            user_correction_rate = excluded.user_correction_rate,
            reopened_task_rate = excluded.reopened_task_rate,
            turns_to_completion = excluded.turns_to_completion,
            tool_calls_per_success = excluded.tool_calls_per_success,
            tokens_per_success = excluded.tokens_per_success,
            memory_reuse_rate = excluded.memory_reuse_rate,
            skill_reuse_rate = excluded.skill_reuse_rate,
            first_owner_routing_accuracy = excluded.first_owner_routing_accuracy,
            first_owner_routing_accuracy_sample_size = excluded.first_owner_routing_accuracy_sample_size,
            first_owner_routing_accuracy_confidence = excluded.first_owner_routing_accuracy_confidence,
            task_success_num = excluded.task_success_num,
            task_success_den = excluded.task_success_den,
            user_correction_num = excluded.user_correction_num,
            user_correction_den = excluded.user_correction_den,
            reopened_task_num = excluded.reopened_task_num,
            reopened_task_den = excluded.reopened_task_den,
            first_owner_routing_num = excluded.first_owner_routing_num,
            first_owner_routing_den = excluded.first_owner_routing_den,
            first_owner_routing_coverage_num = excluded.first_owner_routing_coverage_num,
            first_owner_routing_coverage_den = excluded.first_owner_routing_coverage_den,
            turns_total = excluded.turns_total,
            turns_n = excluded.turns_n,
            tool_calls_total = excluded.tool_calls_total,
            tool_calls_n = excluded.tool_calls_n,
            tokens_total = excluded.tokens_total,
            tokens_n = excluded.tokens_n,
            eligible_real_substantial_tasks = excluded.eligible_real_substantial_tasks,
            real_lightweight_tasks = excluded.real_lightweight_tasks,
            bootstrap_tasks = excluded.bootstrap_tasks,
            synthetic_tasks = excluded.synthetic_tasks,
            seed_tasks = excluded.seed_tasks,
            unknown_classification_tasks = excluded.unknown_classification_tasks,
            run_attempts_per_task = excluded.run_attempts_per_task,
            failed_run_rate = excluded.failed_run_rate,
            crash_rate_per_run = excluded.crash_rate_per_run,
            give_up_rate_per_run = excluded.give_up_rate_per_run,
            median_time_to_first_action = excluded.median_time_to_first_action,
            median_run_duration = excluded.median_run_duration,
            reroute_rate = excluded.reroute_rate,
            reviewer_engagement_rate = excluded.reviewer_engagement_rate,
            review_findings_per_review = excluded.review_findings_per_review,
            high_severity_finding_rate = excluded.high_severity_finding_rate,
            telemetry_completeness_rate = excluded.telemetry_completeness_rate
        """,
        (
            bench["date"],
            bench["tasks_completed"],
            bench["task_success_rate"],
            bench["user_correction_rate"],
            bench["reopened_task_rate"],
            bench["turns_to_completion"],
            bench["tool_calls_per_success"],
            bench["tokens_per_success"],
            bench["memory_reuse_rate"],
            bench["skill_reuse_rate"],
            bench["first_owner_routing_accuracy"],
            bench["first_owner_routing_accuracy_sample_size"],
            bench["first_owner_routing_accuracy_confidence"],
            bench["task_success_num"],
            bench["task_success_den"],
            bench["user_correction_num"],
            bench["user_correction_den"],
            bench["reopened_task_num"],
            bench["reopened_task_den"],
            bench["first_owner_routing_num"],
            bench["first_owner_routing_den"],
            bench["first_owner_routing_coverage_num"],
            bench["first_owner_routing_coverage_den"],
            bench["turns_total"],
            bench["turns_n"],
            bench["tool_calls_total"],
            bench["tool_calls_n"],
            bench["tokens_total"],
            bench["tokens_n"],
            bench["eligible_real_substantial_tasks"],
            bench["real_lightweight_tasks"],
            bench["bootstrap_tasks"],
            bench["synthetic_tasks"],
            bench["seed_tasks"],
            bench["unknown_classification_tasks"],
            bench["run_attempts_per_task"],
            bench["failed_run_rate"],
            bench["crash_rate_per_run"],
            bench["give_up_rate_per_run"],
            bench["median_time_to_first_action"],
            bench["median_run_duration"],
            bench["reroute_rate"],
            bench["reviewer_engagement_rate"],
            bench["review_findings_per_review"],
            bench["high_severity_finding_rate"],
            bench["telemetry_completeness_rate"],
        ),
    )

    for profile_payload in payload["profiles"].values():
        conn.execute(
            """
            INSERT INTO profile_metrics_daily(
                date, profile, sessions, tasks_completed, eligible_tasks, excluded_tasks,
                task_success_rate, user_correction_rate, routing_accuracy, memory_growth_count,
                memory_reuse_rate, skill_reuse_rate, direct_execution_share
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, profile) DO UPDATE SET
                sessions = excluded.sessions,
                tasks_completed = excluded.tasks_completed,
                eligible_tasks = excluded.eligible_tasks,
                excluded_tasks = excluded.excluded_tasks,
                task_success_rate = excluded.task_success_rate,
                user_correction_rate = excluded.user_correction_rate,
                routing_accuracy = excluded.routing_accuracy,
                memory_growth_count = excluded.memory_growth_count,
                memory_reuse_rate = excluded.memory_reuse_rate,
                skill_reuse_rate = excluded.skill_reuse_rate,
                direct_execution_share = excluded.direct_execution_share
            """,
            (
                profile_payload["date"],
                profile_payload["profile"],
                profile_payload["sessions"],
                profile_payload["tasks_completed"],
                profile_payload["eligible_tasks"],
                profile_payload["excluded_tasks"],
                profile_payload["task_success_rate"],
                profile_payload["user_correction_rate"],
                profile_payload["routing_accuracy"],
                profile_payload["memory_growth_count"],
                profile_payload["memory_reuse_rate"],
                profile_payload["skill_reuse_rate"],
                profile_payload["direct_execution_share"],
            ),
        )

    for workflow_payload in payload.get("workflows", {}).values():
        conn.execute(
            """
            INSERT INTO workflow_metrics_daily(
                date, workflow, tasks_completed, eligible_tasks, excluded_tasks,
                task_success_rate, user_correction_rate, reopened_task_rate, avg_handoff_count, avg_blocked_events,
                avg_generic_blocked_events, avg_review_blocked_unblocked_cycles,
                avg_kanban_crashed_events, avg_kanban_gave_up_events,
                run_attempts_per_task, failed_run_rate, crash_rate_per_run,
                give_up_rate_per_run, reroute_rate, reviewer_engagement_rate,
                telemetry_completeness_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, workflow) DO UPDATE SET
                tasks_completed = excluded.tasks_completed,
                eligible_tasks = excluded.eligible_tasks,
                excluded_tasks = excluded.excluded_tasks,
                task_success_rate = excluded.task_success_rate,
                user_correction_rate = excluded.user_correction_rate,
                reopened_task_rate = excluded.reopened_task_rate,
                avg_handoff_count = excluded.avg_handoff_count,
                avg_blocked_events = excluded.avg_blocked_events,
                avg_generic_blocked_events = excluded.avg_generic_blocked_events,
                avg_review_blocked_unblocked_cycles = excluded.avg_review_blocked_unblocked_cycles,
                avg_kanban_crashed_events = excluded.avg_kanban_crashed_events,
                avg_kanban_gave_up_events = excluded.avg_kanban_gave_up_events,
                run_attempts_per_task = excluded.run_attempts_per_task,
                failed_run_rate = excluded.failed_run_rate,
                crash_rate_per_run = excluded.crash_rate_per_run,
                give_up_rate_per_run = excluded.give_up_rate_per_run,
                reroute_rate = excluded.reroute_rate,
                reviewer_engagement_rate = excluded.reviewer_engagement_rate,
                telemetry_completeness_rate = excluded.telemetry_completeness_rate
            """,
            (
                workflow_payload["date"],
                workflow_payload["workflow"],
                workflow_payload["tasks_completed"],
                workflow_payload["eligible_tasks"],
                workflow_payload["excluded_tasks"],
                workflow_payload["task_success_rate"],
                workflow_payload["user_correction_rate"],
                workflow_payload["reopened_task_rate"],
                workflow_payload["avg_handoff_count"],
                workflow_payload["avg_blocked_events"],
                workflow_payload["avg_generic_blocked_events"],
                workflow_payload["avg_review_blocked_unblocked_cycles"],
                workflow_payload["avg_kanban_crashed_events"],
                workflow_payload["avg_kanban_gave_up_events"],
                workflow_payload["run_attempts_per_task"],
                workflow_payload["failed_run_rate"],
                workflow_payload["crash_rate_per_run"],
                workflow_payload["give_up_rate_per_run"],
                workflow_payload["reroute_rate"],
                workflow_payload["reviewer_engagement_rate"],
                workflow_payload["telemetry_completeness_rate"],
            ),
        )


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    end_date = utc_date(args.date)
    written = []
    with events_connection(telemetry_root) as conn:
        for day in date_range(end_date, args.days):
            payload = compute_for_day(conn, day.isoformat())
            write_metrics(conn, payload)
            written.append(payload)

    print(json.dumps({"days_written": [item["bench"]["date"] for item in written], "latest": written[-1] if written else None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
