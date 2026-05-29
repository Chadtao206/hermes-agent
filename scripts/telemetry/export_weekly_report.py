#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from common import ensure_initialized, resolve_telemetry_root
from telemetry_evidence import aggregate_mean, aggregate_proportion, confidence_label, safe_div, wilson_interval


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a weekly Hermes self-improvement markdown report.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--end-date", help="UTC end date in YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--days", type=int, default=7, help="Days in the primary reporting window")
    return parser.parse_args()


def parse_date(raw: str | None):
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return datetime.utcnow().date()


def load_rows(conn: sqlite3.Connection, query: str, params=()):
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.row_factory = None


def fmt_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def fmt_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 3600:
        return f"{value / 3600:.2f}h"
    if value >= 60:
        return f"{value / 60:.1f}m"
    return f"{value:.0f}s"


def fmt_delta(current: float | None, baseline: float | None, pct: bool = False) -> str:
    if current is None or baseline is None:
        return "n/a"
    delta = current - baseline
    if pct:
        return f"{delta * 100:+.1f}pp"
    return f"{delta:+.2f}"


def fmt_ci(successes: int, total: int) -> str:
    low, high = wilson_interval(successes, total)
    if low is None or high is None:
        return "n/a"
    return f"[{low * 100:.1f}%, {high * 100:.1f}%]"


def median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def parse_iso(raw: str | None):
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


def parse_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}




def blocked_durations_from_events(events: list[sqlite3.Row]) -> list[float]:
    durations: list[float] = []
    by_task: dict[str, list[sqlite3.Row]] = {}
    for event in events:
        by_task.setdefault(event["task_id"], []).append(event)
    for task_events in by_task.values():
        pending: list[datetime] = []
        for event in sorted(task_events, key=lambda row: (row["occurred_at"], row["id"])):
            event_type = str(event["event_type"] or "")
            occurred = parse_iso(event["occurred_at"])
            if not occurred:
                continue
            if event_type == "blocked":
                pending.append(occurred)
            elif event_type in {"unblocked", "kanban_completed", "task_completed"} and pending:
                start = pending.pop(0)
                delta = (occurred - start).total_seconds()
                if delta >= 0:
                    durations.append(delta)
    return durations


def count_rows_with_state(rows: list[sqlite3.Row], states: set[str]) -> int:
    return sum(1 for row in rows if str(row["state"] or "").lower() in states)

def fetch_window_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = load_rows(conn, "SELECT * FROM bench_metrics_daily WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))
    return [dict(row) for row in rows]


def summarize_window(rows: list[dict]) -> dict:
    inventory = {
        "total_closed": sum(int(row.get("tasks_completed") or 0) for row in rows),
        "eligible_real_substantial_tasks": sum(int(row.get("eligible_real_substantial_tasks") or 0) for row in rows),
        "real_lightweight_tasks": sum(int(row.get("real_lightweight_tasks") or 0) for row in rows),
        "bootstrap_tasks": sum(int(row.get("bootstrap_tasks") or 0) for row in rows),
        "synthetic_tasks": sum(int(row.get("synthetic_tasks") or 0) for row in rows),
        "seed_tasks": sum(int(row.get("seed_tasks") or 0) for row in rows),
        "unknown_classification_tasks": sum(int(row.get("unknown_classification_tasks") or 0) for row in rows),
    }
    contaminated = inventory["unknown_classification_tasks"] > 0

    decision = {}
    for metric, num_key, den_key in (
        ("task_success_rate", "task_success_num", "task_success_den"),
        ("user_correction_rate", "user_correction_num", "user_correction_den"),
        ("reopened_task_rate", "reopened_task_num", "reopened_task_den"),
        ("first_owner_routing_accuracy", "first_owner_routing_num", "first_owner_routing_den"),
    ):
        num, den, value = aggregate_proportion(rows, num_key, den_key)
        decision[metric] = {
            "numerator": num,
            "denominator": den,
            "value": value,
            "confidence": confidence_label(den, contaminated=contaminated),
            "ci": fmt_ci(num, den),
        }

    coverage_num, coverage_den, coverage_value = aggregate_proportion(rows, "first_owner_routing_coverage_num", "first_owner_routing_coverage_den")
    decision["first_owner_routing_coverage"] = {
        "numerator": coverage_num,
        "denominator": coverage_den,
        "value": coverage_value,
        "confidence": confidence_label(coverage_den, contaminated=contaminated),
        "ci": fmt_ci(coverage_num, coverage_den),
    }

    successful_pool = decision["task_success_rate"]["numerator"]
    for metric, total_key, n_key in (
        ("turns_to_completion", "turns_total", "turns_n"),
        ("tool_calls_per_success", "tool_calls_total", "tool_calls_n"),
        ("tokens_per_success", "tokens_total", "tokens_n"),
    ):
        total, n_used, value = aggregate_mean(rows, total_key, n_key)
        decision[metric] = {
            "total": total,
            "n_used": n_used,
            "eligible_pool": successful_pool,
            "value": value,
            "confidence": confidence_label(n_used, contaminated=contaminated),
        }

    return {"inventory": inventory, "contaminated": contaminated, "decision": decision}


def build_recommendation(current: dict, baseline: dict) -> str:
    decision = current["decision"]
    if current["contaminated"]:
        return "Not scoreable for recommendations: missing provenance/substantiality labels contaminate headline denominators."
    confidence_floor = {
        decision["task_success_rate"]["confidence"],
        decision["user_correction_rate"]["confidence"],
        decision["reopened_task_rate"]["confidence"],
    }
    if "insufficient" in confidence_floor:
        return "Collect more real+substantial evidence before acting; headline confidence is insufficient."
    cur_success = decision["task_success_rate"]["value"]
    cur_corr = decision["user_correction_rate"]["value"]
    cur_reopen = decision["reopened_task_rate"]["value"]
    base_success = baseline["decision"]["task_success_rate"]["value"]
    base_corr = baseline["decision"]["user_correction_rate"]["value"]
    base_reopen = baseline["decision"]["reopened_task_rate"]["value"]
    if None in {cur_success, cur_corr, cur_reopen, base_success, base_corr, base_reopen}:
        return "Collect more evidence; baseline or current headline metrics are incomplete."
    improved = (cur_success >= base_success) and (cur_corr <= base_corr) and (cur_reopen <= base_reopen)
    if improved:
        return "Eligible evidence is trending in the right direction. Keep current approach and continue sampling."
    return "Headline evidence mixed/regressing. Investigate workflow changes before scaling current approach."


def window_eligible_tasks(conn: sqlite3.Connection, start: str, end: str):
    return load_rows(
        conn,
        """
        SELECT * FROM tasks
        WHERE closed_at IS NOT NULL
          AND substr(closed_at, 1, 10) BETWEEN ? AND ?
          AND lower(coalesce(provenance, '')) = 'real'
          AND lower(coalesce(substantiality, '')) = 'substantial'
        ORDER BY closed_at, task_id
        """,
        (start, end),
    )


def latest_reporting_day(conn: sqlite3.Connection, end: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) FROM profile_metrics_daily WHERE date <= ?",
        (end,),
    ).fetchone()
    return row[0] if row and row[0] else None


def load_profile_activation_rows(conn: sqlite3.Connection, day: str | None):
    if not day:
        return []
    return load_rows(
        conn,
        "SELECT * FROM profile_metrics_daily WHERE date = ? ORDER BY profile",
        (day,),
    )


def routing_decision_rows(conn: sqlite3.Connection, task_ids: list[str], start: str, end: str):
    if not task_ids:
        return []
    placeholders = ",".join("?" for _ in task_ids)
    rows = load_rows(
        conn,
        f"SELECT * FROM routing_decisions WHERE task_id IN ({placeholders}) AND substr(occurred_at, 1, 10) BETWEEN ? AND ? ORDER BY sequence_index, occurred_at, id",
        (*task_ids, start, end),
    )
    if rows:
        return rows
    return load_rows(
        conn,
        f"SELECT task_id, occurred_at, 0 AS sequence_index, initial_owner, current_owner AS decided_owner, was_initial_owner_correct FROM routing_events WHERE task_id IN ({placeholders}) AND substr(occurred_at, 1, 10) BETWEEN ? AND ? ORDER BY occurred_at, id",
        (*task_ids, start, end),
    )


def operational_summary(conn: sqlite3.Connection, start: str, end: str) -> dict:
    eligible_tasks = window_eligible_tasks(conn, start, end)
    eligible_task_ids = [row["task_id"] for row in eligible_tasks]
    if not eligible_task_ids:
        return {
            "eligible_tasks": 0,
            "runtime_coverage": None,
            "run_attempts_per_task": None,
            "failed_run_rate": None,
            "crash_rate_per_run": None,
            "give_up_rate_per_run": None,
            "protocol_violation_events": 0,
            "spawn_failure_events": 0,
            "reclaim_events": 0,
            "respawn_guard_events": 0,
            "claim_extension_events": 0,
            "heartbeat_events": 0,
            "stale_claim_or_reclaim_rate": None,
            "spawn_failure_rate_per_run": None,
            "protocol_violation_rate_per_run": None,
            "median_blocked_duration": None,
            "median_queue_wait": None,
            "median_time_to_first_action": None,
            "median_run_duration": None,
            "reroute_rate": None,
            "reviewer_engagement_rate": None,
            "review_findings_per_review": None,
            "high_severity_finding_rate": None,
            "telemetry_completeness_rate": None,
            "all_closed_telemetry_completeness_rate": None,
            "top_gaps": [],
            "required_review_tasks": 0,
            "reviewed_tasks": 0,
            "review_events": 0,
            "review_findings": 0,
            "handoff_started_tasks": 0,
            "handoff_accepted_tasks": 0,
            "handoff_resolved_tasks": 0,
            "handoff_sent_tasks": 0,
            "handoff_event_tasks": 0,
            "handoff_resolution_rate": None,
            "handoff_send_rate": None,
        }

    placeholders = ",".join("?" for _ in eligible_task_ids)
    execution_runs = load_rows(conn, f"SELECT * FROM execution_runs WHERE task_id IN ({placeholders})", tuple(eligible_task_ids))
    run_state_events = load_rows(conn, f"SELECT * FROM run_state_events WHERE task_id IN ({placeholders})", tuple(eligible_task_ids))
    routing_decisions = routing_decision_rows(conn, eligible_task_ids, start, end)
    task_participants = load_rows(conn, f"SELECT * FROM task_participants WHERE task_id IN ({placeholders})", tuple(eligible_task_ids))
    task_events = load_rows(conn, f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) AND substr(occurred_at, 1, 10) BETWEEN ? AND ?", (*eligible_task_ids, start, end))
    all_task_events = load_rows(conn, f"SELECT * FROM task_events WHERE task_id IN ({placeholders})", tuple(eligible_task_ids))
    review_events = load_rows(conn, f"SELECT * FROM review_events WHERE task_id IN ({placeholders}) AND substr(occurred_at, 1, 10) BETWEEN ? AND ?", (*eligible_task_ids, start, end))
    review_findings = load_rows(conn, f"SELECT rf.* FROM review_findings rf JOIN review_events re ON re.id = rf.review_event_id WHERE rf.task_id IN ({placeholders}) AND substr(re.occurred_at, 1, 10) BETWEEN ? AND ?", (*eligible_task_ids, start, end))

    routed_task_ids = {row["task_id"] for row in routing_decisions}
    rerouted_task_ids = {row["task_id"] for row in routing_decisions if int(row["sequence_index"] or 0) > 0}

    runtime_task_ids = {row["task_id"] for row in execution_runs}
    failed_runs = [row for row in execution_runs if str(row["status"] or "").lower() in {"crashed", "blocked", "reclaimed"} or str(row["outcome"] or "").lower() in {"crashed", "timed_out", "spawn_failed", "blocked", "failed"}]
    crash_events = [row for row in run_state_events if str(row["state"] or "").lower() == "crashed"]
    give_up_events = [row for row in run_state_events if str(row["state"] or "").lower() == "gave_up"]
    spawn_failure_events = [row for row in run_state_events if str(row["state"] or "").lower() == "spawn_failed"]
    reclaim_events = [row for row in run_state_events if str(row["state"] or "").lower() == "reclaimed"]
    respawn_guard_events = [row for row in run_state_events if str(row["state"] or "").lower() == "respawn_guarded"]
    claim_extension_events = [row for row in run_state_events if str(row["state"] or "").lower() == "claim_extended"]
    heartbeat_events = [row for row in run_state_events if str(row["state"] or "").lower() == "heartbeat"]
    # Count explicit protocol-violation events only. Error/summary text often
    # repeats the same violation on crashed/gave_up rows, so treating text
    # matches as additional events overstates the severity.
    protocol_violation_events = [
        row for row in task_events
        if str(row["event_type"] or "").lower() == "protocol_violation"
    ]
    protocol_violation_events.extend(
        row for row in run_state_events
        if str(row["state"] or "").lower() == "protocol_violation"
    )

    first_action_values = []
    for row in eligible_tasks:
        delta = seconds_between(row["opened_at"], row["first_action_at"])
        if delta is not None and delta >= 0:
            first_action_values.append(delta)
    run_duration_values = []
    for row in execution_runs:
        delta = seconds_between(row["started_at"], row["ended_at"])
        if delta is not None and delta >= 0:
            run_duration_values.append(delta)

    required_review_task_ids = {row["task_id"] for row in eligible_tasks if int(row["review_required"] or 0) == 1}
    participant_review_task_ids = {
        row["task_id"]
        for row in task_participants
        if "review" in str(row["role"] or "").lower() or str(row["profile"] or "").lower() == "reviewer"
    }
    review_event_task_ids = {row["task_id"] for row in review_events}
    reviewed_task_ids = participant_review_task_ids | review_event_task_ids

    high_severity_findings = [row for row in review_findings if str(row["severity"] or "").lower() in {"high", "critical", "blocker"}]

    handoff_started_task_ids = {row["task_id"] for row in task_events if row["event_type"] == "handoff_started"}
    handoff_accepted_task_ids = {row["task_id"] for row in task_events if row["event_type"] == "handoff_accepted"}
    handoff_resolved_task_ids = {row["task_id"] for row in task_events if row["event_type"] == "handoff_resolved"}
    handoff_sent_task_ids = {row["task_id"] for row in task_events if row["event_type"] == "handoff_sent"}
    handoff_event_task_ids = {row["task_id"] for row in task_events if str(row["event_type"] or "").startswith("handoff_")}
    blocked_duration_values = blocked_durations_from_events(all_task_events)
    queue_wait_values = []
    for row in eligible_tasks:
        delta = seconds_between(row["opened_at"], row["first_action_at"])
        if delta is not None and delta >= 0:
            queue_wait_values.append(delta)

    all_closed = load_rows(conn, "SELECT task_id, telemetry_complete, telemetry_gaps_json FROM tasks WHERE closed_at IS NOT NULL AND substr(closed_at, 1, 10) BETWEEN ? AND ?", (start, end))
    gap_counter = Counter()
    for row in all_closed:
        for gap in parse_payload(row["telemetry_gaps_json"] if row["telemetry_gaps_json"] and row["telemetry_gaps_json"].startswith('{') else None).values():
            pass
        raw = row["telemetry_gaps_json"]
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = []
        if isinstance(parsed, list):
            for gap in parsed:
                gap_counter[str(gap)] += 1

    telemetry_complete_eligible = sum(1 for row in eligible_tasks if int(row["telemetry_complete"] or 0) == 1)
    telemetry_complete_all = sum(1 for row in all_closed if int(row["telemetry_complete"] or 0) == 1)

    return {
        "eligible_tasks": len(eligible_tasks),
        "runtime_coverage": safe_div(len(runtime_task_ids), len(eligible_tasks)),
        "run_attempts_per_task": safe_div(len(execution_runs), len(eligible_tasks)),
        "failed_run_rate": safe_div(len(failed_runs), len(execution_runs)),
        "crash_rate_per_run": safe_div(len(crash_events), len(execution_runs)),
        "give_up_rate_per_run": safe_div(len(give_up_events), len(execution_runs)),
        "protocol_violation_events": len(protocol_violation_events),
        "spawn_failure_events": len(spawn_failure_events),
        "reclaim_events": len(reclaim_events),
        "respawn_guard_events": len(respawn_guard_events),
        "claim_extension_events": len(claim_extension_events),
        "heartbeat_events": len(heartbeat_events),
        "stale_claim_or_reclaim_rate": safe_div(len(reclaim_events), len(execution_runs)),
        "spawn_failure_rate_per_run": safe_div(len(spawn_failure_events), len(execution_runs)),
        "protocol_violation_rate_per_run": safe_div(len(protocol_violation_events), len(execution_runs)),
        "median_blocked_duration": median_or_none(blocked_duration_values),
        "median_queue_wait": median_or_none(queue_wait_values),
        "median_time_to_first_action": median_or_none(first_action_values),
        "median_run_duration": median_or_none(run_duration_values),
        "reroute_rate": safe_div(len(rerouted_task_ids), len(routed_task_ids)),
        "reviewer_engagement_rate": safe_div(len(required_review_task_ids & reviewed_task_ids), len(required_review_task_ids)),
        "review_findings_per_review": safe_div(len(review_findings), len(review_events)),
        "high_severity_finding_rate": safe_div(len(high_severity_findings), len(review_findings)),
        "telemetry_completeness_rate": safe_div(telemetry_complete_eligible, len(eligible_tasks)),
        "all_closed_telemetry_completeness_rate": safe_div(telemetry_complete_all, len(all_closed)),
        "top_gaps": gap_counter.most_common(5),
        "required_review_tasks": len(required_review_task_ids),
        "reviewed_tasks": len(required_review_task_ids & reviewed_task_ids),
        "review_events": len(review_events),
        "review_findings": len(review_findings),
        "routed_tasks": len(routed_task_ids),
        "handoff_started_tasks": len(handoff_started_task_ids),
        "handoff_accepted_tasks": len(handoff_accepted_task_ids),
        "handoff_resolved_tasks": len(handoff_resolved_task_ids),
        "handoff_sent_tasks": len(handoff_sent_task_ids),
        "handoff_event_tasks": len(handoff_event_task_ids),
        "handoff_resolution_rate": safe_div(len(handoff_started_task_ids & handoff_resolved_task_ids), len(handoff_started_task_ids)),
        "handoff_send_rate": safe_div(len(handoff_resolved_task_ids & handoff_sent_task_ids), len(handoff_resolved_task_ids)),
    }


def experiment_scoreability_note(current: dict, operational: dict) -> str:
    blockers = []
    eligible = current["inventory"]["eligible_real_substantial_tasks"]
    if current["contaminated"]:
        blockers.append("unknown-classification contamination remains in the window")
    if eligible < 5:
        blockers.append(f"only {eligible} eligible real+substantial tasks in the window")
    if operational["telemetry_completeness_rate"] is not None and operational["telemetry_completeness_rate"] < 1.0:
        blockers.append(f"telemetry completeness is {fmt_rate(operational['telemetry_completeness_rate'])}")
    routing_cov = current["decision"]["first_owner_routing_coverage"]["value"]
    if routing_cov is not None and routing_cov < 1.0:
        blockers.append(f"routing coverage is {fmt_rate(routing_cov)}")
    if blockers:
        return "NOT SCOREABLE: " + "; ".join(blockers)
    return "SCOREABLE: evidence, routing coverage, and telemetry completeness look sufficient for decision-grade experimentation."


def build_report(conn: sqlite3.Connection, end_date, days: int) -> str:
    current_start = end_date - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)

    c_start = current_start.isoformat()
    c_end = end_date.isoformat()
    p_start = previous_start.isoformat()
    p_end = previous_end.isoformat()

    current_rows = fetch_window_rows(conn, c_start, c_end)
    baseline_rows = fetch_window_rows(conn, p_start, p_end)

    current = summarize_window(current_rows)
    baseline = summarize_window(baseline_rows)
    operational = operational_summary(conn, c_start, c_end)
    reporting_day = latest_reporting_day(conn, c_end)
    profile_activation_rows = load_profile_activation_rows(conn, reporting_day)

    lines = []
    lines.append("# Hermes Self-Improvement Weekly Report")
    lines.append("")
    lines.append(f"Window: {c_start} to {c_end} UTC")
    lines.append(f"Baseline: {p_start} to {p_end} UTC")
    lines.append("")
    lines.append("## Decision evidence (real + substantial only)")
    lines.append("")
    lines.append("| Metric | Current value | n/N | 95% CI | Confidence | Baseline | Delta |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for metric in ("task_success_rate", "user_correction_rate", "reopened_task_rate", "first_owner_routing_accuracy", "first_owner_routing_coverage"):
        cur = current["decision"][metric]
        base = baseline["decision"][metric]
        lines.append(f"| {metric} | {fmt_rate(cur['value'])} | {cur['numerator']}/{cur['denominator']} | {cur['ci']} | {cur['confidence']} | {fmt_rate(base['value'])} | {fmt_delta(cur['value'], base['value'], pct=True)} |")
    for metric in ("turns_to_completion", "tool_calls_per_success", "tokens_per_success"):
        cur = current["decision"][metric]
        base = baseline["decision"][metric]
        lines.append(f"| {metric} | {fmt_float(cur['value'])} | {cur['n_used']}/{cur['eligible_pool']} | n/a | {cur['confidence']} | {fmt_float(base['value'])} | {fmt_delta(cur['value'], base['value'])} |")

    lines.append("")
    lines.append("## Evidence inventory / exclusions")
    lines.append("")
    inv = current["inventory"]
    lines.append("| Category | Count |")
    lines.append("|---|---:|")
    for key in ("total_closed", "eligible_real_substantial_tasks", "real_lightweight_tasks", "bootstrap_tasks", "synthetic_tasks", "seed_tasks", "unknown_classification_tasks"):
        lines.append(f"| {key} | {inv[key]} |")

    lines.append("")
    lines.append("## Profile activation")
    lines.append("")
    lines.append(f"Reporting day: {reporting_day or 'n/a'}")
    if profile_activation_rows:
        lines.append("")
        lines.append("| Profile | Sessions | Tasks completed | Eligible tasks | Excluded tasks |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in profile_activation_rows:
            lines.append(
                f"| {row['profile']} | {int(row['sessions'] or 0)} | {int(row['tasks_completed'] or 0)} | {int(row['eligible_tasks'] or 0)} | {int(row['excluded_tasks'] or 0)} |"
            )
    else:
        lines.append("No profile activation rows found.")

    lines.append("")
    lines.append("## Execution reliability")
    lines.append("")
    lines.append(f"- Runtime coverage: {fmt_rate(operational['runtime_coverage'])} ({operational['eligible_tasks']} eligible tasks)")
    lines.append(f"- Run attempts per task: {fmt_float(operational['run_attempts_per_task'])}")
    lines.append(f"- Failed run rate: {fmt_rate(operational['failed_run_rate'])}")
    lines.append(f"- Crash rate per run: {fmt_rate(operational['crash_rate_per_run'])}")
    lines.append(f"- Give-up rate per run: {fmt_rate(operational['give_up_rate_per_run'])}")
    lines.append(f"- Protocol-violation events: {operational['protocol_violation_events']} ({fmt_rate(operational['protocol_violation_rate_per_run'])} per run)")
    lines.append(f"- Spawn-failure events: {operational['spawn_failure_events']} ({fmt_rate(operational['spawn_failure_rate_per_run'])} per run)")
    lines.append(f"- Reclaim/stale-claim events: {operational['reclaim_events']} ({fmt_rate(operational['stale_claim_or_reclaim_rate'])} per run)")
    lines.append(f"- Respawn-guard events: {operational['respawn_guard_events']}")
    lines.append(f"- Claim-extension / heartbeat events: {operational['claim_extension_events']} / {operational['heartbeat_events']}")
    lines.append(f"- Median queue wait: {fmt_seconds(operational['median_queue_wait'])}")
    lines.append(f"- Median blocked duration: {fmt_seconds(operational['median_blocked_duration'])}")
    lines.append(f"- Median time to first action: {fmt_seconds(operational['median_time_to_first_action'])}")
    lines.append(f"- Median run duration: {fmt_seconds(operational['median_run_duration'])}")

    lines.append("")
    lines.append("## Routing quality and coverage")
    lines.append("")
    lines.append("Source of truth: `routing_decisions` is canonical; legacy `routing_events` rows are fallback/compatibility evidence only. Do not infer routing quality from owner labels, PR creation, or prose summaries alone.")
    lines.append(f"- First-owner routing accuracy: {fmt_rate(current['decision']['first_owner_routing_accuracy']['value'])} ({current['decision']['first_owner_routing_accuracy']['numerator']}/{current['decision']['first_owner_routing_accuracy']['denominator']})")
    lines.append(f"- First-owner routing coverage: {fmt_rate(current['decision']['first_owner_routing_coverage']['value'])} ({current['decision']['first_owner_routing_coverage']['numerator']}/{current['decision']['first_owner_routing_coverage']['denominator']})")
    lines.append(f"- Reroute rate: {fmt_rate(operational['reroute_rate'])}")

    lines.append("")
    lines.append("## Participation / ownership quality")
    lines.append("")
    lines.append(f"- Review-required tasks: {operational['required_review_tasks']}")
    lines.append(f"- Review-engaged tasks: {operational['reviewed_tasks']}")
    lines.append(f"- Reviewer engagement rate: {fmt_rate(operational['reviewer_engagement_rate'])}")

    lines.append("")
    lines.append("## Review effectiveness")
    lines.append("")
    lines.append(f"- Review events: {operational['review_events']}")
    lines.append(f"- Review findings: {operational['review_findings']}")
    lines.append(f"- Review findings per review: {fmt_float(operational['review_findings_per_review'])}")
    lines.append(f"- High-severity finding rate: {fmt_rate(operational['high_severity_finding_rate'])}")

    lines.append("")
    lines.append("## Handoff effectiveness")
    lines.append("")
    lines.append("Explicit handoff events are counted separately from owner/routing activation events.")
    lines.append(f"- Tasks with any handoff event: {operational['handoff_event_tasks']}")
    lines.append(f"- Handoff started / accepted / resolved / sent: {operational['handoff_started_tasks']} / {operational['handoff_accepted_tasks']} / {operational['handoff_resolved_tasks']} / {operational['handoff_sent_tasks']}")
    lines.append(f"- Handoff resolution rate: {fmt_rate(operational['handoff_resolution_rate'])}")
    lines.append(f"- Handoff send rate after resolution: {fmt_rate(operational['handoff_send_rate'])}")

    lines.append("")
    lines.append("## Telemetry completeness")
    lines.append("")
    lines.append(f"- Eligible-task telemetry completeness: {fmt_rate(operational['telemetry_completeness_rate'])}")
    lines.append(f"- All-closed-task telemetry completeness: {fmt_rate(operational['all_closed_telemetry_completeness_rate'])}")
    if operational['top_gaps']:
        lines.append("- Top telemetry gaps:")
        for gap, count in operational['top_gaps']:
            lines.append(f"  - {gap}: {count}")
    else:
        lines.append("- Top telemetry gaps: none")

    lines.append("")
    lines.append("## Experiment scoreability")
    lines.append("")
    lines.append(experiment_scoreability_note(current, operational))

    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(build_recommendation(current, baseline))
    if current["contaminated"]:
        lines.append("")
        lines.append("Contamination note: unknown classification rows are excluded from headline denominators and prevent high-confidence recommendation use.")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    end_date = parse_date(args.end_date)
    db_path = telemetry_root / "events.db"
    reports_dir = telemetry_root / "reports"
    ensure_initialized(telemetry_root)
    reports_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        report = build_report(conn, end_date, args.days)
    finally:
        conn.close()

    path = reports_dir / f"weekly-report-{end_date.isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
