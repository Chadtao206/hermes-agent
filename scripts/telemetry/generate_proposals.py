#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import audit_telemetry_completeness
import readiness_doctor
from common import ensure_initialized, experiments_connection, resolve_telemetry_root
from telemetry_evidence import confidence_label

PROPOSAL_TYPES = {
    "readiness_gate_fix",
    "telemetry_gap_repair",
    "experiment_not_scoreable_fix",
    "metric_regression_investigation",
    "metric_opportunity_investigation",
}

CONFIDENCE_SCORES = {
    "high": 0.9,
    "medium": 0.7,
    "low": 0.4,
    "not_ready": 0.1,
}

METRIC_DIRECTIONS = {
    "task_success_rate": "higher_is_better",
    "user_correction_rate": "lower_is_better",
    "reopened_task_rate": "lower_is_better",
}

METRIC_DENOMINATOR_KEYS = {
    "task_success_rate": "task_success_den",
    "user_correction_rate": "user_correction_den",
    "reopened_task_rate": "reopened_task_den",
}

METRIC_NUMERATOR_KEYS = {
    "task_success_rate": "task_success_num",
    "user_correction_rate": "user_correction_num",
    "reopened_task_rate": "reopened_task_num",
}


PACKET_FIELDS: tuple[tuple[str, str], ...] = (
    ("proposal_id", "proposal id"),
    ("title", "title"),
    ("decision_requested", "decision requested"),
    ("tl_dr", "TL;DR"),
    ("evidence", "evidence"),
    ("impact", "impact"),
    ("risk", "risk"),
    ("rollback", "rollback"),
    ("verification", "verification"),
    ("confidence", "confidence"),
    ("owner", "owner"),
    ("approve_deny_discuss", "approve/deny/discuss"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate evidence-backed proposal packets from telemetry readiness, completeness, and experiment state.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--output-dir", required=True, help="Directory to write proposal packet JSON/Markdown files")
    parser.add_argument("--kanban-db", default="/Users/ctao/.hermes/kanban.db", help="Path to kanban DB for readiness_doctor")
    parser.add_argument("--cron-state", default="/Users/ctao/.hermes/cron/jobs.json", help="Path to cron jobs.json for readiness_doctor")
    parser.add_argument("--readiness-json", help="Optional precomputed readiness_doctor JSON payload")
    parser.add_argument("--audit-json", help="Optional precomputed audit_telemetry_completeness JSON payload")
    parser.add_argument("--score-json", help="Optional precomputed score_experiment JSON payload")
    parser.add_argument("--dry-run", action="store_true", help="Render packets but do not write proposal rows into experiments.db")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(prefix: str, seed: str) -> str:
    compact = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in seed)
    compact = "-".join(part for part in compact.split("-") if part)
    compact = compact.lower()[:96] if compact else "unknown"
    return f"{prefix}:{compact}"


def load_json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def load_readiness(args: argparse.Namespace, telemetry_root: Path) -> dict[str, Any]:
    if args.readiness_json:
        return load_json_file(Path(args.readiness_json))

    readiness_args = argparse.Namespace(
        telemetry_root=str(telemetry_root),
        kanban_db=args.kanban_db,
        cron_state=args.cron_state,
        json_only=True,
    )
    ctx = readiness_doctor.build_context(readiness_args)
    try:
        return readiness_doctor.evaluate(ctx)
    finally:
        for conn in (ctx.events_conn, ctx.experiments_conn, ctx.kanban_conn):
            if conn is not None:
                conn.close()


def load_audit(args: argparse.Namespace, telemetry_root: Path) -> dict[str, Any]:
    if args.audit_json:
        return load_json_file(Path(args.audit_json))

    events_db = telemetry_root / "events.db"
    summary, rows = audit_telemetry_completeness.load_rows(events_db, "eligible", 1000)
    return {
        "summary": summary,
        "tasks": rows,
    }


def load_score(args: argparse.Namespace, telemetry_root: Path) -> dict[str, Any]:
    if args.score_json:
        return load_json_file(Path(args.score_json))

    conn = sqlite3.connect(telemetry_root / "experiments.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT experiment_id, name, latest_scoreable_status, latest_not_scoreable_reasons_json, latest_recommendation
            FROM experiments
            ORDER BY created_at, experiment_id
            """
        ).fetchall()
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for row in rows:
        reasons_raw = row["latest_not_scoreable_reasons_json"]
        try:
            reasons = json.loads(reasons_raw) if reasons_raw else []
        except json.JSONDecodeError:
            reasons = []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        results.append(
            {
                "experiment_id": row["experiment_id"],
                "name": row["name"],
                "scoreable_status": row["latest_scoreable_status"],
                "not_scoreable_reasons": reasons,
                "recommendation": row["latest_recommendation"],
            }
        )
    return {"results": results}


def confidence(label: str, *, basis: dict[str, Any]) -> dict[str, Any]:
    normalized = label if label in CONFIDENCE_SCORES else "low"
    return {
        "label": normalized,
        "score": CONFIDENCE_SCORES[normalized],
        "basis": basis,
    }


def metric_confidence_basis(
    *,
    metric_name: str,
    denominator_key: str,
    numerator_key: str,
    denominator: int,
    new_value: Any,
    old_value: Any,
    newest: dict[str, Any],
    previous: dict[str, Any],
    gate_blocked: bool,
) -> dict[str, Any]:
    """Return deterministic row-backed confidence evidence for metric proposals."""
    classification_context = {
        "bootstrap_tasks": newest.get("bootstrap_tasks"),
        "synthetic_tasks": newest.get("synthetic_tasks"),
        "seed_tasks": newest.get("seed_tasks"),
        "unknown_classification_tasks": newest.get("unknown_classification_tasks"),
    }
    contamination_sources = [
        name
        for name, value in classification_context.items()
        if isinstance(value, (int, float)) and value > 0
    ]
    readiness_overall_verdict = "NOT_COMPLETE" if gate_blocked else "COMPLETE"
    current_ref = f"bench_metrics_daily:{newest.get('date')}"
    previous_ref = f"bench_metrics_daily:{previous.get('date')}"
    return {
        "metric_name": metric_name,
        "denominator_key": denominator_key,
        "numerator_key": numerator_key,
        "denominator": denominator,
        "new": new_value,
        "old": old_value,
        "current_metric_ref": current_ref,
        "previous_metric_ref": previous_ref,
        "current_date": newest.get("date"),
        "baseline_date": previous.get("date"),
        "current_num": newest.get(numerator_key),
        "current_den": newest.get(denominator_key),
        "baseline_num": previous.get(numerator_key),
        "baseline_den": previous.get(denominator_key),
        "readiness_overall_verdict": readiness_overall_verdict,
        "readiness_gate_blocked": bool(gate_blocked),
        "telemetry_completeness_rate": newest.get("telemetry_completeness_rate"),
        "eligible_real_substantial_tasks": newest.get("eligible_real_substantial_tasks"),
        "readiness_state": {
            "overall_verdict": readiness_overall_verdict,
            "gate_blocked": bool(gate_blocked),
            "telemetry_completeness_rate": newest.get("telemetry_completeness_rate"),
            "eligible_real_substantial_tasks": newest.get("eligible_real_substantial_tasks"),
        },
        "evidence_provenance": {
            "baseline_ref": previous_ref,
            "current_ref": current_ref,
            "baseline_date": previous.get("date"),
            "current_date": newest.get("date"),
        },
        "classification_context": classification_context,
        "contamination_evidence": {
            "present": bool(contamination_sources),
            "sources": contamination_sources,
        },
        "contamination_present": bool(contamination_sources),
        "contamination_sources": contamination_sources,
        "test_evidence": "deterministic_generator_metric_confidence_basis_tests",
    }


def base_proposal(
    *,
    proposal_type: str,
    title: str,
    decision_requested: str,
    tl_dr: str,
    problem_statement: str,
    proposed_change: str,
    impact: dict[str, Any],
    risk_level: str,
    risk_notes: str,
    rollback_plan: str,
    verification_plan: str,
    confidence_payload: dict[str, Any],
    owner_profile: str,
    evidence: list[dict[str, Any]],
    approve_deny_discuss: str,
    linked_experiment_id: str | None = None,
    suppressed_by_gate: bool = False,
) -> dict[str, Any]:
    if proposal_type not in PROPOSAL_TYPES:
        raise ValueError(f"unknown proposal_type={proposal_type}")
    now = utc_now()
    seed = f"{proposal_type}:{title}:{owner_profile}:{linked_experiment_id or ''}"
    proposal_id = stable_id("proposal", seed)
    return {
        "proposal_id": proposal_id,
        "created_at": now,
        "updated_at": now,
        "proposal_type": proposal_type,
        "title": title,
        "status": "proposed",
        "owner_profile": owner_profile,
        "confidence_label": confidence_payload["label"],
        "confidence_score": confidence_payload["score"],
        "confidence_basis": confidence_payload["basis"],
        "decision_requested": decision_requested,
        "tl_dr": tl_dr,
        "problem_statement": problem_statement,
        "proposed_change": proposed_change,
        "expected_metric_impact": impact,
        "risk_level": risk_level,
        "risk_notes": risk_notes,
        "rollback_plan": rollback_plan,
        "verification_plan": verification_plan,
        "approved_at": None,
        "denied_at": None,
        "approver": None,
        "denial_reason": None,
        "applied_at": None,
        "verified_at": None,
        "scored_at": None,
        "outcome": "unknown",
        "linked_experiment_id": linked_experiment_id,
        "evidence": evidence,
        "approve_deny_discuss": approve_deny_discuss,
        "suppressed_by_gate": suppressed_by_gate,
    }


def render_packet_json(proposal: dict[str, Any]) -> OrderedDict[str, Any]:
    packet = OrderedDict()
    packet["proposal_id"] = proposal["proposal_id"]
    packet["title"] = proposal["title"]
    packet["decision_requested"] = proposal["decision_requested"]
    packet["tl_dr"] = proposal["tl_dr"]
    packet["evidence"] = proposal["evidence"]
    packet["impact"] = proposal["expected_metric_impact"]
    packet["risk"] = {
        "level": proposal["risk_level"],
        "notes": proposal["risk_notes"],
    }
    packet["rollback"] = proposal["rollback_plan"]
    packet["verification"] = proposal["verification_plan"]
    packet["confidence"] = {
        "score": proposal["confidence_score"],
        "band": proposal["confidence_label"],
        "basis": proposal["confidence_basis"],
    }
    packet["owner"] = proposal["owner_profile"]
    packet["approve_deny_discuss"] = proposal["approve_deny_discuss"]
    return packet


def _safe_evidence_summary(text: str, max_len: int = 200) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def render_packet_markdown(proposal: dict[str, Any]) -> str:
    packet = render_packet_json(proposal)
    lines = [f"# {packet['title']}", ""]
    lines.append(f"_Proposal ID: {packet['proposal_id']}_")
    lines.append("")
    if proposal.get("suppressed_by_gate"):
        lines.append("> Readiness gate NOT_COMPLETE — repair proposals only.")
        lines.append("")

    lines.append("## Decision requested")
    lines.append(str(packet["decision_requested"]))
    lines.append("")

    lines.append("## TL;DR")
    lines.append(str(packet["tl_dr"]))
    lines.append("")

    lines.append("## Evidence")
    for item in packet["evidence"]:
        lines.append(
            f"- {item.get('evidence_type')} | {item.get('evidence_ref')} | {_safe_evidence_summary(item.get('evidence_summary', ''))}"
        )
    lines.append("")

    lines.append("## Expected impact")
    lines.append(f"```json\n{json.dumps(packet['impact'], indent=2, sort_keys=True)}\n```")
    lines.append("")

    lines.append("## Risk / blast radius")
    lines.append(f"- level: {packet['risk']['level']}")
    lines.append(f"- notes: {packet['risk']['notes']}")
    lines.append("")

    lines.append("## Rollback")
    lines.append(str(packet["rollback"]))
    lines.append("")

    lines.append("## Verification")
    lines.append(str(packet["verification"]))
    lines.append("")

    lines.append("## Confidence")
    lines.append(f"- score: {packet['confidence']['score']}")
    lines.append(f"- band: {packet['confidence']['band']}")
    lines.append(f"- basis: {json.dumps(packet['confidence']['basis'], sort_keys=True)}")
    lines.append("")

    lines.append("## Owner")
    lines.append(str(packet["owner"]))
    lines.append("")

    lines.append("## Approve / Deny / Discuss")
    lines.append(str(packet["approve_deny_discuss"]))
    lines.append("")
    return "\n".join(lines)


def readiness_gate_proposals(readiness: dict[str, Any], gate_blocked: bool) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for gate in readiness.get("gates", []):
        if gate.get("status") == "pass":
            continue
        gate_id = str(gate.get("gate_id") or "unknown_gate")
        evidence = [
            {
                "evidence_type": "readiness_doctor",
                "evidence_ref": gate_id,
                "evidence_summary": _safe_evidence_summary(str(gate.get("summary") or "")),
            }
        ]
        proposals.append(
            base_proposal(
                proposal_type="readiness_gate_fix",
                title=f"Repair readiness gate: {gate_id}",
                decision_requested="approve",
                tl_dr=f"Readiness gate {gate_id} is failing and blocks strategic recommendations.",
                problem_statement=str(gate.get("summary") or ""),
                proposed_change="Execute the minimal deterministic remediation for this readiness gate and re-run readiness_doctor.",
                impact={"primary_metric": "overall_readiness", "expected_direction": "up"},
                risk_level="low",
                risk_notes="Repair-only gate fix; no broad strategy changes.",
                rollback_plan="No irreversible state mutation required for this proposal packet.",
                verification_plan=f"Re-run readiness_doctor and confirm gate {gate_id} status=pass.",
                confidence_payload=confidence(
                    "not_ready" if gate_blocked else "medium",
                    basis={"reasons": gate.get("reasons", [])[:3], "gate_id": gate_id},
                ),
                owner_profile="ops",
                evidence=evidence,
                approve_deny_discuss="approve to execute gate repair, deny only with alternate deterministic fix path.",
                suppressed_by_gate=gate_blocked,
            )
        )
    return proposals


def classify_gap_task(task: dict[str, Any]) -> str:
    gaps = [str(item) for item in task.get("telemetry_gaps") or []]
    if gaps == ["execution_runs"] and str(task.get("task_id", "")).startswith("kanban:"):
        return "deterministic_safe_repair"
    if gaps and all(item.startswith("missing_handoff_") for item in gaps):
        return "needs_evidence_or_human"
    return "historical_gap_candidate"


def telemetry_gap_proposals(audit: dict[str, Any], gate_blocked: bool) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for task in audit.get("tasks", []):
        task_id = str(task.get("task_id") or "unknown_task")
        bucket = classify_gap_task(task)
        if bucket == "historical_gap_candidate":
            continue

        deterministic = bucket == "deterministic_safe_repair"
        confidence_label_value = "medium" if deterministic else "low"
        risk_level = "low" if deterministic else "medium"
        decision_requested = "approve" if deterministic else "discuss"
        owner = str(task.get("owner_profile") or "ops")

        proposals.append(
            base_proposal(
                proposal_type="telemetry_gap_repair",
                title=f"Repair telemetry gaps for {task_id}",
                decision_requested=decision_requested,
                tl_dr=f"Telemetry completeness gaps remain for {task_id}; classify={bucket}.",
                problem_statement=f"Task {task_id} has telemetry gaps: {', '.join(task.get('telemetry_gaps') or [])}",
                proposed_change="Apply dry-run repair plan first, then execute only explicitly approved deterministic backfill steps.",
                impact={
                    "primary_metric": "telemetry_completeness_rate",
                    "expected_direction": "up",
                    "task_id": task_id,
                },
                risk_level=risk_level,
                risk_notes="Deterministic repairs are low risk; handoff/history repairs need human review.",
                rollback_plan="Dry-run first; defer any mutable backfill until explicit approval.",
                verification_plan="Re-run audit_telemetry_completeness and verify task drops from incomplete list.",
                confidence_payload=confidence(
                    "not_ready" if gate_blocked else confidence_label_value,
                    basis={"bucket": bucket, "telemetry_gaps": task.get("telemetry_gaps", [])},
                ),
                owner_profile=owner,
                evidence=[
                    {
                        "evidence_type": "task",
                        "evidence_ref": task_id,
                        "evidence_summary": _safe_evidence_summary(str(task.get("title") or task_id)),
                    }
                ],
                approve_deny_discuss="approve deterministic repairs only; discuss human-dependent repairs.",
                suppressed_by_gate=gate_blocked,
            )
        )
    return proposals


def experiment_not_scoreable_proposals(score_payload: dict[str, Any], gate_blocked: bool) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for item in score_payload.get("results", []):
        if str(item.get("scoreable_status") or "") != "not_scoreable":
            continue
        experiment_id = str(item.get("experiment_id") or "unknown_experiment")
        reasons = [str(reason) for reason in item.get("not_scoreable_reasons") or []]
        proposals.append(
            base_proposal(
                proposal_type="experiment_not_scoreable_fix",
                title=f"Make experiment scoreable: {experiment_id}",
                decision_requested="approve",
                tl_dr=f"Experiment {experiment_id} is not scoreable and cannot produce actionable recommendations.",
                problem_statement=f"Experiment marked not_scoreable with reasons: {', '.join(reasons) if reasons else 'unknown'}.",
                proposed_change="Address missing baseline/coverage blockers and re-run score_experiment with fresh observations.",
                impact={"primary_metric": "experiment_scoreability", "expected_direction": "up", "experiment_id": experiment_id},
                risk_level="low",
                risk_notes="Investigation/fix scoped to scoring readiness, not production behavior changes.",
                rollback_plan="No rollout mutation; this proposal changes telemetry evidence quality only.",
                verification_plan=f"Re-run score_experiment and confirm {experiment_id} scoreable_status=scoreable.",
                confidence_payload=confidence(
                    "not_ready" if gate_blocked else "medium",
                    basis={"not_scoreable_reasons": reasons},
                ),
                owner_profile="researcher",
                evidence=[
                    {
                        "evidence_type": "experiment",
                        "evidence_ref": experiment_id,
                        "evidence_summary": _safe_evidence_summary(str(item.get("name") or experiment_id)),
                    }
                ],
                approve_deny_discuss="approve remediation scope and required baseline collection window.",
                linked_experiment_id=experiment_id,
                suppressed_by_gate=gate_blocked,
            )
        )
    return proposals


def metric_change_proposals(telemetry_root: Path, gate_blocked: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conn = sqlite3.connect(telemetry_root / "events.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM bench_metrics_daily ORDER BY date DESC LIMIT 2").fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return [], []

    newest = dict(rows[0])
    previous = dict(rows[1])
    suppressed: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []

    for metric_name, direction in METRIC_DIRECTIONS.items():
        new_value = newest.get(metric_name)
        old_value = previous.get(metric_name)
        if new_value is None or old_value is None:
            continue

        delta = float(new_value) - float(old_value)
        is_regression = (
            (direction == "higher_is_better" and delta < -0.01)
            or (direction == "lower_is_better" and delta > 0.01)
        )
        is_opportunity = (
            (direction == "higher_is_better" and delta > 0.01)
            or (direction == "lower_is_better" and delta < -0.01)
        )

        if not (is_regression or is_opportunity):
            continue

        den_key = METRIC_DENOMINATOR_KEYS[metric_name]
        denominator = int(newest.get(den_key) or 0)
        band = confidence_label(denominator)
        normalized_band = "not_ready" if band == "insufficient" else band

        proposal_type = "metric_regression_investigation" if is_regression else "metric_opportunity_investigation"
        candidate = base_proposal(
            proposal_type=proposal_type,
            title=f"Investigate {metric_name} {'regression' if is_regression else 'opportunity'}",
            decision_requested="discuss",
            tl_dr=f"{metric_name} changed from {old_value:.3f} to {new_value:.3f} ({delta:+.3f}).",
            problem_statement=f"Recent day-over-day change detected for {metric_name}.",
            proposed_change="Run targeted root-cause analysis before any broad behavior change rollout.",
            impact={"metric_name": metric_name, "delta": delta, "direction": direction},
            risk_level="low",
            risk_notes="Investigation-only proposal with no immediate rollout action.",
            rollback_plan="No rollout mutation; investigation can be stopped without side effects.",
            verification_plan="Validate change over next window and correlate with workflow/task mix evidence.",
            confidence_payload=confidence(
                "not_ready" if gate_blocked else normalized_band,
                basis=metric_confidence_basis(
                    metric_name=metric_name,
                    denominator_key=den_key,
                    numerator_key=METRIC_NUMERATOR_KEYS[metric_name],
                    denominator=denominator,
                    new_value=new_value,
                    old_value=old_value,
                    newest=newest,
                    previous=previous,
                    gate_blocked=gate_blocked,
                ),
            ),
            owner_profile="researcher",
            evidence=[
                {
                    "evidence_type": "metric_rollup",
                    "evidence_ref": f"bench_metrics_daily:{newest.get('date')}",
                    "evidence_summary": _safe_evidence_summary(f"{metric_name} delta={delta:+.3f}"),
                }
            ],
            approve_deny_discuss="discuss investigation scope and success criteria.",
            suppressed_by_gate=gate_blocked,
        )

        if gate_blocked:
            suppressed.append(candidate)
        else:
            proposals.append(candidate)

    return proposals, suppressed


def persist_proposals(telemetry_root: Path, proposals: list[dict[str, Any]]) -> None:
    if not proposals:
        return

    with experiments_connection(telemetry_root) as conn:
        for proposal in proposals:
            packet = render_packet_json(proposal)
            conn.execute(
                """
                INSERT INTO proposals(
                    proposal_id, created_at, updated_at, proposal_type, title, status,
                    owner_profile, confidence_label, confidence_score, confidence_basis_json,
                    decision_requested, tl_dr, problem_statement, proposed_change,
                    expected_metric_impact_json, risk_level, risk_notes, rollback_plan,
                    verification_plan, approved_at, denied_at, approver, denial_reason,
                    applied_at, verified_at, scored_at, outcome, linked_experiment_id, packet_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    -- Preserve human-controlled lifecycle state. Status must
                    -- move only via explicit decision/apply commands, not
                    -- proposal regeneration upserts.
                    owner_profile=excluded.owner_profile,
                    confidence_label=excluded.confidence_label,
                    confidence_score=excluded.confidence_score,
                    confidence_basis_json=excluded.confidence_basis_json,
                    decision_requested=excluded.decision_requested,
                    tl_dr=excluded.tl_dr,
                    problem_statement=excluded.problem_statement,
                    proposed_change=excluded.proposed_change,
                    expected_metric_impact_json=excluded.expected_metric_impact_json,
                    risk_level=excluded.risk_level,
                    risk_notes=excluded.risk_notes,
                    rollback_plan=excluded.rollback_plan,
                    verification_plan=excluded.verification_plan,
                    outcome=excluded.outcome,
                    linked_experiment_id=excluded.linked_experiment_id,
                    packet_json=excluded.packet_json
                """,
                (
                    proposal["proposal_id"],
                    proposal["created_at"],
                    proposal["updated_at"],
                    proposal["proposal_type"],
                    proposal["title"],
                    proposal["status"],
                    proposal["owner_profile"],
                    proposal["confidence_label"],
                    proposal["confidence_score"],
                    json.dumps(proposal["confidence_basis"], sort_keys=True),
                    proposal["decision_requested"],
                    proposal["tl_dr"],
                    proposal["problem_statement"],
                    proposal["proposed_change"],
                    json.dumps(proposal["expected_metric_impact"], sort_keys=True),
                    proposal["risk_level"],
                    proposal["risk_notes"],
                    proposal["rollback_plan"],
                    proposal["verification_plan"],
                    proposal["approved_at"],
                    proposal["denied_at"],
                    proposal["approver"],
                    proposal["denial_reason"],
                    proposal["applied_at"],
                    proposal["verified_at"],
                    proposal["scored_at"],
                    proposal["outcome"],
                    proposal["linked_experiment_id"],
                    json.dumps(packet, sort_keys=False),
                ),
            )
            for evidence in proposal["evidence"]:
                conn.execute(
                    """
                    INSERT INTO proposal_evidence_links(
                        proposal_id, evidence_type, evidence_ref, evidence_summary,
                        confidence_contribution, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(proposal_id, evidence_type, evidence_ref) DO UPDATE SET
                        evidence_summary=excluded.evidence_summary,
                        confidence_contribution=excluded.confidence_contribution
                    """,
                    (
                        proposal["proposal_id"],
                        evidence.get("evidence_type"),
                        evidence.get("evidence_ref"),
                        evidence.get("evidence_summary"),
                        json.dumps(proposal["confidence_basis"], sort_keys=True),
                        proposal["updated_at"],
                    ),
                )


def write_packets(output_dir: Path, proposals: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for proposal in proposals:
        packet_json = render_packet_json(proposal)
        markdown = render_packet_markdown(proposal)
        (output_dir / f"{proposal['proposal_id']}.json").write_text(
            json.dumps(packet_json, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        (output_dir / f"{proposal['proposal_id']}.md").write_text(markdown, encoding="utf-8")
        (output_dir / f"{proposal['proposal_id']}.row.json").write_text(
            json.dumps(proposal, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    output_dir = Path(args.output_dir).expanduser().resolve()
    ensure_initialized(telemetry_root)

    readiness = load_readiness(args, telemetry_root)
    audit = load_audit(args, telemetry_root)
    score_payload = load_score(args, telemetry_root)

    gate_blocked = readiness.get("overall_verdict") == "NOT_COMPLETE"

    proposals: list[dict[str, Any]] = []
    proposals.extend(readiness_gate_proposals(readiness, gate_blocked))
    proposals.extend(telemetry_gap_proposals(audit, gate_blocked))
    proposals.extend(experiment_not_scoreable_proposals(score_payload, gate_blocked))

    metric_proposals, suppressed_metric_proposals = metric_change_proposals(telemetry_root, gate_blocked)
    proposals.extend(metric_proposals)

    write_packets(output_dir, proposals)
    if not args.dry_run:
        persist_proposals(telemetry_root, proposals)

    evaluated_at = utc_now()
    payload = {
        "evaluated_at": evaluated_at,
        "telemetry_root": str(telemetry_root),
        "output_dir": str(output_dir),
        "overall_verdict": readiness.get("overall_verdict"),
        "dry_run": bool(args.dry_run),
        "proposal_count": len(proposals),
        "suppressed_count": len(suppressed_metric_proposals),
        "proposals": [
            {
                "proposal_id": item["proposal_id"],
                "proposal_type": item["proposal_type"],
                "title": item["title"],
                "decision_requested": item["decision_requested"],
                "owner_profile": item["owner_profile"],
                "confidence_label": item["confidence_label"],
            }
            for item in proposals
        ],
        "suppressed": [
            {
                "proposal_id": item["proposal_id"],
                "proposal_type": item["proposal_type"],
                "title": item["title"],
            }
            for item in suppressed_metric_proposals
        ],
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
