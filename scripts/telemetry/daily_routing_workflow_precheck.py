#!/usr/bin/env python3
"""Delta/anomaly precheck for the daily Hermes routing/workflow audit cron.

The paired cron job is agent-driven, but this script gives it a deterministic
small surface and a durable state signature so unchanged healthy/baseline facts
can be suppressed as `[SILENT]` instead of becoming another daily narrative.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

HOME = Path("/Users/ctao")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes")))
STATE_PATH = HERMES_HOME / "cron" / "state" / "daily-routing-workflow-audit.json"
EVENTS_DB = HERMES_HOME / "telemetry" / "events.db"
REQUIRED_PROFILES = {"default", "engineer", "researcher", "reviewer", "ops", "designer"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit routing/workflow audit deltas and anomaly state.")
    parser.add_argument("--json", action="store_true", help="Emit JSON; text mode is still JSON for cron compatibility")
    parser.add_argument("--hermes-home", default=str(HERMES_HOME))
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--no-write-state", action="store_true", help="Compute without updating durable state")
    return parser.parse_args()


def run_json(name: str, cmd: list[str], timeout: int = 40) -> dict[str, Any]:
    env = os.environ.copy()
    env["HOME"] = str(HOME)
    env["HERMES_HOME"] = str(HERMES_HOME)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(HERMES_HOME / "hermes-agent") if (HERMES_HOME / "hermes-agent").exists() else str(HOME),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        payload: Any = None
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = {"unparsed_stdout": proc.stdout[:4000]}
        return {"name": name, "exit": proc.returncode, "payload": payload, "stderr": proc.stderr[:2000]}
    except subprocess.TimeoutExpired as exc:
        return {"name": name, "exit": "timeout", "payload": None, "stderr": str(exc)}
    except Exception as exc:  # pragma: no cover - operator guard
        return {"name": name, "exit": "error", "payload": None, "stderr": repr(exc)}


def rows(conn: sqlite3.Connection, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(query, args).fetchall()]


def latest_table_rows(conn: sqlite3.Connection, table: str, date_col: str = "date") -> tuple[str | None, list[dict[str, Any]]]:
    try:
        latest = conn.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None, []
    if not latest:
        return None, []
    return str(latest), rows(conn, f"SELECT * FROM {table} WHERE {date_col} = ? ORDER BY 1, 2", (latest,))


def recent_event_counts(conn: sqlite3.Connection, table: str, event_col: str = "event_type", hours: int = 30) -> dict[str, int]:
    # Tables have evolved; inspect columns and choose the best timestamp column.
    try:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return {}
    if event_col not in cols:
        return {}
    time_col = "created_at" if "created_at" in cols else ("timestamp" if "timestamp" in cols else None)
    where = ""
    args: tuple[Any, ...] = ()
    if time_col:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
        where = f"WHERE {time_col} >= ?"
        args = (cutoff,)
    try:
        return {str(r[0]): int(r[1]) for r in conn.execute(f"SELECT {event_col}, COUNT(*) FROM {table} {where} GROUP BY {event_col}", args)}
    except sqlite3.Error:
        return {}


def run_failure_attribution(conn: sqlite3.Connection, day: str | None) -> dict[str, Any]:
    if not day:
        return {}
    try:
        task_rows = rows(
            conn,
            """
            SELECT task_id, title, owner_profile
            FROM tasks
            WHERE closed_at IS NOT NULL
              AND substr(closed_at, 1, 10) = ?
              AND lower(coalesce(provenance, '')) = 'real'
              AND lower(coalesce(substantiality, '')) = 'substantial'
            ORDER BY closed_at, task_id
            """,
            (day,),
        )
    except sqlite3.Error:
        return {}
    task_ids = {row["task_id"] for row in task_rows}
    task_meta = {row["task_id"]: row for row in task_rows}
    if not task_ids:
        return {"date": day, "failed_run_groups": [], "affected_tasks": []}

    placeholders = ",".join("?" for _ in task_ids)
    run_rows = rows(
        conn,
        f"""
        SELECT task_id, run_id, profile, status, outcome, started_at, ended_at, error, summary
        FROM execution_runs
        WHERE task_id IN ({placeholders})
        ORDER BY started_at, run_id
        """,
        tuple(task_ids),
    )
    failed_runs = [
        row for row in run_rows
        if row.get("status") in {"crashed", "blocked", "reclaimed"}
        or row.get("outcome") in {"crashed", "timed_out", "spawn_failed", "blocked", "failed"}
    ]

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in failed_runs:
        key = (str(row.get("profile") or "unknown"), str(row.get("status") or "unknown"), str(row.get("outcome") or "unknown"))
        item = grouped.setdefault(
            key,
            {
                "profile": key[0],
                "status": key[1],
                "outcome": key[2],
                "count": 0,
                "run_ids": [],
                "sample_errors": [],
            },
        )
        item["count"] += 1
        if row.get("run_id"):
            item["run_ids"].append(row.get("run_id"))
        if row.get("error") and len(item["sample_errors"]) < 2:
            item["sample_errors"].append(row.get("error"))

    affected: dict[str, dict[str, Any]] = {}
    for row in failed_runs:
        task_id = row["task_id"]
        meta = task_meta.get(task_id, {})
        item = affected.setdefault(
            task_id,
            {
                "task_id": task_id,
                "title": meta.get("title"),
                "owner_profile": meta.get("owner_profile"),
                "failed_runs": [],
            },
        )
        item["failed_runs"].append({
            "run_id": row.get("run_id"),
            "profile": row.get("profile"),
            "status": row.get("status"),
            "outcome": row.get("outcome"),
            "error": row.get("error"),
        })

    return {
        "date": day,
        "failed_run_count": len(failed_runs),
        "total_run_count": len(run_rows),
        "failed_run_groups": sorted(grouped.values(), key=lambda item: (-item["count"], item["profile"], item["status"]))[:6],
        "affected_tasks": sorted(affected.values(), key=lambda item: item["task_id"])[:6],
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def stable_signature(payload: dict[str, Any]) -> str:
    signature_payload = {
        "reporting_day": payload.get("reporting_day"),
        "severity": payload.get("severity"),
        "actionable_findings": payload.get("actionable_findings"),
        "metric_watch": payload.get("metric_watch"),
    }
    text = json.dumps(signature_payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def analyze(root: Path, state_path: Path, write_state: bool) -> dict[str, Any]:
    now = dt.datetime.now().astimezone().isoformat()
    findings: list[dict[str, Any]] = []
    metric_watch: dict[str, Any] = {}
    snapshot: dict[str, Any] = {
        "generated_at": now,
        "events_db": str(EVENTS_DB),
        "state_path": str(state_path),
    }

    if not EVENTS_DB.exists():
        findings.append({"severity": "critical", "type": "telemetry_missing", "summary": "Telemetry events DB is missing.", "evidence": str(EVENTS_DB)})
    else:
        conn = sqlite3.connect(str(EVENTS_DB))
        conn.row_factory = sqlite3.Row
        try:
            bench_day, bench_rows = latest_table_rows(conn, "bench_metrics_daily")
            profile_day, profile_rows = latest_table_rows(conn, "profile_metrics_daily")
            workflow_day, workflow_rows = latest_table_rows(conn, "workflow_metrics_daily")
            reporting_day = bench_day or profile_day or workflow_day
            snapshot["reporting_day"] = reporting_day
            snapshot["latest_counts"] = {
                "bench_rows": len(bench_rows),
                "profile_rows": len(profile_rows),
                "workflow_rows": len(workflow_rows),
            }

            profiles = {str(row.get("profile")) for row in profile_rows}
            missing_profiles = sorted(REQUIRED_PROFILES - profiles)
            if missing_profiles:
                findings.append({
                    "severity": "major",
                    "type": "profile_metrics_gap",
                    "summary": "Latest profile_metrics_daily is missing expected active profiles.",
                    "evidence": {"profile_day": profile_day, "missing": missing_profiles, "present": sorted(profiles)},
                })

            bench = bench_rows[0] if bench_rows else {}
            metric_watch["bench"] = {
                "date": bench_day,
                "tasks_completed": bench.get("tasks_completed"),
                "first_owner_routing_accuracy": bench.get("first_owner_routing_accuracy"),
                "first_owner_routing_sample": bench.get("first_owner_routing_accuracy_sample_size"),
                "first_owner_routing_coverage_num": bench.get("first_owner_routing_coverage_num"),
                "first_owner_routing_coverage_den": bench.get("first_owner_routing_coverage_den"),
                "failed_run_rate": bench.get("failed_run_rate"),
                "crash_rate_per_run": bench.get("crash_rate_per_run"),
                "give_up_rate_per_run": bench.get("give_up_rate_per_run"),
                "telemetry_completeness_rate": bench.get("telemetry_completeness_rate"),
                "user_correction_rate": bench.get("user_correction_rate"),
                "reopened_task_rate": bench.get("reopened_task_rate"),
            }
            failure_attribution = run_failure_attribution(conn, bench_day)
            if failure_attribution:
                metric_watch["run_failure_attribution"] = failure_attribution
            coverage_num = int(bench.get("first_owner_routing_coverage_num") or 0)
            coverage_den = int(bench.get("first_owner_routing_coverage_den") or 0)
            if coverage_den > coverage_num:
                findings.append({
                    "severity": "major",
                    "type": "routing_correctness_coverage_gap",
                    "summary": "First-owner routing correctness coverage is incomplete; routing accuracy is not scoreable.",
                    "evidence": {
                        "date": bench_day,
                        "coverage_num": coverage_num,
                        "coverage_den": coverage_den,
                        "sample_size": bench.get("first_owner_routing_accuracy_sample_size"),
                    },
                })
            for key, threshold in (
                ("failed_run_rate", 0.0),
                ("crash_rate_per_run", 0.0),
                ("give_up_rate_per_run", 0.0),
                ("user_correction_rate", 0.0),
                ("reopened_task_rate", 0.0),
            ):
                value = bench.get(key)
                if value is not None and float(value) > threshold:
                    evidence = {"date": bench_day, key: value}
                    if key in {"failed_run_rate", "crash_rate_per_run", "give_up_rate_per_run"} and failure_attribution:
                        evidence["failure_attribution_ref"] = "metric_watch.run_failure_attribution"
                    findings.append({"severity": "major", "type": f"bench_{key}", "summary": f"Bench metric {key} is non-zero.", "evidence": evidence})
            completeness = bench.get("telemetry_completeness_rate")
            if completeness is not None and float(completeness) < 0.98:
                findings.append({"severity": "major", "type": "telemetry_completeness_regression", "summary": "Telemetry completeness is below 98%.", "evidence": {"date": bench_day, "telemetry_completeness_rate": completeness}})

            workflow_watch = []
            for row in workflow_rows:
                notable = {k: row.get(k) for k in ("workflow", "tasks_completed", "failed_run_rate", "crash_rate_per_run", "give_up_rate_per_run", "avg_generic_blocked_events", "avg_review_blocked_unblocked_cycles", "telemetry_completeness_rate")}
                workflow_watch.append(notable)
                for key in ("failed_run_rate", "crash_rate_per_run", "give_up_rate_per_run", "avg_generic_blocked_events"):
                    value = row.get(key)
                    if value is not None and float(value) > 0:
                        findings.append({"severity": "major", "type": f"workflow_{key}", "summary": f"Workflow {row.get('workflow')} has non-zero {key}.", "evidence": {"date": workflow_day, "workflow": row.get("workflow"), key: value}})
            metric_watch["workflow"] = workflow_watch

            metric_watch["recent_task_events"] = recent_event_counts(conn, "task_events")
            metric_watch["recent_routing_events"] = recent_event_counts(conn, "routing_events", event_col="event_type")
        finally:
            conn.close()

    invariants = run_json(
        "workflow_invariants",
        ["python3", str(root / "scripts" / "telemetry" / "audit_workflow_invariants.py"), "--json"],
    )
    snapshot["workflow_invariants"] = {"exit": invariants.get("exit")}
    inv_payload = invariants.get("payload") if isinstance(invariants.get("payload"), dict) else {}
    inv_failures = [c for c in inv_payload.get("checks", []) if not c.get("pass")]
    if invariants.get("exit") not in (0, "0"):
        findings.append({"severity": "major", "type": "workflow_invariant_failures", "summary": "Workflow invariant audit returned failures.", "evidence": {"failure_count": len(inv_failures), "failures": inv_failures[:6], "stderr": invariants.get("stderr")}})

    completeness = run_json(
        "telemetry_completeness",
        ["python3", str(root / "scripts" / "telemetry" / "audit_telemetry_completeness.py"), "--json", "--limit", "20"],
    )
    comp_payload = completeness.get("payload") if isinstance(completeness.get("payload"), dict) else {}
    comp_summary = comp_payload.get("summary") or {}
    comp_tasks = comp_payload.get("tasks") or []
    non_routing_gap_tasks = []
    routing_only_gap_tasks = []
    for task in comp_tasks:
        gaps = [str(gap) for gap in (task.get("telemetry_gaps") or [])]
        non_routing_gaps = [gap for gap in gaps if gap != "routing_correctness"]
        if non_routing_gaps:
            non_routing_gap_tasks.append({**task, "telemetry_gaps": non_routing_gaps})
        elif gaps == ["routing_correctness"]:
            routing_only_gap_tasks.append(task)
    if non_routing_gap_tasks:
        findings.append({
            "severity": "major",
            "type": "incomplete_telemetry_tasks",
            "summary": "Closed eligible tasks still have incomplete closeout or handoff evidence.",
            "evidence": {
                "summary": comp_summary,
                "sampled_non_routing_gap_count": len(non_routing_gap_tasks),
                "tasks": non_routing_gap_tasks[:5],
            },
        })
    if routing_only_gap_tasks:
        metric_watch["routing_correctness_gap_sample"] = routing_only_gap_tasks[:5]

    severity = "silent_ready"
    if any(f.get("severity") == "critical" for f in findings):
        severity = "critical"
    elif findings:
        severity = "actionable"

    payload = {
        "generated_at": now,
        "reporting_day": snapshot.get("reporting_day"),
        "severity": severity,
        "actionable_findings": findings,
        "metric_watch": metric_watch,
        "source_summary": snapshot,
        "instructions_for_agent": (
            "If signature_changed is false and severity is not critical, output exactly [SILENT], "
            "even when known stable hygiene findings remain. If signature_changed is true, report "
            "only the changed actionable findings in one short delta. Critical findings may be "
            "repeated until resolved. Do not restate healthy baselines."
        ),
    }

    sig = stable_signature(payload)
    prior = load_state(state_path)
    payload["signature"] = sig
    payload["previous_signature"] = prior.get("signature")
    payload["signature_changed"] = sig != prior.get("signature")
    if not payload["signature_changed"] and severity != "critical":
        payload["recommended_output"] = "[SILENT]"
    elif severity == "silent_ready":
        payload["recommended_output"] = "short_delta_or_silent"
    else:
        payload["recommended_output"] = "short_actionable_delta_report"

    if write_state:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"updated_at": now, "signature": sig, "reporting_day": payload.get("reporting_day"), "severity": severity}, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    args = parse_args()
    global HERMES_HOME, STATE_PATH, EVENTS_DB
    HERMES_HOME = Path(args.hermes_home).expanduser().resolve()
    STATE_PATH = Path(args.state).expanduser().resolve()
    EVENTS_DB = HERMES_HOME / "telemetry" / "events.db"
    payload = analyze(HERMES_HOME, STATE_PATH, not args.no_write_state)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
