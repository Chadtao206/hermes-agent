#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from common import resolve_telemetry_root


REQUIRED_PROFILES = {"default", "engineer", "researcher", "reviewer", "ops", "designer"}
ALLOWED_EXTRA_PROFILES = {"Jensen"}
REQUIRED_CRON_JOBS = {
    "daily-telemetry-kanban-sync",
    "daily-telemetry-metric-aggregation",
    "daily-routing-workflow-audit",
    "weekly-orchestration-workflow-report",
}
BOOTSTRAP_PATTERN = re.compile(r"\b(bootstrap|synthetic|demo)\b", re.IGNORECASE)
REPORT_FILENAME_PATTERN = re.compile(r"weekly-report-(\d{4}-\d{2}-\d{2})\.md$")
REPORT_WINDOW_PATTERN = re.compile(r"^Window:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\s+UTC\s*$")
EXCLUDED_EXPERIMENT_PATTERN = re.compile(r"\b(bootstrap|synthetic|demo)\b", re.IGNORECASE)
CANONICAL_TASK_TYPES = {"implementation", "ops", "review", "research", "planning"}
TERMINAL_STATUS_BY_KANBAN_STATUS = {
    "done": ("completed", "success"),
    "blocked_failed": ("failed", "fail"),
    "blocked": ("blocked", None),
}


@dataclass
class TaskRecord:
    row: sqlite3.Row
    notes: dict[str, Any]
    provenance_label: str
    provenance_source: str
    is_real: bool
    is_bootstrap_or_synthetic: bool
    is_seed: bool
    substantiality_label: str
    substantiality_source: str
    is_substantial: bool

    @property
    def task_id(self) -> str:
        return str(self.row["task_id"])

    @property
    def closed_day(self) -> str | None:
        raw = self.row["closed_at"]
        return str(raw)[:10] if raw else None

    @property
    def closed_at(self) -> str | None:
        raw = self.row["closed_at"]
        return str(raw) if raw else None


@dataclass
class ReportInfo:
    path: Path
    report_day: str
    window_start: str | None
    window_end: str | None
    profiles: set[str]
    first_owner_routing_accuracy: str | None
    first_owner_routing_accuracy_ratio: str | None


@dataclass
class DoctorContext:
    telemetry_root: Path
    events_db: Path
    experiments_db: Path
    kanban_db: Path
    cron_state_path: Path
    report_dir: Path
    events_conn: sqlite3.Connection | None
    experiments_conn: sqlite3.Connection | None
    kanban_conn: sqlite3.Connection | None
    cron_jobs: dict[str, dict[str, Any]] | None
    cron_state_error: str | None
    tasks: list[TaskRecord]
    task_by_id: dict[str, TaskRecord]
    routing_evidence_task_ids: set[str]
    canonical_routing_correctness: dict[str, int]
    reporting_day: str | None
    report_info: ReportInfo | None
    latest_bench_row: sqlite3.Row | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only readiness gate for Hermes self-improvement telemetry. "
            "Writes JSON verdict to stdout and a compact human summary to stderr."
        )
    )
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--kanban-db", default="/Users/ctao/.hermes/kanban.db", help="Path to kanban SQLite database")
    parser.add_argument("--cron-state", default="/Users/ctao/.hermes/cron/jobs.json", help="Path to cron jobs.json")
    parser.add_argument("--json-only", action="store_true", help="Suppress human summary on stderr")
    return parser.parse_args()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}



def parse_json_any(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None



def parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None



def parse_epoch_utc(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None



def connect_sqlite_readonly(path: Path) -> tuple[sqlite3.Connection | None, str | None]:
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn, None
    except sqlite3.Error as exc:
        return None, f"sqlite open failed for {path}: {exc}"



def load_cron_jobs(path: Path) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"failed to parse {path}: {exc}"
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return None, f"invalid cron state payload in {path}: top-level jobs list missing"
    by_name: dict[str, dict[str, Any]] = {}
    for item in jobs:
        if isinstance(item, dict) and item.get("name"):
            by_name[str(item["name"])] = item
    return by_name, None



def latest_weekly_report(report_dir: Path, reporting_day: str | None) -> ReportInfo | None:
    if not report_dir.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for path in sorted(report_dir.glob("weekly-report-*.md")):
        match = REPORT_FILENAME_PATTERN.search(path.name)
        if match:
            candidates.append((match.group(1), path))
    if not candidates:
        return None

    selected_day: str
    selected_path: Path
    if reporting_day:
        exact = [item for item in candidates if item[0] == reporting_day]
        if exact:
            selected_day, selected_path = exact[-1]
        else:
            selected_day, selected_path = max(candidates, key=lambda item: item[0])
    else:
        selected_day, selected_path = max(candidates, key=lambda item: item[0])

    lines = selected_path.read_text(encoding="utf-8").splitlines()
    window_start = None
    window_end = None
    profiles: set[str] = set()
    routing_value = None
    routing_ratio = None

    for line in lines:
        match = REPORT_WINDOW_PATTERN.match(line.strip())
        if match:
            window_start, window_end = match.group(1), match.group(2)
            break

    profile_section = False
    decision_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            profile_section = stripped == "## Profile activation"
            decision_section = stripped == "## Decision evidence (real + substantial only)"
            continue
        if profile_section:
            row = parse_markdown_row(stripped)
            if row and row[0].lower() not in {"profile", "---"}:
                profiles.add(row[0])
        if decision_section and routing_value is None:
            row = parse_markdown_row(stripped)
            if row and row[0] == "first_owner_routing_accuracy":
                routing_value = row[1] if len(row) > 1 else None
                routing_ratio = row[2] if len(row) > 2 else None

    return ReportInfo(
        path=selected_path,
        report_day=selected_day,
        window_start=window_start,
        window_end=window_end,
        profiles=profiles,
        first_owner_routing_accuracy=routing_value,
        first_owner_routing_accuracy_ratio=routing_ratio,
    )



def parse_markdown_row(line: str) -> list[str]:
    if not line.startswith("|"):
        return []
    parts = [part.strip() for part in line.strip("|").split("|")]
    if not parts or all(not part for part in parts):
        return []
    return parts



def load_task_records(conn: sqlite3.Connection | None) -> tuple[list[TaskRecord], dict[str, TaskRecord]]:
    if conn is None:
        return [], {}

    routing_evidence_task_ids = load_routing_evidence_task_ids(conn)
    rows = conn.execute("SELECT * FROM tasks WHERE closed_at IS NOT NULL ORDER BY closed_at, task_id").fetchall()
    records: list[TaskRecord] = []
    by_id: dict[str, TaskRecord] = {}
    for row in rows:
        notes = parse_json_object(row["notes_json"])
        provenance_label, provenance_source, is_real, is_bootstrap_or_synthetic, is_seed = classify_provenance(row, notes)
        substantiality_label, substantiality_source, is_substantial = classify_substantiality(
            row,
            notes,
            routing_evidence_task_ids,
        )
        record = TaskRecord(
            row=row,
            notes=notes,
            provenance_label=provenance_label,
            provenance_source=provenance_source,
            is_real=is_real,
            is_bootstrap_or_synthetic=is_bootstrap_or_synthetic,
            is_seed=is_seed,
            substantiality_label=substantiality_label,
            substantiality_source=substantiality_source,
            is_substantial=is_substantial,
        )
        records.append(record)
        by_id[record.task_id] = record
    return records, by_id



def classify_provenance(row: sqlite3.Row, notes: dict[str, Any]) -> tuple[str, str, bool, bool, bool]:
    explicit = str(row["provenance"] or "").strip().lower()
    if explicit:
        if explicit == "real":
            return "real", "tasks.provenance", True, False, False
        if explicit == "seed":
            return "seed", "tasks.provenance", False, False, True
        if explicit in {"bootstrap", "synthetic"}:
            return explicit, "tasks.provenance", False, True, False
        return explicit, "tasks.provenance", False, False, False

    if notes.get("is_bootstrap") is True:
        return "bootstrap", "notes_json.is_bootstrap", False, True, False
    if notes.get("is_synthetic") is True:
        return "synthetic", "notes_json.is_synthetic", False, True, False

    combined_text = " ".join(
        part for part in [str(row["title"] or "").strip(), str(row["user_goal_summary"] or "").strip()] if part
    )
    if BOOTSTRAP_PATTERN.search(combined_text):
        matched = BOOTSTRAP_PATTERN.search(combined_text)
        label = str(matched.group(1)).lower() if matched else "bootstrap"
        if label not in {"bootstrap", "synthetic"}:
            label = "bootstrap"
        return label, "title/user_goal_summary regex", False, True, False

    return "real", "fallback:not bootstrap/synthetic", True, False, False



def classify_substantiality(
    row: sqlite3.Row,
    notes: dict[str, Any],
    routing_evidence_task_ids: set[str],
) -> tuple[str, str, bool]:
    explicit = str(row["substantiality"] or "").strip().lower()
    if explicit:
        return explicit, "tasks.substantiality", explicit == "substantial"

    reasons: list[str] = []
    if str(row["surface"] or "").strip().lower() == "kanban":
        reasons.append("surface=kanban")
    tool_calls = notes.get("tool_calls")
    if isinstance(tool_calls, int) and tool_calls >= 5:
        reasons.append("notes_json.tool_calls>=5")
    turn_count = notes.get("turn_count")
    if isinstance(turn_count, int) and turn_count >= 4:
        reasons.append("notes_json.turn_count>=4")
    if str(row["assisting_profiles"] or "").strip():
        reasons.append("assisting_profiles")
    if str(row["task_type"] or "").strip().lower() in CANONICAL_TASK_TYPES:
        reasons.append("task_type")
    if str(row["task_id"]) in routing_evidence_task_ids:
        reasons.append("routing_evidence")

    if reasons:
        return "substantial", "heuristic:" + ",".join(reasons), True
    return "unknown", "missing evidence", False



def load_routing_evidence_task_ids(conn: sqlite3.Connection) -> set[str]:
    ids = {str(row[0]) for row in conn.execute("SELECT DISTINCT task_id FROM routing_decisions")}
    ids.update(str(row[0]) for row in conn.execute("SELECT DISTINCT task_id FROM routing_events"))
    return ids



def load_canonical_routing_correctness(conn: sqlite3.Connection | None) -> dict[str, int]:
    if conn is None:
        return {}
    by_task: dict[str, int] = {}
    decision_rows = conn.execute(
        """
        SELECT task_id, was_initial_owner_correct
        FROM routing_decisions
        WHERE was_initial_owner_correct IS NOT NULL
        ORDER BY task_id, COALESCE(sequence_index, 0), occurred_at, id
        """
    ).fetchall()
    for row in decision_rows:
        task_id = str(row["task_id"])
        if task_id not in by_task:
            by_task[task_id] = int(row["was_initial_owner_correct"])

    event_rows = conn.execute(
        """
        SELECT task_id, was_initial_owner_correct
        FROM routing_events
        WHERE was_initial_owner_correct IS NOT NULL
        ORDER BY task_id, occurred_at, id
        """
    ).fetchall()
    for row in event_rows:
        task_id = str(row["task_id"])
        if task_id not in by_task:
            by_task[task_id] = int(row["was_initial_owner_correct"])
    return by_task



def build_context(args: argparse.Namespace) -> DoctorContext:
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    events_db = telemetry_root / "events.db"
    experiments_db = telemetry_root / "experiments.db"
    report_dir = telemetry_root / "reports"
    kanban_db = Path(args.kanban_db).expanduser().resolve()
    cron_state_path = Path(args.cron_state).expanduser().resolve()

    events_conn, _ = connect_sqlite_readonly(events_db)
    experiments_conn, _ = connect_sqlite_readonly(experiments_db)
    kanban_conn, _ = connect_sqlite_readonly(kanban_db)
    cron_jobs, cron_state_error = load_cron_jobs(cron_state_path)
    tasks, task_by_id = load_task_records(events_conn)
    routing_evidence_task_ids = load_routing_evidence_task_ids(events_conn) if events_conn else set()
    canonical_routing_correctness = load_canonical_routing_correctness(events_conn)
    reporting_day = None
    latest_bench_row = None
    if events_conn is not None:
        row = events_conn.execute("SELECT MAX(date) AS max_date FROM profile_metrics_daily").fetchone()
        reporting_day = row["max_date"] if row and row["max_date"] else None
        latest_bench_row = events_conn.execute("SELECT * FROM bench_metrics_daily ORDER BY date DESC LIMIT 1").fetchone()
    report_info = latest_weekly_report(report_dir, reporting_day)
    return DoctorContext(
        telemetry_root=telemetry_root,
        events_db=events_db,
        experiments_db=experiments_db,
        kanban_db=kanban_db,
        cron_state_path=cron_state_path,
        report_dir=report_dir,
        events_conn=events_conn,
        experiments_conn=experiments_conn,
        kanban_conn=kanban_conn,
        cron_jobs=cron_jobs,
        cron_state_error=cron_state_error,
        tasks=tasks,
        task_by_id=task_by_id,
        routing_evidence_task_ids=routing_evidence_task_ids,
        canonical_routing_correctness=canonical_routing_correctness,
        reporting_day=reporting_day,
        report_info=report_info,
        latest_bench_row=latest_bench_row,
    )



def gate_result(gate_id: str, status: str, summary: str, evidence: dict[str, Any], reasons: Iterable[str], applicable: bool = True) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": status,
        "applicable": applicable,
        "summary": summary,
        "evidence": evidence,
        "reasons": list(reasons),
    }



def gate_real_substantial_task_coverage(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    real_ids = [task.task_id for task in ctx.tasks if task.is_real and task.is_substantial]
    bootstrap_ids = [task.task_id for task in ctx.tasks if task.is_bootstrap_or_synthetic]
    evidence = {
        "real_substantial_closed_task_count": len(real_ids),
        "real_substantial_closed_task_ids": real_ids,
        "bootstrap_or_synthetic_closed_task_count": len(bootstrap_ids),
        "bootstrap_or_synthetic_closed_task_ids": bootstrap_ids,
        "seed_closed_task_ids": [task.task_id for task in ctx.tasks if task.is_seed],
    }
    if len(real_ids) < 5:
        reasons.append(f"Only {len(real_ids)} real substantial closed telemetry tasks found; threshold is 5.")
        if len(real_ids) + len(bootstrap_ids) >= 5 and bootstrap_ids:
            reasons.append("The threshold would only be met by counting bootstrap/synthetic tasks, which is forbidden.")
        return gate_result(
            "real_substantial_task_coverage",
            "fail",
            f"Only {len(real_ids)} real substantial closed telemetry tasks recorded.",
            evidence,
            reasons,
        )
    return gate_result(
        "real_substantial_task_coverage",
        "pass",
        f"Telemetry records {len(real_ids)} real substantial closed tasks.",
        evidence,
        [],
    )



def gate_all_profile_rollups_present(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    if ctx.events_conn is None:
        reasons.append(f"Telemetry DB unavailable: {ctx.events_db}")
        return gate_result(
            "all_profile_rollups_present",
            "fail",
            "Telemetry DB is unavailable, so profile rollups cannot be verified.",
            {
                "reporting_day": None,
                "profiles_in_profile_metrics_daily": [],
                "profiles_in_weekly_report": [],
            },
            reasons,
        )

    profiles_in_daily: list[str] = []
    if ctx.reporting_day:
        rows = ctx.events_conn.execute(
            "SELECT profile FROM profile_metrics_daily WHERE date = ? ORDER BY profile",
            (ctx.reporting_day,),
        ).fetchall()
        profiles_in_daily = [str(row["profile"]) for row in rows]
    else:
        reasons.append("profile_metrics_daily has no reporting day.")

    report_profiles = sorted(ctx.report_info.profiles) if ctx.report_info else []
    evidence = {
        "reporting_day": ctx.reporting_day,
        "profiles_in_profile_metrics_daily": profiles_in_daily,
        "profiles_in_weekly_report": report_profiles,
        "weekly_report_path": str(ctx.report_info.path) if ctx.report_info else None,
    }

    missing_daily = sorted(REQUIRED_PROFILES - set(profiles_in_daily))
    missing_report = sorted(REQUIRED_PROFILES - set(report_profiles))
    extra_daily = sorted(set(profiles_in_daily) - REQUIRED_PROFILES - ALLOWED_EXTRA_PROFILES)
    extra_report = sorted(set(report_profiles) - REQUIRED_PROFILES - ALLOWED_EXTRA_PROFILES)

    if not ctx.reporting_day:
        return gate_result(
            "all_profile_rollups_present",
            "fail",
            "Latest reporting day is missing from profile_metrics_daily.",
            evidence,
            reasons,
        )
    if ctx.report_info is None:
        reasons.append("No weekly report file found.")
    if missing_daily:
        reasons.append(f"profile_metrics_daily is missing required profiles: {', '.join(missing_daily)}.")
    if missing_report:
        reasons.append(f"weekly report is missing required profiles: {', '.join(missing_report)}.")
    if extra_daily:
        reasons.append(f"profile_metrics_daily has unexpected profiles: {', '.join(extra_daily)}.")
    if extra_report:
        reasons.append(f"weekly report has unexpected profiles: {', '.join(extra_report)}.")

    if reasons:
        return gate_result(
            "all_profile_rollups_present",
            "fail",
            "Required all-profile rollups are not present in both daily metrics and the weekly report.",
            evidence,
            reasons,
        )
    return gate_result(
        "all_profile_rollups_present",
        "pass",
        f"All {len(REQUIRED_PROFILES)} required profiles are present in daily metrics and the weekly report.",
        evidence,
        [],
    )



def latest_bench_routing_metric(ctx: DoctorContext) -> tuple[float | None, int | None, str | None]:
    if ctx.latest_bench_row is None:
        return None, None, None
    value = ctx.latest_bench_row["first_owner_routing_accuracy"]
    sample = ctx.latest_bench_row["first_owner_routing_accuracy_sample_size"]
    confidence = ctx.latest_bench_row["first_owner_routing_accuracy_confidence"]
    return (float(value) if value is not None else None, int(sample) if sample is not None else None, str(confidence) if confidence else None)



def gate_routing_accuracy_evaluable(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    real_routed_ids = sorted(
        task.task_id
        for task in ctx.tasks
        if task.is_real and task.is_substantial and task.task_id in ctx.routing_evidence_task_ids
    )
    evaluable_real_routed_ids = sorted(task_id for task_id in real_routed_ids if task_id in ctx.canonical_routing_correctness)

    bench_value, bench_sample_size, bench_confidence = latest_bench_routing_metric(ctx)
    report_metric_non_null = False
    report_metric_value = None
    report_metric_ratio = None
    if ctx.report_info and ctx.report_info.first_owner_routing_accuracy:
        report_metric_value = ctx.report_info.first_owner_routing_accuracy
        report_metric_ratio = ctx.report_info.first_owner_routing_accuracy_ratio
        report_metric_non_null = str(report_metric_value).strip().lower() != "n/a"

    metric_is_non_null = bench_value is not None or report_metric_non_null
    applicable = bool(real_routed_ids) or metric_is_non_null

    supporting_metric_ids: list[str] = []
    bootstrap_support_ids: list[str] = []
    support_window_start = None
    support_window_end = None
    if bench_value is not None and ctx.reporting_day:
        support_window_start = ctx.reporting_day
        support_window_end = ctx.reporting_day
    elif report_metric_non_null and ctx.report_info and ctx.report_info.window_start and ctx.report_info.window_end:
        support_window_start = ctx.report_info.window_start
        support_window_end = ctx.report_info.window_end

    if support_window_start and support_window_end:
        for task in ctx.tasks:
            if not task.closed_day:
                continue
            if not (support_window_start <= task.closed_day <= support_window_end):
                continue
            if task.task_id not in ctx.canonical_routing_correctness:
                continue
            if task.is_real and task.is_substantial:
                supporting_metric_ids.append(task.task_id)
            elif task.is_bootstrap_or_synthetic:
                bootstrap_support_ids.append(task.task_id)

    evidence = {
        "real_routed_task_count": len(real_routed_ids),
        "real_routed_task_ids": real_routed_ids,
        "evaluable_real_routed_task_count": len(evaluable_real_routed_ids),
        "evaluable_real_routed_task_ids": evaluable_real_routed_ids,
        "latest_bench_first_owner_routing_accuracy": bench_value,
        "latest_bench_first_owner_routing_accuracy_sample_size": bench_sample_size,
        "latest_bench_first_owner_routing_accuracy_confidence": bench_confidence,
        "latest_report_first_owner_routing_accuracy": report_metric_value,
        "latest_report_first_owner_routing_accuracy_ratio": report_metric_ratio,
        "routing_tasks_supporting_metric": supporting_metric_ids,
        "bootstrap_or_synthetic_routing_task_ids": bootstrap_support_ids,
        "reporting_day": ctx.reporting_day,
    }

    if not applicable:
        return gate_result(
            "routing_accuracy_evaluable",
            "pass",
            "Routing accuracy is not yet applicable and no bench/report routing metric is being claimed.",
            evidence,
            [],
            applicable=False,
        )

    if not real_routed_ids:
        reasons.append("No real substantial tasks with routing evidence exist, but a routing metric is being evaluated.")
    if real_routed_ids and not evaluable_real_routed_ids:
        reasons.append("Real routed work exists, but every routed real task still has unknown initial-owner correctness.")
    if metric_is_non_null and not supporting_metric_ids:
        reasons.append("A non-null routing metric is present, but no real routed task on the latest reporting day supports it.")
    if metric_is_non_null and supporting_metric_ids == [] and bootstrap_support_ids:
        reasons.append("Routing accuracy appears to be supported only by bootstrap/synthetic work.")

    if reasons:
        return gate_result(
            "routing_accuracy_evaluable",
            "fail",
            "Routing accuracy is not adequately supported by evaluable real routed work.",
            evidence,
            reasons,
        )
    return gate_result(
        "routing_accuracy_evaluable",
        "pass",
        "Routing accuracy is supported by evaluable real routed work.",
        evidence,
        [],
    )



def gate_weekly_report_grounded_in_real_work(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    if ctx.report_info is None:
        return gate_result(
            "weekly_report_grounded_in_real_work",
            "fail",
            "No weekly report file is present.",
            {
                "weekly_report_path": None,
                "report_window_start": None,
                "report_window_end": None,
                "window_real_substantial_closed_task_count": 0,
                "window_bootstrap_or_synthetic_closed_task_count": 0,
                "window_real_substantial_closed_task_ids": [],
            },
            ["No weekly report file found under telemetry/reports/."],
        )

    if ctx.reporting_day and ctx.report_info.report_day != ctx.reporting_day:
        reasons.append(
            f"Latest report file is for {ctx.report_info.report_day}, but latest reporting_day is {ctx.reporting_day}."
        )

    window_real_ids: list[str] = []
    window_bootstrap_ids: list[str] = []
    if ctx.report_info.window_start and ctx.report_info.window_end:
        for task in ctx.tasks:
            if not task.closed_day:
                continue
            if ctx.report_info.window_start <= task.closed_day <= ctx.report_info.window_end:
                if task.is_real and task.is_substantial:
                    window_real_ids.append(task.task_id)
                elif task.is_bootstrap_or_synthetic:
                    window_bootstrap_ids.append(task.task_id)
    else:
        reasons.append("Weekly report window header is missing or unparsable.")

    evidence = {
        "weekly_report_path": str(ctx.report_info.path),
        "report_window_start": ctx.report_info.window_start,
        "report_window_end": ctx.report_info.window_end,
        "window_real_substantial_closed_task_count": len(window_real_ids),
        "window_bootstrap_or_synthetic_closed_task_count": len(window_bootstrap_ids),
        "window_real_substantial_closed_task_ids": window_real_ids,
        "window_bootstrap_or_synthetic_closed_task_ids": window_bootstrap_ids,
    }

    if not ctx.report_info.path.exists():
        reasons.append(f"Weekly report file is missing: {ctx.report_info.path}")
    if len(window_real_ids) <= len(window_bootstrap_ids):
        reasons.append(
            "Weekly report window is not primarily grounded in real work because bootstrap/synthetic closed tasks are at least half of the closed-task mix."
        )
    if not window_real_ids:
        reasons.append("Weekly report window contains zero real substantial closed tasks.")

    if reasons:
        return gate_result(
            "weekly_report_grounded_in_real_work",
            "fail",
            "Weekly report is not clearly grounded in real substantial work.",
            evidence,
            reasons,
        )
    return gate_result(
        "weekly_report_grounded_in_real_work",
        "pass",
        "Weekly report window is primarily grounded in real substantial work.",
        evidence,
        [],
    )



def experiment_is_bootstrap(row: sqlite3.Row) -> bool:
    haystack = " ".join(
        [
            str(row["experiment_id"] or ""),
            str(row["name"] or ""),
            str(row["change_summary"] or ""),
            str(row["hypothesis"] or ""),
        ]
    )
    return bool(EXCLUDED_EXPERIMENT_PATTERN.search(haystack))



def gate_experiment_baseline_observation_readiness(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    if ctx.experiments_conn is None:
        return gate_result(
            "experiment_baseline_observation_readiness",
            "fail",
            "Experiments DB is unavailable, so experiment readiness cannot be verified.",
            {
                "non_bootstrap_experiment_ids": [],
                "scored_non_bootstrap_experiment_ids": [],
                "experiments_missing_baseline": [],
                "experiments_with_non_actionable_recommendation": [],
            },
            [f"Experiments DB unavailable: {ctx.experiments_db}"],
        )

    experiments = ctx.experiments_conn.execute("SELECT * FROM experiments ORDER BY created_at, experiment_id").fetchall()
    non_bootstrap = [row for row in experiments if not experiment_is_bootstrap(row)]
    non_bootstrap_ids = [str(row["experiment_id"]) for row in non_bootstrap]
    scored_ids = [str(row["experiment_id"]) for row in non_bootstrap if row["latest_scored_at"]]
    missing_baseline: list[str] = []
    non_actionable: list[str] = []
    actionable_ids: list[str] = []

    for row in non_bootstrap:
        experiment_id = str(row["experiment_id"])
        target_metrics = parse_json_object(row["target_metrics_json"]).keys()
        observed_at = row["latest_scored_at"] or row["latest_scored_at"]
        latest_rows = []
        if observed_at:
            latest_rows = ctx.experiments_conn.execute(
                "SELECT * FROM experiment_observations WHERE experiment_id = ? AND observed_at = ?",
                (experiment_id, observed_at),
            ).fetchall()
        by_metric = {str(item["metric_name"]): item for item in latest_rows}

        metrics_missing_baseline = []
        metrics_missing_current = []
        for metric in target_metrics:
            metric_row = by_metric.get(str(metric))
            if metric_row is None or metric_row["baseline_value"] is None:
                metrics_missing_baseline.append(str(metric))
            if metric_row is None or metric_row["metric_value"] is None:
                metrics_missing_current.append(str(metric))

        if metrics_missing_baseline or metrics_missing_current:
            missing_baseline.append(experiment_id)

        recommendation = str(row["latest_recommendation"] or "").strip().lower()
        if recommendation in {"", "insufficient_data", "not_scoreable"}:
            non_actionable.append(experiment_id)
        elif not metrics_missing_baseline and not metrics_missing_current:
            actionable_ids.append(experiment_id)

    evidence = {
        "non_bootstrap_experiment_ids": non_bootstrap_ids,
        "scored_non_bootstrap_experiment_ids": scored_ids,
        "experiments_missing_baseline": missing_baseline,
        "experiments_with_non_actionable_recommendation": non_actionable,
        "actionable_experiment_ids": actionable_ids,
    }

    if not non_bootstrap_ids:
        reasons.append("Only bootstrap/synthetic experiments exist.")
    if non_bootstrap_ids and len(missing_baseline) == len(non_bootstrap_ids):
        reasons.append("All non-bootstrap experiments are missing baseline or current target-metric observations.")
    if non_bootstrap_ids and len(non_actionable) == len(non_bootstrap_ids):
        reasons.append("All non-bootstrap experiments still yield non-actionable recommendations.")
    if not actionable_ids:
        reasons.append("No non-bootstrap experiment currently satisfies both observation completeness and an actionable recommendation.")

    if reasons:
        return gate_result(
            "experiment_baseline_observation_readiness",
            "fail",
            "Experiment evidence is not yet ready for a non-bootstrap actionable recommendation.",
            evidence,
            reasons,
        )
    return gate_result(
        "experiment_baseline_observation_readiness",
        "pass",
        "At least one non-bootstrap experiment has full baseline/current observations and an actionable recommendation.",
        evidence,
        [],
    )



def gate_scheduler_proof_state(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    evidence = {
        "required_jobs": sorted(REQUIRED_CRON_JOBS),
        "jobs_missing_last_run_at": [],
        "jobs_with_non_ok_status": [],
        "job_receipts": {},
    }
    if ctx.cron_jobs is None:
        reasons.append(ctx.cron_state_error or f"Cron state unavailable: {ctx.cron_state_path}")
        return gate_result(
            "scheduler_proof_state",
            "fail",
            "Cron scheduler state is unavailable.",
            evidence,
            reasons,
        )

    missing_last_run_at: list[str] = []
    non_ok: list[str] = []
    missing_jobs: list[str] = []
    for name in sorted(REQUIRED_CRON_JOBS):
        job = ctx.cron_jobs.get(name)
        if job is None:
            missing_jobs.append(name)
            continue
        evidence["job_receipts"][name] = {
            "job_id": job.get("id"),
            "last_run_at": job.get("last_run_at"),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
        }
        if not job.get("last_run_at"):
            missing_last_run_at.append(name)
        if job.get("last_status") != "ok":
            non_ok.append(name)

    evidence["jobs_missing_last_run_at"] = missing_last_run_at + missing_jobs
    evidence["jobs_with_non_ok_status"] = non_ok + missing_jobs

    if missing_jobs:
        reasons.append(f"Required cron jobs missing from jobs.json: {', '.join(missing_jobs)}.")
    if missing_last_run_at:
        reasons.append(f"Required jobs missing successful last_run_at: {', '.join(missing_last_run_at)}.")
    if non_ok:
        reasons.append(f"Required jobs with non-ok last_status: {', '.join(non_ok)}.")

    if reasons:
        return gate_result(
            "scheduler_proof_state",
            "fail",
            "At least one required cron job lacks scheduler proof of a successful run.",
            evidence,
            reasons,
        )
    return gate_result(
        "scheduler_proof_state",
        "pass",
        "All required cron jobs have successful scheduler receipts in jobs.json.",
        evidence,
        [],
    )



def latest_successful_sync_at(ctx: DoctorContext) -> datetime | None:
    if ctx.cron_jobs is None:
        return None
    job = ctx.cron_jobs.get("daily-telemetry-kanban-sync")
    if not job or job.get("last_status") != "ok":
        return None
    return parse_dt(job.get("last_run_at"))



def latest_blocked_timestamp(kanban_conn: sqlite3.Connection, task_id: str, fallback_row: sqlite3.Row) -> datetime | None:
    row = kanban_conn.execute(
        "SELECT MAX(created_at) AS ts FROM task_events WHERE task_id = ? AND kind = 'blocked'",
        (task_id,),
    ).fetchone()
    ts = parse_epoch_utc(row["ts"] if row else None)
    if ts:
        return ts
    return parse_epoch_utc(fallback_row["last_heartbeat_at"] or fallback_row["started_at"] or fallback_row["created_at"])



def expected_telemetry_state(kanban_row: sqlite3.Row) -> tuple[str, str | None]:
    if kanban_row["status"] == "done":
        return TERMINAL_STATUS_BY_KANBAN_STATUS["done"]
    if kanban_row["status"] == "blocked" and int(kanban_row["consecutive_failures"] or 0) > 0:
        return TERMINAL_STATUS_BY_KANBAN_STATUS["blocked_failed"]
    return TERMINAL_STATUS_BY_KANBAN_STATUS["blocked"]



def gate_kanban_telemetry_drift_state(ctx: DoctorContext) -> dict[str, Any]:
    reasons: list[str] = []
    evidence = {
        "latest_successful_sync_at": None,
        "unsynced_kanban_done_task_ids": [],
        "unsynced_kanban_blocked_task_ids": [],
        "state_mismatch_task_ids": [],
    }
    if ctx.kanban_conn is None:
        reasons.append(f"Kanban DB unavailable: {ctx.kanban_db}")
        return gate_result(
            "kanban_telemetry_drift_state",
            "fail",
            "Kanban DB is unavailable, so drift cannot be verified.",
            evidence,
            reasons,
        )

    sync_at = latest_successful_sync_at(ctx)
    if sync_at is None:
        reasons.append("daily-telemetry-kanban-sync has never recorded a successful scheduler run.")
        return gate_result(
            "kanban_telemetry_drift_state",
            "fail",
            "Kanban/telemetry drift cannot pass before the sync job succeeds at least once.",
            evidence,
            reasons,
        )

    evidence["latest_successful_sync_at"] = sync_at.isoformat()
    relevant_rows = ctx.kanban_conn.execute(
        "SELECT * FROM tasks WHERE status IN ('done', 'blocked') ORDER BY id"
    ).fetchall()

    unsynced_done: list[str] = []
    unsynced_blocked: list[str] = []
    mismatches: list[str] = []
    for row in relevant_rows:
        source_ts = parse_epoch_utc(row["completed_at"]) if row["status"] == "done" else latest_blocked_timestamp(ctx.kanban_conn, str(row["id"]), row)
        if source_ts is None or source_ts > sync_at:
            continue
        telemetry_id = f"kanban:{row['id']}"
        telemetry = ctx.task_by_id.get(telemetry_id)
        expected_status, expected_outcome = expected_telemetry_state(row)
        if telemetry is None:
            if row["status"] == "done":
                unsynced_done.append(str(row["id"]))
            else:
                unsynced_blocked.append(str(row["id"]))
            continue
        actual_status = str(telemetry.row["status"] or "")
        actual_outcome = telemetry.row["outcome"]
        if row["status"] == "done" and not telemetry.closed_at:
            unsynced_done.append(str(row["id"]))
            continue
        if actual_status != expected_status or actual_outcome != expected_outcome:
            mismatches.append(str(row["id"]))

    evidence["unsynced_kanban_done_task_ids"] = unsynced_done
    evidence["unsynced_kanban_blocked_task_ids"] = unsynced_blocked
    evidence["state_mismatch_task_ids"] = mismatches

    if unsynced_done:
        reasons.append(f"Done kanban tasks still unsynced in telemetry: {', '.join(unsynced_done)}.")
    if unsynced_blocked:
        reasons.append(f"Blocked kanban tasks still unsynced in telemetry: {', '.join(unsynced_blocked)}.")
    if mismatches:
        reasons.append(f"Kanban/telemetry state mismatches remain for: {', '.join(mismatches)}.")

    if reasons:
        return gate_result(
            "kanban_telemetry_drift_state",
            "fail",
            "Kanban source state and telemetry still drift for tasks that should already have been synchronized.",
            evidence,
            reasons,
        )
    return gate_result(
        "kanban_telemetry_drift_state",
        "pass",
        "All done/blocked kanban tasks older than the last successful sync are aligned in telemetry.",
        evidence,
        [],
    )



def evaluate(ctx: DoctorContext) -> dict[str, Any]:
    gates = [
        gate_real_substantial_task_coverage(ctx),
        gate_all_profile_rollups_present(ctx),
        gate_routing_accuracy_evaluable(ctx),
        gate_weekly_report_grounded_in_real_work(ctx),
        gate_experiment_baseline_observation_readiness(ctx),
        gate_scheduler_proof_state(ctx),
        gate_kanban_telemetry_drift_state(ctx),
    ]
    overall_verdict = "COMPLETE" if all(gate["status"] == "pass" for gate in gates) else "NOT_COMPLETE"
    return {
        "overall_verdict": overall_verdict,
        "evaluated_at": utc_now_iso(),
        "reporting_day": ctx.reporting_day,
        "gates": gates,
    }



def print_human_summary(result: dict[str, Any]) -> None:
    verdict = result["overall_verdict"]
    failed = [gate for gate in result["gates"] if gate["status"] != "pass"]
    print(verdict, file=sys.stderr)
    if not failed:
        print("All readiness gates passed.", file=sys.stderr)
        return
    print(f"{len(failed)} failing gate(s):", file=sys.stderr)
    for gate in failed:
        print(f"- {gate['gate_id']}: {gate['summary']}", file=sys.stderr)
        for reason in gate.get("reasons", [])[:3]:
            print(f"    • {reason}", file=sys.stderr)



def main() -> int:
    args = parse_args()
    ctx = build_context(args)
    try:
        result = evaluate(ctx)
    finally:
        for conn in (ctx.events_conn, ctx.experiments_conn, ctx.kanban_conn):
            if conn is not None:
                conn.close()

    if not args.json_only:
        print_human_summary(result)
    json.dump(result, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0 if result["overall_verdict"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
