#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from typing import Any

from common import ensure_initialized, experiments_connection, resolve_telemetry_root, utc_now
from telemetry_evidence import aggregate_mean, aggregate_proportion, confidence_label

DEFAULT_GUARDRAILS = {
    "task_success_rate": "increase",
    "user_correction_rate": "decrease",
    "reopened_task_rate": "decrease",
}

METRIC_CONFIG: dict[str, dict[str, str]] = {
    "task_success_rate": {"kind": "proportion", "num": "task_success_num", "den": "task_success_den"},
    "user_correction_rate": {"kind": "proportion", "num": "user_correction_num", "den": "user_correction_den"},
    "reopened_task_rate": {"kind": "proportion", "num": "reopened_task_num", "den": "reopened_task_den"},
    "first_owner_routing_accuracy": {"kind": "proportion", "num": "first_owner_routing_num", "den": "first_owner_routing_den"},
    "first_owner_routing_coverage": {"kind": "proportion", "num": "first_owner_routing_coverage_num", "den": "first_owner_routing_coverage_den"},
    "turns_to_completion": {"kind": "mean", "total": "turns_total", "count": "turns_n"},
    "tool_calls_per_success": {"kind": "mean", "total": "tool_calls_total", "count": "tool_calls_n"},
    "tokens_per_success": {"kind": "mean", "total": "tokens_total", "count": "tokens_n"},
}


NOT_SCOREABLE_CODES = {
    "no_baseline",
    "baseline_too_small",
    "observation_window_incomplete",
    "too_few_real_tasks",
    "too_few_routed_tasks",
    "synthetic_contamination",
    "missing_provenance_labels",
    "missing_metric_coverage",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score Hermes self-improvement experiments against daily metrics.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--experiment-id", help="Score a single experiment instead of all active experiments")
    parser.add_argument("--as-of", help="UTC date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--write-observations", action="store_true", help="Persist observations into experiments.db")
    return parser.parse_args()


def parse_date(raw: str | None):
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return datetime.utcnow().date()


def evaluate_direction(direction: str, baseline, current):
    if baseline is None or current is None:
        return "insufficient_data"
    if direction == "increase":
        return "improved" if current > baseline else ("flat" if current == baseline else "regressed")
    if direction == "decrease":
        return "improved" if current < baseline else ("flat" if current == baseline else "regressed")
    raise ValueError(f"Unknown direction: {direction}")


def fetch_experiments(conn: sqlite3.Connection, experiment_id: str | None):
    if experiment_id:
        return conn.execute("SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchall()
    return conn.execute(
        """
        SELECT * FROM experiments
        WHERE status IN ('proposed', 'collecting_baseline', 'observing', 'ready_to_score')
        ORDER BY created_at
        """
    ).fetchall()


def fetch_bench_rows(events_conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    events_conn.row_factory = sqlite3.Row
    try:
        rows = events_conn.execute(
            "SELECT * FROM bench_metrics_daily WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        ).fetchall()
    finally:
        events_conn.row_factory = None
    return [dict(row) for row in rows]


def aggregate_metric(rows: list[dict[str, Any]], metric_name: str) -> dict[str, Any]:
    cfg = METRIC_CONFIG.get(metric_name)
    if not cfg:
        return {
            "metric_name": metric_name,
            "kind": "unknown",
            "value": None,
            "denominator": 0,
            "coverage_n": 0,
        }

    if cfg["kind"] == "proportion":
        num, den, value = aggregate_proportion(rows, cfg["num"], cfg["den"])
        return {
            "metric_name": metric_name,
            "kind": "proportion",
            "value": value,
            "numerator": num,
            "denominator": den,
            "coverage_n": den,
            "confidence": confidence_label(den),
        }

    total, count, value = aggregate_mean(rows, cfg["total"], cfg["count"])
    return {
        "metric_name": metric_name,
        "kind": "mean",
        "value": value,
        "total": total,
        "denominator": count,
        "coverage_n": count,
        "confidence": confidence_label(count),
    }


def contamination_flags(rows: list[dict[str, Any]]) -> dict[str, int]:
    unknown = sum(int(row.get("unknown_classification_tasks") or 0) for row in rows)
    bootstrap = sum(int(row.get("bootstrap_tasks") or 0) for row in rows)
    synthetic = sum(int(row.get("synthetic_tasks") or 0) for row in rows)
    seed = sum(int(row.get("seed_tasks") or 0) for row in rows)
    return {
        "unknown": unknown,
        "bootstrap": bootstrap,
        "synthetic": synthetic,
        "seed": seed,
    }


def parse_date_field(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def build_not_scoreable_reasons(
    exp_row: sqlite3.Row,
    as_of_date,
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    metric_results: list[dict[str, Any]],
) -> list[str]:
    reasons: set[str] = set()

    baseline_start = parse_date_field(exp_row["baseline_start_date"])
    baseline_end = parse_date_field(exp_row["baseline_end_date"])
    observation_start = parse_date_field(exp_row["observation_start_date"])
    observation_end = parse_date_field(exp_row["observation_end_date"])

    min_real_tasks = int(exp_row["min_real_tasks"] or 5)
    min_routed_tasks = int(exp_row["min_routed_tasks"] or 5)

    has_baseline_window = bool(baseline_start and baseline_end)
    has_observation_window = bool(observation_start and observation_end)

    if not has_baseline_window:
        reasons.add("no_baseline")

    if not has_observation_window:
        reasons.add("observation_window_incomplete")
    elif as_of_date < observation_end:
        reasons.add("observation_window_incomplete")

    if not (has_baseline_window and has_observation_window):
        return [code for code in sorted(reasons) if code in NOT_SCOREABLE_CODES]

    baseline_flags = contamination_flags(baseline_rows)
    current_flags = contamination_flags(current_rows)
    if baseline_flags["unknown"] > 0 or current_flags["unknown"] > 0:
        reasons.add("missing_provenance_labels")
        reasons.add("synthetic_contamination")

    baseline_success = aggregate_metric(baseline_rows, "task_success_rate")
    current_success = aggregate_metric(current_rows, "task_success_rate")
    if (current_success.get("denominator") or 0) < min_real_tasks:
        reasons.add("too_few_real_tasks")

    routing_required = any(
        item["metric_name"] in {"first_owner_routing_accuracy", "first_owner_routing_coverage"}
        for item in metric_results
    )
    if routing_required:
        current_routing = aggregate_metric(current_rows, "first_owner_routing_accuracy")
        baseline_routing = aggregate_metric(baseline_rows, "first_owner_routing_accuracy")
        if (baseline_routing.get("denominator") or 0) <= 0:
            reasons.add("no_baseline")
        if (baseline_routing.get("denominator") or 0) < min_routed_tasks:
            reasons.add("baseline_too_small")
        if (current_routing.get("denominator") or 0) < min_routed_tasks:
            reasons.add("too_few_routed_tasks")

    for item in metric_results:
        baseline_den = int(item.get("baseline_denominator") or 0)
        current_den = int(item.get("eligible_denominator") or 0)
        if baseline_den <= 0:
            reasons.add("no_baseline")
        if baseline_den < min_real_tasks:
            reasons.add("baseline_too_small")

        metric_name = item.get("metric_name")
        metric_cfg = METRIC_CONFIG.get(metric_name or "", {})
        metric_kind = metric_cfg.get("kind")
        if item.get("interpretation") == "insufficient_data":
            reasons.add("missing_metric_coverage")
            continue
        if metric_kind == "mean" and current_den < min_real_tasks:
            reasons.add("missing_metric_coverage")
        if metric_kind == "proportion" and current_den < min_real_tasks:
            reasons.add("too_few_real_tasks")

    # Global baseline gate for headline eligible-task evidence.
    if (baseline_success.get("denominator") or 0) <= 0:
        reasons.add("no_baseline")
    if (baseline_success.get("denominator") or 0) < min_real_tasks:
        reasons.add("baseline_too_small")

    return [code for code in sorted(reasons) if code in NOT_SCOREABLE_CODES]


def score_experiment(events_conn: sqlite3.Connection, exp_row: sqlite3.Row, as_of_date):
    targets = json.loads(exp_row["target_metrics_json"])

    baseline_start = parse_date_field(exp_row["baseline_start_date"])
    baseline_end = parse_date_field(exp_row["baseline_end_date"])
    observation_start = parse_date_field(exp_row["observation_start_date"])
    observation_end = parse_date_field(exp_row["observation_end_date"])

    baseline_range = (
        baseline_start.isoformat() if baseline_start else None,
        baseline_end.isoformat() if baseline_end else None,
    )
    current_range = (
        observation_start.isoformat() if observation_start else None,
        observation_end.isoformat() if observation_end else None,
    )

    baseline_rows: list[dict[str, Any]] = []
    current_rows: list[dict[str, Any]] = []
    if baseline_range[0] and baseline_range[1]:
        baseline_rows = fetch_bench_rows(events_conn, baseline_range[0], baseline_range[1])
    if current_range[0] and current_range[1]:
        current_rows = fetch_bench_rows(events_conn, current_range[0], current_range[1])

    metric_results = []
    for metric, direction in targets.items():
        baseline = aggregate_metric(baseline_rows, metric)
        current = aggregate_metric(current_rows, metric)

        verdict = evaluate_direction(direction, baseline.get("value"), current.get("value"))
        metric_results.append(
            {
                "metric_name": metric,
                "metric_scope": "target",
                "direction": direction,
                "baseline_value": baseline.get("value"),
                "metric_value": current.get("value"),
                "delta_value": None if baseline.get("value") is None or current.get("value") is None else current["value"] - baseline["value"],
                "interpretation": verdict,
                "eligible_denominator": current.get("denominator", 0),
                "baseline_denominator": baseline.get("denominator", 0),
                "confidence_label": current.get("confidence"),
            }
        )

    guardrail_results = []
    for metric, direction in DEFAULT_GUARDRAILS.items():
        baseline = aggregate_metric(baseline_rows, metric)
        current = aggregate_metric(current_rows, metric)
        verdict = evaluate_direction(direction, baseline.get("value"), current.get("value"))
        guardrail_results.append(
            {
                "metric_name": metric,
                "metric_scope": "guardrail",
                "direction": direction,
                "baseline_value": baseline.get("value"),
                "metric_value": current.get("value"),
                "delta_value": None if baseline.get("value") is None or current.get("value") is None else current["value"] - baseline["value"],
                "interpretation": verdict,
                "eligible_denominator": current.get("denominator", 0),
                "baseline_denominator": baseline.get("denominator", 0),
                "confidence_label": current.get("confidence"),
            }
        )

    not_scoreable_reasons = build_not_scoreable_reasons(
        exp_row,
        as_of_date,
        baseline_rows,
        current_rows,
        metric_results,
    )
    scoreable_status = "scoreable" if not not_scoreable_reasons else "not_scoreable"

    if not_scoreable_reasons:
        recommendation = "not_scoreable"
    else:
        positive = sum(1 for item in metric_results if item["interpretation"] in {"improved", "flat"})
        regressions = [item for item in metric_results if item["interpretation"] == "regressed"]
        guardrail_regressions = [item for item in guardrail_results if item["interpretation"] == "regressed"]

        if guardrail_regressions:
            recommendation = "revert"
        elif positive and not regressions:
            recommendation = "keep"
        else:
            recommendation = "extend"

    return {
        "experiment_id": exp_row["experiment_id"],
        "name": exp_row["name"],
        "status": exp_row["status"],
        "current_range": current_range,
        "baseline_range": baseline_range,
        "metric_results": metric_results,
        "guardrails": guardrail_results,
        "scoreable_status": scoreable_status,
        "not_scoreable_reasons": not_scoreable_reasons,
        "recommendation": recommendation,
    }


def maybe_write_observations(conn: sqlite3.Connection, exp_row: sqlite3.Row, result: dict):
    observed_at = utc_now()
    reasons_json = json.dumps(result["not_scoreable_reasons"]) if result["not_scoreable_reasons"] else "[]"
    window_label = f"{result['baseline_range'][0]}..{result['baseline_range'][1]}__{result['current_range'][0]}..{result['current_range'][1]}"

    for item in [*result["metric_results"], *result["guardrails"]]:
        conn.execute(
            """
            INSERT INTO experiment_observations(
                experiment_id, observed_at, metric_name, metric_value, baseline_value, delta_value,
                interpretation, metric_scope, eligible_denominator, baseline_denominator,
                confidence_label, scoreable_status, not_scoreable_reasons_json, window_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["experiment_id"],
                observed_at,
                item["metric_name"],
                item["metric_value"],
                item["baseline_value"],
                item["delta_value"],
                item["interpretation"],
                item["metric_scope"],
                item.get("eligible_denominator"),
                item.get("baseline_denominator"),
                item.get("confidence_label"),
                result["scoreable_status"],
                reasons_json,
                window_label,
            ),
        )

    next_status = exp_row["status"]
    if exp_row["status"] == "ready_to_score" and result["scoreable_status"] == "scoreable":
        next_status = "scored"

    conn.execute(
        """
        UPDATE experiments
        SET status = ?,
            latest_recommendation = ?,
            latest_scoreable_status = ?,
            latest_not_scoreable_reasons_json = ?,
            latest_scored_at = ?
        WHERE experiment_id = ?
        """,
        (
            next_status,
            result["recommendation"],
            result["scoreable_status"],
            reasons_json,
            observed_at,
            result["experiment_id"],
        ),
    )


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    ensure_initialized(telemetry_root)
    as_of_date = parse_date(args.as_of)

    events_conn = sqlite3.connect(telemetry_root / "events.db")
    events_conn.row_factory = sqlite3.Row
    try:
        with experiments_connection(telemetry_root) as exp_conn:
            exp_conn.row_factory = sqlite3.Row
            experiments = fetch_experiments(exp_conn, args.experiment_id)
            results = [score_experiment(events_conn, row, as_of_date) for row in experiments]
            if args.write_observations:
                for row, result in zip(experiments, results):
                    maybe_write_observations(exp_conn, row, result)
    finally:
        events_conn.close()

    print(json.dumps({"as_of": as_of_date.isoformat(), "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
