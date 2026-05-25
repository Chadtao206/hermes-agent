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
CAPTURE_SCRIPT = THIS_DIR / "capture_proposal_decision.py"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    proc = _run(command)
    if proc.returncode != 0:
        raise AssertionError(f"command failed ({proc.returncode}): {' '.join(command)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def _run_init(telemetry_root: Path) -> None:
    _run_checked([sys.executable, str(INIT_SCRIPT), "--telemetry-root", str(telemetry_root)])


def _packet(proposal_id: str, *, title: str = "Test proposal") -> dict:
    return {
        "proposal_id": proposal_id,
        "title": title,
        "decision_requested": "approve",
        "tl_dr": "Capture approval safely.",
        "evidence": [
            {
                "evidence_type": "unit_test",
                "evidence_ref": "test_capture_proposal_decision",
                "evidence_summary": "synthetic packet for temp-db decision capture",
            }
        ],
        "impact": {"primary_metric": "decision_capture", "expected_direction": "up"},
        "risk": {"level": "low", "notes": "temp test packet"},
        "rollback": "restore pre-write experiments.db backup",
        "verification": "query proposal ledger and decision audit rows",
        "confidence": {"score": 0.7, "band": "medium", "basis": {"test": True}},
        "owner": "ops",
        "approve_deny_discuss": "approve for temp test only",
    }


def _write_packet(root: Path, proposal_id: str) -> Path:
    proposal_dir = root / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    packet_path = proposal_dir / f"{proposal_id}.json"
    packet_path.write_text(json.dumps(_packet(proposal_id), indent=2), encoding="utf-8")
    return packet_path


def _query_one(db: Path, query: str, params: tuple = ()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def case_approve_imports_dry_run_packet_and_audits() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        _run_init(telemetry_root)
        proposal_id = "proposal:test-approve"
        packet_path = _write_packet(telemetry_root, proposal_id)

        proc = _run_checked(
            [
                sys.executable,
                str(CAPTURE_SCRIPT),
                proposal_id,
                "--decision",
                "approve",
                "--approver",
                "Chad Tao",
                "--source",
                "unit-test",
                "--telemetry-root",
                str(telemetry_root),
                "--packet",
                str(packet_path),
                "--json",
            ]
        )
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True, payload
        assert payload["imported_from_packet"] is True, payload
        backup = Path(payload["backup_path"])
        assert backup.exists(), payload
        assert "proposals" in payload["mutated_tables"], payload
        assert "proposal_decision_audit" in payload["mutated_tables"], payload

        db = telemetry_root / "experiments.db"
        row = _query_one(db, "SELECT status, approved_at, denied_at, approver, denial_reason FROM proposals WHERE proposal_id=?", (proposal_id,))
        assert row is not None, "proposal row missing"
        assert row[0] == "approved", row
        assert row[1], row
        assert row[2] is None, row
        assert row[3] == "Chad Tao", row
        assert row[4] is None, row
        audit = _query_one(db, "SELECT decision, approver, previous_status, new_status, source, backup_path FROM proposal_decision_audit WHERE proposal_id=?", (proposal_id,))
        assert audit == ("approve", "Chad Tao", "proposed", "approved", "unit-test", str(backup)), audit
        evidence_count = _query_one(db, "SELECT COUNT(*) FROM proposal_evidence_links WHERE proposal_id=?", (proposal_id,))[0]
        assert evidence_count == 1, evidence_count


def case_deny_requires_reason_and_records_reason() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        _run_init(telemetry_root)
        proposal_id = "proposal:test-deny"
        packet_path = _write_packet(telemetry_root, proposal_id)

        failed = _run(
            [
                sys.executable,
                str(CAPTURE_SCRIPT),
                proposal_id,
                "--decision",
                "deny",
                "--approver",
                "Chad Tao",
                "--telemetry-root",
                str(telemetry_root),
                "--packet",
                str(packet_path),
                "--json",
            ]
        )
        assert failed.returncode != 0, failed.stdout
        assert "--reason is required" in failed.stderr, failed.stderr

        proc = _run_checked(
            [
                sys.executable,
                str(CAPTURE_SCRIPT),
                proposal_id,
                "--decision",
                "deny",
                "--approver",
                "Chad Tao",
                "--reason",
                "not enough evidence",
                "--source",
                "unit-test",
                "--telemetry-root",
                str(telemetry_root),
                "--packet",
                str(packet_path),
                "--json",
            ]
        )
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True, payload
        db = telemetry_root / "experiments.db"
        row = _query_one(db, "SELECT status, approved_at, denied_at, approver, denial_reason FROM proposals WHERE proposal_id=?", (proposal_id,))
        assert row[0] == "denied", row
        assert row[1] is None, row
        assert row[2], row
        assert row[3] == "Chad Tao", row
        assert row[4] == "not enough evidence", row
        audit = _query_one(db, "SELECT decision, reason, new_status FROM proposal_decision_audit WHERE proposal_id=?", (proposal_id,))
        assert audit == ("deny", "not enough evidence", "denied"), audit


def case_discuss_and_needs_changes_require_reason_and_persist_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        _run_init(telemetry_root)

        discuss_id = "proposal:test-discuss"
        discuss_packet = _write_packet(telemetry_root, discuss_id)

        discuss_missing_reason = _run(
            [
                sys.executable,
                str(CAPTURE_SCRIPT),
                discuss_id,
                "--decision",
                "discuss",
                "--approver",
                "Chad Tao",
                "--telemetry-root",
                str(telemetry_root),
                "--packet",
                str(discuss_packet),
                "--json",
            ]
        )
        assert discuss_missing_reason.returncode != 0, discuss_missing_reason.stdout
        assert "--reason is required" in discuss_missing_reason.stderr, discuss_missing_reason.stderr

        discuss = _run_checked(
            [
                sys.executable,
                str(CAPTURE_SCRIPT),
                discuss_id,
                "--decision",
                "discuss",
                "--approver",
                "Chad Tao",
                "--reason",
                "need clarifications before approval",
                "--source",
                "unit-test",
                "--telemetry-root",
                str(telemetry_root),
                "--packet",
                str(discuss_packet),
                "--json",
            ]
        )
        discuss_payload = json.loads(discuss.stdout)
        assert discuss_payload["ok"] is True, discuss_payload
        assert discuss_payload["status"] == "discussing", discuss_payload

        db = telemetry_root / "experiments.db"
        discuss_row = _query_one(db, "SELECT status, approved_at, denied_at, approver, denial_reason FROM proposals WHERE proposal_id=?", (discuss_id,))
        assert discuss_row == ("discussing", None, None, "Chad Tao", "need clarifications before approval"), discuss_row
        discuss_audit = _query_one(db, "SELECT decision, reason, new_status FROM proposal_decision_audit WHERE proposal_id=?", (discuss_id,))
        assert discuss_audit == ("discuss", "need clarifications before approval", "discussing"), discuss_audit

        needs_id = "proposal:test-needs-changes"
        needs_packet = _write_packet(telemetry_root, needs_id)
        needs = _run_checked(
            [
                sys.executable,
                str(CAPTURE_SCRIPT),
                needs_id,
                "--decision",
                "needs_changes",
                "--approver",
                "Chad Tao",
                "--reason",
                "requires stronger rollback evidence",
                "--source",
                "unit-test",
                "--telemetry-root",
                str(telemetry_root),
                "--packet",
                str(needs_packet),
                "--json",
            ]
        )
        needs_payload = json.loads(needs.stdout)
        assert needs_payload["ok"] is True, needs_payload
        assert needs_payload["status"] == "needs_changes", needs_payload

        needs_row = _query_one(db, "SELECT status, approved_at, denied_at, approver, denial_reason FROM proposals WHERE proposal_id=?", (needs_id,))
        assert needs_row == ("needs_changes", None, None, "Chad Tao", "requires stronger rollback evidence"), needs_row
        needs_audit = _query_one(db, "SELECT decision, reason, new_status FROM proposal_decision_audit WHERE proposal_id=?", (needs_id,))
        assert needs_audit == ("needs_changes", "requires stronger rollback evidence", "needs_changes"), needs_audit


def case_digest_entries_include_proposal_id() -> None:
    sys.path.insert(0, str(THIS_DIR))
    import cron_generate_proposals_digest as digest

    value = digest.entries(
        [
            {
                "proposal_id": "proposal:test-id",
                "title": "Readable title",
                "proposal_type": "readiness_gate_fix",
                "owner_profile": "ops",
                "decision_requested": "approve",
            }
        ],
        proposal_type="readiness_gate_fix",
    )
    assert value == ["proposal:test-id (Readable title)"], value


def _run_digest_main(*, before: dict, after: dict, previous: dict | None = None) -> tuple[int, dict]:
    sys.path.insert(0, str(THIS_DIR))
    import cron_generate_proposals_digest as digest

    payload = {
        "overall_verdict": "NOT_COMPLETE",
        "proposal_count": 0,
        "suppressed_count": 0,
        "proposals": [],
        "suppressed": [],
        "evaluated_at": "2026-05-25T00:00:00Z",
    }
    counts_iter = iter([before, after])
    saved: dict = {}
    originals = {
        name: getattr(digest, name)
        for name in ("proposal_table_counts", "run_generator", "load_state", "save_state")
    }
    digest.proposal_table_counts = lambda: next(counts_iter)
    digest.run_generator = lambda: payload
    digest.load_state = lambda: previous or {}
    digest.save_state = lambda data: saved.update(data) or None
    try:
        return digest.main(), saved
    finally:
        for name, fn in originals.items():
            setattr(digest, name, fn)


def case_dry_run_succeeds_when_ledger_prepopulated_unchanged() -> None:
    counts = {"proposals": 7, "proposal_evidence_links": 12}
    rc, saved = _run_digest_main(before=counts, after=counts)
    assert rc == 0, rc
    assert saved.get("fingerprint"), saved


def case_dry_run_fails_when_ledger_mutated() -> None:
    rc, _ = _run_digest_main(
        before={"proposals": 7, "proposal_evidence_links": 12},
        after={"proposals": 8, "proposal_evidence_links": 12},
    )
    assert rc == 1, rc


def main() -> int:
    case_approve_imports_dry_run_packet_and_audits()
    case_deny_requires_reason_and_records_reason()
    case_discuss_and_needs_changes_require_reason_and_persist_status()
    case_digest_entries_include_proposal_id()
    case_dry_run_succeeds_when_ledger_prepopulated_unchanged()
    case_dry_run_fails_when_ledger_mutated()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
