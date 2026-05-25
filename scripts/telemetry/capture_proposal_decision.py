#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import ensure_initialized, resolve_telemetry_root
from init_self_improvement_db import ensure_directories

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposal_decision_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    approver TEXT NOT NULL,
    reason TEXT,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    source TEXT,
    backup_path TEXT NOT NULL
)
"""

AUDIT_INDEX = "CREATE INDEX IF NOT EXISTS idx_proposal_decision_audit_proposal_id ON proposal_decision_audit(proposal_id)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)[:120]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a human approve/deny/discuss/needs_changes decision for a self-improvement proposal. "
            "This updates only the proposal ledger/audit tables in experiments.db."
        )
    )
    parser.add_argument("proposal_id", help="Exact proposal_id to decision")
    parser.add_argument("--decision", choices=("approve", "deny", "discuss", "needs_changes"), required=True)
    parser.add_argument("--approver", required=True, help="Human approver identity, e.g. Chad Tao")
    parser.add_argument("--reason", help="Required for deny/discuss/needs_changes; optional approval note")
    parser.add_argument("--source", default="manual", help="Decision source/provenance, e.g. Slack thread id")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--proposal-dir", help="Directory containing dry-run proposal packet files")
    parser.add_argument("--packet", help="Specific dry-run packet JSON or row JSON to import if the proposal row is missing")
    parser.add_argument("--backup-dir", help="Directory for pre-write experiments.db backups")
    parser.add_argument("--force", action="store_true", help="Allow changing an already approved/denied proposal decision")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def find_packet(proposal_id: str, proposal_dir: Path, explicit_packet: str | None) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[Path] = []
    if explicit_packet:
        candidates.append(Path(explicit_packet).expanduser().resolve())
    candidates.extend(
        [
            proposal_dir / f"{proposal_id}.row.json",
            proposal_dir / f"{proposal_id}.json",
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        payload = load_json(path)
        if payload.get("proposal_id") != proposal_id:
            raise ValueError(f"Packet {path} has proposal_id={payload.get('proposal_id')!r}, expected {proposal_id!r}")
        return path, payload
    return None


def normalize_import_row(packet: dict[str, Any]) -> dict[str, Any]:
    confidence = packet.get("confidence") if isinstance(packet.get("confidence"), dict) else {}
    risk = packet.get("risk") if isinstance(packet.get("risk"), dict) else {}

    confidence_basis = packet.get("confidence_basis")
    if confidence_basis is None:
        confidence_basis = confidence.get("basis") or {}

    impact = packet.get("expected_metric_impact")
    if impact is None:
        impact = packet.get("impact") or {}

    return {
        "proposal_id": packet["proposal_id"],
        "created_at": packet.get("created_at") or utc_now(),
        "updated_at": packet.get("updated_at") or utc_now(),
        "proposal_type": packet.get("proposal_type") or "manual_packet_import",
        "title": packet.get("title") or packet["proposal_id"],
        "status": packet.get("status") or "proposed",
        "owner_profile": packet.get("owner_profile") or packet.get("owner") or "default",
        "confidence_label": packet.get("confidence_label") or confidence.get("band") or "not_ready",
        "confidence_score": packet.get("confidence_score") if packet.get("confidence_score") is not None else confidence.get("score"),
        "confidence_basis_json": json.dumps(confidence_basis, sort_keys=True),
        "decision_requested": packet.get("decision_requested") or "discuss",
        "tl_dr": packet.get("tl_dr") or packet.get("title") or packet["proposal_id"],
        "problem_statement": packet.get("problem_statement") or packet.get("tl_dr") or packet.get("title") or packet["proposal_id"],
        "proposed_change": packet.get("proposed_change") or packet.get("approve_deny_discuss") or "Imported from dry-run proposal packet for human decision capture.",
        "expected_metric_impact_json": json.dumps(impact, sort_keys=True),
        "risk_level": packet.get("risk_level") or risk.get("level") or "unknown",
        "risk_notes": packet.get("risk_notes") or risk.get("notes") or "Imported from dry-run proposal packet.",
        "rollback_plan": packet.get("rollback_plan") or packet.get("rollback") or "No implementation mutation is performed by decision capture.",
        "verification_plan": packet.get("verification_plan") or packet.get("verification") or "Verify proposal ledger decision fields and audit row.",
        "approved_at": packet.get("approved_at"),
        "denied_at": packet.get("denied_at"),
        "approver": packet.get("approver"),
        "denial_reason": packet.get("denial_reason"),
        "applied_at": packet.get("applied_at"),
        "verified_at": packet.get("verified_at"),
        "scored_at": packet.get("scored_at"),
        "outcome": packet.get("outcome") or "unknown",
        "linked_experiment_id": packet.get("linked_experiment_id"),
        "packet_json": json.dumps(packet, sort_keys=False),
        "evidence": packet.get("evidence") if isinstance(packet.get("evidence"), list) else [],
    }


def backup_database(experiments_db: Path, backup_dir: Path, proposal_id: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"experiments.db.{utc_now().replace(':', '').replace('+', 'Z')}.{safe_name(proposal_id)}.bak"
    src = sqlite3.connect(experiments_db)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


def require_quick_check_ok(experiments_db: Path, *, stage: str) -> None:
    conn = sqlite3.connect(experiments_db)
    try:
        rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    finally:
        conn.close()
    if rows != ["ok"]:
        detail = "; ".join(rows) if rows else "no result"
        raise ValueError(f"experiments.db quick_check failed at {stage}: {detail}")


def ensure_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(AUDIT_SCHEMA)
    conn.execute(AUDIT_INDEX)


def upsert_imported_proposal(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
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
        ON CONFLICT(proposal_id) DO NOTHING
        """,
        (
            row["proposal_id"],
            row["created_at"],
            row["updated_at"],
            row["proposal_type"],
            row["title"],
            row["status"],
            row["owner_profile"],
            row["confidence_label"],
            row["confidence_score"],
            row["confidence_basis_json"],
            row["decision_requested"],
            row["tl_dr"],
            row["problem_statement"],
            row["proposed_change"],
            row["expected_metric_impact_json"],
            row["risk_level"],
            row["risk_notes"],
            row["rollback_plan"],
            row["verification_plan"],
            row["approved_at"],
            row["denied_at"],
            row["approver"],
            row["denial_reason"],
            row["applied_at"],
            row["verified_at"],
            row["scored_at"],
            row["outcome"],
            row["linked_experiment_id"],
            row["packet_json"],
        ),
    )
    for evidence in row.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        evidence_type = evidence.get("evidence_type") or evidence.get("type") or "packet"
        evidence_ref = evidence.get("evidence_ref") or evidence.get("ref") or row["proposal_id"]
        evidence_summary = evidence.get("evidence_summary") or evidence.get("summary") or "Imported packet evidence"
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
                row["proposal_id"],
                str(evidence_type),
                str(evidence_ref),
                str(evidence_summary),
                row["confidence_basis_json"],
                utc_now(),
            ),
        )


def capture_decision(args: argparse.Namespace) -> dict[str, Any]:
    if args.decision in {"deny", "discuss", "needs_changes"} and not (args.reason or "").strip():
        raise ValueError("--reason is required when --decision deny/discuss/needs_changes")

    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    proposal_dir = Path(args.proposal_dir).expanduser().resolve() if args.proposal_dir else telemetry_root / "proposals"
    backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else telemetry_root / "backups" / "proposal_decisions"

    ensure_directories(telemetry_root)
    experiments_db = telemetry_root / "experiments.db"
    backup_path: Path | None = None
    if experiments_db.exists():
        require_quick_check_ok(experiments_db, stage="pre_backup")
        backup_path = backup_database(experiments_db, backup_dir, args.proposal_id)

    ensure_initialized(telemetry_root)
    require_quick_check_ok(experiments_db, stage="pre_write")
    if backup_path is None:
        backup_path = backup_database(experiments_db, backup_dir, args.proposal_id)

    decided_at = utc_now()
    decision_to_status = {
        "approve": "approved",
        "deny": "denied",
        "discuss": "discussing",
        "needs_changes": "needs_changes",
    }
    new_status = decision_to_status[args.decision]
    packet_source: str | None = None
    imported = False

    conn = sqlite3.connect(experiments_db)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_audit_schema(conn)
        current = conn.execute("SELECT status FROM proposals WHERE proposal_id = ?", (args.proposal_id,)).fetchone()
        if current is None:
            found = find_packet(args.proposal_id, proposal_dir, args.packet)
            if found is None:
                raise ValueError(f"proposal_id {args.proposal_id!r} not found in ledger and no matching packet found")
            packet_path, packet = found
            packet_source = str(packet_path)
            upsert_imported_proposal(conn, normalize_import_row(packet))
            imported = True
            current = conn.execute("SELECT status FROM proposals WHERE proposal_id = ?", (args.proposal_id,)).fetchone()

        previous_status = str(current[0]) if current else None
        if previous_status in {"approved", "denied"} and previous_status != new_status and not args.force:
            raise ValueError(f"proposal already {previous_status}; pass --force to change the decision")

        if args.decision == "approve":
            conn.execute(
                """
                UPDATE proposals
                SET status = 'approved', updated_at = ?, approved_at = ?, denied_at = NULL,
                    approver = ?, denial_reason = NULL
                WHERE proposal_id = ?
                """,
                (decided_at, decided_at, args.approver, args.proposal_id),
            )
        elif args.decision == "deny":
            conn.execute(
                """
                UPDATE proposals
                SET status = 'denied', updated_at = ?, approved_at = NULL, denied_at = ?,
                    approver = ?, denial_reason = ?
                WHERE proposal_id = ?
                """,
                (decided_at, decided_at, args.approver, args.reason, args.proposal_id),
            )
        elif args.decision == "discuss":
            conn.execute(
                """
                UPDATE proposals
                SET status = 'discussing', updated_at = ?, approved_at = NULL, denied_at = NULL,
                    approver = ?, denial_reason = ?
                WHERE proposal_id = ?
                """,
                (decided_at, args.approver, args.reason, args.proposal_id),
            )
        else:
            conn.execute(
                """
                UPDATE proposals
                SET status = 'needs_changes', updated_at = ?, approved_at = NULL, denied_at = NULL,
                    approver = ?, denial_reason = ?
                WHERE proposal_id = ?
                """,
                (decided_at, args.approver, args.reason, args.proposal_id),
            )

        conn.execute(
            """
            INSERT INTO proposal_decision_audit(
                proposal_id, decided_at, decision, approver, reason,
                previous_status, new_status, source, backup_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.proposal_id,
                decided_at,
                args.decision,
                args.approver,
                args.reason,
                previous_status,
                new_status,
                args.source,
                str(backup_path),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "proposal_id": args.proposal_id,
        "decision": args.decision,
        "status": new_status,
        "approver": args.approver,
        "decided_at": decided_at,
        "imported_from_packet": imported,
        "packet_source": packet_source,
        "backup_path": str(backup_path),
        "mutated_tables": ["proposals", "proposal_decision_audit", "proposal_evidence_links" if imported else None],
        "safety": "ledger_only_no_implementation_runtime_cron_or_kanban_mutation",
    }


def main() -> int:
    args = parse_args()
    try:
        result = capture_decision(args)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    result["mutated_tables"] = [item for item in result["mutated_tables"] if item]
    if args.json:
        print(json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
    else:
        print(f"Captured {result['decision']} for {result['proposal_id']} -> {result['status']}")
        print(f"Backup: {result['backup_path']}")
        if result["packet_source"]:
            print(f"Imported packet: {result['packet_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
