#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
INIT_SCRIPT = THIS_DIR / "init_self_improvement_db.py"
GENERATOR_SCRIPT = THIS_DIR / "generate_proposals.py"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _run_init(telemetry_root: Path) -> None:
    proc = _run([sys.executable, str(INIT_SCRIPT), "--telemetry-root", str(telemetry_root)])
    if proc.returncode != 0:
        raise AssertionError(f"init failed: {proc.stdout}\n{proc.stderr}")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _seed_bench_rows(telemetry_root: Path) -> None:
    conn = sqlite3.connect(telemetry_root / "events.db")
    try:
        conn.execute(
            """
            INSERT INTO bench_metrics_daily(
                date, task_success_rate, user_correction_rate, reopened_task_rate,
                task_success_num, task_success_den,
                user_correction_num, user_correction_den,
                reopened_task_num, reopened_task_den,
                telemetry_completeness_rate, eligible_real_substantial_tasks,
                bootstrap_tasks, synthetic_tasks, seed_tasks, unknown_classification_tasks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-05-23", 0.85, 0.10, 0.02, 17, 20, 2, 20, 1, 50, 1.0, 20, 0, 0, 0, 0),
        )
        conn.execute(
            """
            INSERT INTO bench_metrics_daily(
                date, task_success_rate, user_correction_rate, reopened_task_rate,
                task_success_num, task_success_den,
                user_correction_num, user_correction_den,
                reopened_task_num, reopened_task_den,
                telemetry_completeness_rate, eligible_real_substantial_tasks,
                bootstrap_tasks, synthetic_tasks, seed_tasks, unknown_classification_tasks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-05-24", 0.70, 0.20, 0.10, 14, 20, 4, 8, 5, 50, 0.99, 20, 0, 0, 0, 0),
        )
        conn.commit()
    finally:
        conn.close()


def _run_generator(telemetry_root: Path, output_dir: Path, readiness_json: Path, audit_json: Path, score_json: Path, dry_run: bool = True, extra: list[str] | None = None) -> dict:
    cmd = [
        sys.executable,
        str(GENERATOR_SCRIPT),
        "--telemetry-root",
        str(telemetry_root),
        "--output-dir",
        str(output_dir),
        "--readiness-json",
        str(readiness_json),
        "--audit-json",
        str(audit_json),
        "--score-json",
        str(score_json),
    ]
    if dry_run:
        cmd.append("--dry-run")
    if extra:
        cmd.extend(extra)
    proc = _run(cmd)
    if proc.returncode != 0:
        raise AssertionError(f"generator failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def _proposal_types(payload: dict) -> set[str]:
    return {item["proposal_type"] for item in payload.get("proposals", [])}


def _metric_regression_basis_by_metric(output_dir: Path) -> dict[str, dict]:
    by_metric: dict[str, dict] = {}
    for path in output_dir.glob("*.json"):
        packet = json.loads(path.read_text(encoding="utf-8"))
        confidence = packet.get("confidence") or {}
        basis = confidence.get("basis") or {}
        metric_name = basis.get("metric_name")
        if metric_name:
            by_metric[metric_name] = basis
    return by_metric


def _run_complete_mode_payload(root: Path, telemetry_root: Path, output_dir: Path) -> dict:
    readiness = {"overall_verdict": "COMPLETE", "gates": []}
    audit = {"summary": {"incomplete_tasks": 0}, "tasks": []}
    score = {"results": []}

    readiness_path = root / "readiness.json"
    audit_path = root / "audit.json"
    score_path = root / "score.json"
    _write_json(readiness_path, readiness)
    _write_json(audit_path, audit)
    _write_json(score_path, score)
    return _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=True)


def case_not_complete_repair_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        readiness = {
            "overall_verdict": "NOT_COMPLETE",
            "gates": [
                {
                    "gate_id": "weekly_report_grounded_in_real_work",
                    "status": "fail",
                    "summary": "Weekly report stale",
                    "reasons": ["Latest report file is stale"],
                    "evidence": {"weekly_report_path": "/tmp/weekly-report.md"},
                },
                {
                    "gate_id": "experiment_baseline_observation_readiness",
                    "status": "fail",
                    "summary": "Experiment readiness incomplete",
                    "reasons": ["All non-bootstrap experiments still yield non-actionable recommendations."],
                    "evidence": {"non_bootstrap_experiment_ids": ["exp-kanban-specialist-routing"]},
                },
            ],
        }
        audit = {
            "summary": {"incomplete_tasks": 2},
            "tasks": [
                {
                    "task_id": "kanban:t_d80b4448",
                    "title": "repairable task",
                    "owner_profile": "ops",
                    "telemetry_gaps": ["execution_runs"],
                    "closeout_source": "closeout",
                },
                {
                    "task_id": "jira:PLAT-1898",
                    "title": "needs handoff evidence",
                    "owner_profile": "engineer",
                    "telemetry_gaps": [
                        "missing_handoff_started",
                        "missing_handoff_accepted",
                        "missing_handoff_resolved",
                        "missing_handoff_sent",
                    ],
                    "closeout_source": "closeout",
                },
            ],
        }
        score = {
            "results": [
                {
                    "experiment_id": "exp-kanban-specialist-routing",
                    "name": "routing",
                    "scoreable_status": "not_scoreable",
                    "not_scoreable_reasons": ["no_baseline"],
                    "recommendation": "not_scoreable",
                }
            ]
        }

        readiness_path = root / "readiness.json"
        audit_path = root / "audit.json"
        score_path = root / "score.json"
        _write_json(readiness_path, readiness)
        _write_json(audit_path, audit)
        _write_json(score_path, score)

        payload = _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=True)
        types = _proposal_types(payload)
        assert "readiness_gate_fix" in types, types
        assert "telemetry_gap_repair" in types, types
        assert "experiment_not_scoreable_fix" in types, types
        assert "metric_regression_investigation" not in types, types
        assert "metric_opportunity_investigation" not in types, types
        assert payload.get("suppressed_count", 0) >= 1, payload

        files = list(output_dir.glob("*.md"))
        assert files, "expected markdown proposal packets"
        markdown = files[0].read_text(encoding="utf-8")
        assert "_Proposal ID: proposal:" in markdown, markdown
        json_files = list(output_dir.glob("*.json"))
        assert json_files, "expected JSON proposal packets"
        packet = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert str(packet.get("proposal_id", "")).startswith("proposal:"), packet
        assert packet["proposal_id"] in json_files[0].name, (packet, json_files[0])
        row_files = list(output_dir.glob("*.row.json"))
        assert row_files, "expected ledger-row JSON proposal packets for dry-run import"
        expected_order = [
            "Decision requested",
            "TL;DR",
            "Evidence",
            "Expected impact",
            "Risk / blast radius",
            "Rollback",
            "Verification",
            "Confidence",
            "Owner",
            "Approve / Deny / Discuss",
        ]
        idx = -1
        for marker in expected_order:
            next_idx = markdown.find(marker)
            assert next_idx > idx, f"section order invalid for {marker}"
            idx = next_idx

        conn = sqlite3.connect(telemetry_root / "experiments.db")
        try:
            row = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()
            assert row[0] == 0, row[0]
        finally:
            conn.close()


def case_complete_emits_metric_regression() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        payload = _run_complete_mode_payload(root, telemetry_root, output_dir)
        types = _proposal_types(payload)
        assert "metric_regression_investigation" in types, types

        basis_by_metric = _metric_regression_basis_by_metric(output_dir)
        basis = basis_by_metric["reopened_task_rate"]
        assert basis["denominator_key"] == "reopened_task_den", basis
        assert basis["denominator"] == 50, basis
        assert basis["numerator_key"] == "reopened_task_num", basis
        assert basis["current_num"] == 5, basis
        assert basis["current_den"] == 50, basis
        assert basis["baseline_num"] == 1, basis
        assert basis["baseline_den"] == 50, basis
        assert basis["current_date"] == "2026-05-24", basis
        assert basis["baseline_date"] == "2026-05-23", basis
        assert basis["current_metric_ref"] == "bench_metrics_daily:2026-05-24", basis
        assert basis["previous_metric_ref"] == "bench_metrics_daily:2026-05-23", basis
        assert basis["readiness_overall_verdict"] == "COMPLETE", basis
        assert basis["readiness_gate_blocked"] is False, basis
        assert basis["readiness_state"]["overall_verdict"] == "COMPLETE", basis
        assert basis["readiness_state"]["gate_blocked"] is False, basis
        assert basis["telemetry_completeness_rate"] == 0.99, basis
        assert basis["eligible_real_substantial_tasks"] == 20, basis
        assert basis["classification_context"]["unknown_classification_tasks"] == 0, basis
        assert basis["evidence_provenance"]["current_ref"] == "bench_metrics_daily:2026-05-24", basis
        assert basis["evidence_provenance"]["baseline_ref"] == "bench_metrics_daily:2026-05-23", basis
        assert basis["contamination_present"] is False, basis
        assert basis["contamination_sources"] == [], basis
        assert basis["contamination_evidence"]["present"] is False, basis
        assert basis["contamination_evidence"]["sources"] == [], basis
        assert basis["test_evidence"] == "deterministic_generator_metric_confidence_basis_tests", basis


def case_complete_basis_task_success_rate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        payload = _run_complete_mode_payload(root, telemetry_root, output_dir)
        basis = _metric_regression_basis_by_metric(output_dir)["task_success_rate"]
        assert basis["metric_name"] == "task_success_rate", basis
        assert basis["denominator_key"] == "task_success_den", basis
        assert basis["numerator_key"] == "task_success_num", basis
        assert basis["denominator"] == 20, basis
        assert basis["current_num"] == 14, basis
        assert basis["current_den"] == 20, basis
        assert basis["baseline_num"] == 17, basis
        assert basis["baseline_den"] == 20, basis
        assert basis["current_date"] == "2026-05-24", basis
        assert basis["baseline_date"] == "2026-05-23", basis
        assert basis["new"] == 0.70, basis
        assert basis["old"] == 0.85, basis
        assert basis["current_metric_ref"] == "bench_metrics_daily:2026-05-24", basis
        assert basis["previous_metric_ref"] == "bench_metrics_daily:2026-05-23", basis
        assert basis["readiness_overall_verdict"] == "COMPLETE", basis
        assert basis["readiness_gate_blocked"] is False, basis
        assert basis["readiness_state"]["overall_verdict"] == "COMPLETE", basis
        assert basis["readiness_state"]["gate_blocked"] is False, basis
        assert basis["telemetry_completeness_rate"] == 0.99, basis
        assert basis["eligible_real_substantial_tasks"] == 20, basis
        assert basis["evidence_provenance"]["current_ref"] == "bench_metrics_daily:2026-05-24", basis
        assert basis["evidence_provenance"]["baseline_ref"] == "bench_metrics_daily:2026-05-23", basis
        assert basis["contamination_present"] is False, basis
        assert basis["contamination_sources"] == [], basis
        assert basis["contamination_evidence"]["present"] is False, basis
        assert basis["contamination_evidence"]["sources"] == [], basis
        assert basis["test_evidence"] == "deterministic_generator_metric_confidence_basis_tests", basis


def case_complete_basis_user_correction_rate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        payload = _run_complete_mode_payload(root, telemetry_root, output_dir)
        basis = _metric_regression_basis_by_metric(output_dir)["user_correction_rate"]
        assert basis["metric_name"] == "user_correction_rate", basis
        assert basis["denominator_key"] == "user_correction_den", basis
        assert basis["numerator_key"] == "user_correction_num", basis
        assert basis["denominator"] == 8, basis
        assert basis["current_num"] == 4, basis
        assert basis["current_den"] == 8, basis
        assert basis["baseline_num"] == 2, basis
        assert basis["baseline_den"] == 20, basis
        assert basis["current_date"] == "2026-05-24", basis
        assert basis["baseline_date"] == "2026-05-23", basis
        assert basis["new"] == 0.20, basis
        assert basis["old"] == 0.10, basis
        assert basis["readiness_overall_verdict"] == "COMPLETE", basis
        assert basis["readiness_gate_blocked"] is False, basis
        assert basis["contamination_present"] is False, basis
        assert basis["contamination_sources"] == [], basis
        assert basis["test_evidence"] == "deterministic_generator_metric_confidence_basis_tests", basis


def case_complete_basis_reopened_task_rate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        payload = _run_complete_mode_payload(root, telemetry_root, output_dir)
        basis = _metric_regression_basis_by_metric(output_dir)["reopened_task_rate"]
        assert basis["metric_name"] == "reopened_task_rate", basis
        assert basis["denominator_key"] == "reopened_task_den", basis
        assert basis["numerator_key"] == "reopened_task_num", basis
        assert basis["denominator"] == 50, basis
        assert basis["current_num"] == 5, basis
        assert basis["current_den"] == 50, basis
        assert basis["baseline_num"] == 1, basis
        assert basis["baseline_den"] == 50, basis
        assert basis["current_date"] == "2026-05-24", basis
        assert basis["baseline_date"] == "2026-05-23", basis
        assert basis["new"] == 0.10, basis
        assert basis["old"] == 0.02, basis
        assert basis["readiness_overall_verdict"] == "COMPLETE", basis
        assert basis["readiness_gate_blocked"] is False, basis
        assert basis["contamination_present"] is False, basis
        assert basis["contamination_sources"] == [], basis
        assert basis["test_evidence"] == "deterministic_generator_metric_confidence_basis_tests", basis


def case_persist_proposals_preserves_human_lifecycle_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        readiness = {"overall_verdict": "COMPLETE", "gates": []}
        audit = {"summary": {"incomplete_tasks": 0}, "tasks": []}
        score = {"results": []}

        readiness_path = root / "readiness.json"
        audit_path = root / "audit.json"
        score_path = root / "score.json"
        _write_json(readiness_path, readiness)
        _write_json(audit_path, audit)
        _write_json(score_path, score)

        first = _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=False)
        proposal_ids = [item["proposal_id"] for item in first.get("proposals", [])]
        assert len(proposal_ids) >= 2, proposal_ids

        approved_id = proposal_ids[0]
        applied_id = proposal_ids[1]

        conn = sqlite3.connect(telemetry_root / "experiments.db")
        try:
            conn.execute(
                """
                UPDATE proposals
                SET status = 'approved', approved_at = '2026-05-25T10:00:00+00:00',
                    approver = 'Chad Tao', updated_at = '2026-05-25T10:00:00+00:00'
                WHERE proposal_id = ?
                """,
                (approved_id,),
            )
            conn.execute(
                """
                UPDATE proposals
                SET status = 'applied', approved_at = '2026-05-25T09:00:00+00:00',
                    applied_at = '2026-05-25T11:00:00+00:00', approver = 'Chad Tao',
                    updated_at = '2026-05-25T11:00:00+00:00'
                WHERE proposal_id = ?
                """,
                (applied_id,),
            )
            conn.commit()
        finally:
            conn.close()

        _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=False)

        conn = sqlite3.connect(telemetry_root / "experiments.db")
        try:
            approved_row = conn.execute(
                "SELECT status, approved_at, approver FROM proposals WHERE proposal_id = ?",
                (approved_id,),
            ).fetchone()
            assert approved_row == ("approved", "2026-05-25T10:00:00+00:00", "Chad Tao"), approved_row

            applied_row = conn.execute(
                "SELECT status, approved_at, applied_at, approver FROM proposals WHERE proposal_id = ?",
                (applied_id,),
            ).fetchone()
            assert applied_row == (
                "applied",
                "2026-05-25T09:00:00+00:00",
                "2026-05-25T11:00:00+00:00",
                "Chad Tao",
            ), applied_row
        finally:
            conn.close()


def case_regeneration_never_clobbers_outcome_or_touches_progressed_rows() -> None:
    """A verified row must survive regeneration untouched (incl. outcome/verified_at)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        readiness = {"overall_verdict": "COMPLETE", "gates": []}
        audit = {"summary": {"incomplete_tasks": 0}, "tasks": []}
        score = {"results": []}

        readiness_path = root / "readiness.json"
        audit_path = root / "audit.json"
        score_path = root / "score.json"
        _write_json(readiness_path, readiness)
        _write_json(audit_path, audit)
        _write_json(score_path, score)

        first = _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=False)
        proposal_ids = [item["proposal_id"] for item in first.get("proposals", [])]
        assert len(proposal_ids) >= 2, proposal_ids

        verified_id = proposal_ids[0]
        proposed_id = proposal_ids[1]
        frozen_updated_at = "2026-05-26T00:04:28+00:00"

        conn = sqlite3.connect(telemetry_root / "experiments.db")
        try:
            conn.execute(
                """
                UPDATE proposals
                SET status = 'verified', outcome = 'success',
                    approved_at = '2026-05-25T19:35:44+00:00',
                    applied_at = '2026-05-25T19:36:10+00:00',
                    verified_at = '1779737928',
                    approver = 'Chad Tao', updated_at = ?
                WHERE proposal_id = ?
                """,
                (frozen_updated_at, verified_id),
            )
            conn.commit()
        finally:
            conn.close()

        _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=False)

        conn = sqlite3.connect(telemetry_root / "experiments.db")
        try:
            verified_row = conn.execute(
                "SELECT status, outcome, verified_at, updated_at FROM proposals WHERE proposal_id = ?",
                (verified_id,),
            ).fetchone()
            assert verified_row == (
                "verified",
                "success",
                "1779737928",
                frozen_updated_at,
            ), verified_row

            proposed_row = conn.execute(
                "SELECT status, outcome FROM proposals WHERE proposal_id = ?",
                (proposed_id,),
            ).fetchone()
            assert proposed_row == ("proposed", "unknown"), proposed_row
        finally:
            conn.close()


def _repair_only_fixture(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    telemetry_root = root / "telemetry"
    output_dir = root / "proposals"
    _run_init(telemetry_root)
    _seed_bench_rows(telemetry_root)
    readiness = {
        "overall_verdict": "NOT_COMPLETE",
        "gates": [
            {
                "gate_id": "weekly_report_grounded_in_real_work",
                "status": "fail",
                "summary": "Weekly report stale",
                "reasons": ["Latest report file is stale"],
                "evidence": {},
            }
        ],
    }
    readiness_path = root / "readiness.json"
    audit_path = root / "audit.json"
    score_path = root / "score.json"
    _write_json(readiness_path, readiness)
    _write_json(audit_path, {"summary": {"incomplete_tasks": 0}, "tasks": []})
    _write_json(score_path, {"results": []})
    return telemetry_root, output_dir, readiness_path, audit_path, score_path


def case_ledger_confidence_floor_keeps_not_ready_file_only() -> None:
    """not_ready packets render to files but stay out of the ledger by default."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root, output_dir, readiness_path, audit_path, score_path = _repair_only_fixture(root)

        payload = _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=False)
        assert payload["proposal_count"] == 1, payload
        assert payload["min_ledger_confidence"] == "medium", payload
        assert payload["ledger_persisted_count"] == 0, payload
        assert payload["file_only_count"] == 1, payload

        count = sqlite3.connect(telemetry_root / "experiments.db").execute(
            "SELECT COUNT(*) FROM proposals"
        ).fetchone()[0]
        assert count == 0, count

        packet_files = list(output_dir.glob("proposal:*.row.json"))
        assert len(packet_files) == 1, packet_files

        lowered = _run_generator(
            telemetry_root, output_dir, readiness_path, audit_path, score_path,
            dry_run=False, extra=["--min-ledger-confidence", "not_ready"],
        )
        assert lowered["ledger_persisted_count"] == 1, lowered
        count = sqlite3.connect(telemetry_root / "experiments.db").execute(
            "SELECT COUNT(*) FROM proposals"
        ).fetchone()[0]
        assert count == 1, count


def case_gap_repair_skips_stale_closed_tasks() -> None:
    """telemetry_gap_repair proposals are TTL'd: tasks closed long ago are skipped, with a log."""
    from datetime import datetime, timedelta, timezone

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        output_dir = root / "proposals"
        _run_init(telemetry_root)
        _seed_bench_rows(telemetry_root)

        now = datetime.now(timezone.utc)
        fresh_closed = (now - timedelta(days=1)).isoformat()
        stale_closed = (now - timedelta(days=30)).isoformat()

        readiness = {"overall_verdict": "COMPLETE", "gates": []}
        audit = {
            "summary": {"incomplete_tasks": 2},
            "tasks": [
                {
                    "task_id": "jira:FRESH-1",
                    "title": "fresh task",
                    "owner_profile": "engineer",
                    "telemetry_gaps": ["missing_handoff_sent"],
                    "closed_at": fresh_closed,
                },
                {
                    "task_id": "jira:STALE-1",
                    "title": "stale task",
                    "owner_profile": "engineer",
                    "telemetry_gaps": ["missing_handoff_sent"],
                    "closed_at": stale_closed,
                },
            ],
        }
        readiness_path = root / "readiness.json"
        audit_path = root / "audit.json"
        score_path = root / "score.json"
        _write_json(readiness_path, readiness)
        _write_json(audit_path, audit)
        _write_json(score_path, {"results": []})

        payload = _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=True)
        gap_ids = [
            item["proposal_id"]
            for item in payload.get("proposals", [])
            if item["proposal_type"] == "telemetry_gap_repair"
        ]
        assert any("fresh-1" in pid for pid in gap_ids), gap_ids
        assert not any("stale-1" in pid for pid in gap_ids), gap_ids
        assert payload["skipped_stale_gap_tasks"] == ["jira:STALE-1"], payload


def case_stale_packets_archived_only_unledgered() -> None:
    """Packet files for proposals no longer generated nor ledgered move to archive/."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root, output_dir, readiness_path, audit_path, score_path = _repair_only_fixture(root)
        output_dir.mkdir(parents=True, exist_ok=True)

        stale_id = "proposal:telemetry_gap_repair-repair-telemetry-gaps-for-jira-old-99-engineer"
        for suffix in (".json", ".md", ".row.json"):
            (output_dir / f"{stale_id}{suffix}").write_text("{}", encoding="utf-8")
        apply_artifact = output_dir / f"{stale_id}.apply.json"
        apply_artifact.write_text("{}", encoding="utf-8")

        ledgered_id = "proposal:readiness_gate_fix-repair-readiness-gate-old-gate-ops"
        (output_dir / f"{ledgered_id}.json").write_text("{}", encoding="utf-8")
        conn = sqlite3.connect(telemetry_root / "experiments.db")
        try:
            conn.execute(
                """
                INSERT INTO proposals(
                    proposal_id, created_at, updated_at, proposal_type, title, status,
                    owner_profile, confidence_label, confidence_score, confidence_basis_json,
                    decision_requested, tl_dr, problem_statement, proposed_change,
                    expected_metric_impact_json, risk_level, risk_notes, rollback_plan,
                    verification_plan, packet_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledgered_id, "2026-05-25T00:00:00+00:00", "2026-05-25T00:00:00+00:00",
                    "readiness_gate_fix", "old gate", "verified", "ops", "high", 0.9, "{}",
                    "approve", "tl;dr", "problem", "change", "{}", "low", "notes",
                    "rollback", "verify", "{}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        payload = _run_generator(telemetry_root, output_dir, readiness_path, audit_path, score_path, dry_run=True)
        archived = payload.get("archived_stale_packets", [])
        assert sorted(archived) == sorted(
            [f"{stale_id}.json", f"{stale_id}.md", f"{stale_id}.row.json"]
        ), archived

        archive_dir = output_dir / "archive"
        for suffix in (".json", ".md", ".row.json"):
            assert not (output_dir / f"{stale_id}{suffix}").exists(), suffix
            assert (archive_dir / f"{stale_id}{suffix}").exists(), suffix
        assert apply_artifact.exists(), "apply artifacts must never be archived"
        assert (output_dir / f"{ledgered_id}.json").exists(), "ledgered packets must never be archived"
        assert list(output_dir.glob("proposal:*.row.json")), "current packets should remain"


def main() -> int:
    case_not_complete_repair_only()
    case_complete_emits_metric_regression()
    case_complete_basis_task_success_rate()
    case_complete_basis_user_correction_rate()
    case_complete_basis_reopened_task_rate()
    case_persist_proposals_preserves_human_lifecycle_status()
    case_regeneration_never_clobbers_outcome_or_touches_progressed_rows()
    case_ledger_confidence_floor_keeps_not_ready_file_only()
    case_gap_repair_skips_stale_closed_tasks()
    case_stale_packets_archived_only_unledgered()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
