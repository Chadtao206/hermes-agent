#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
INIT_SCRIPT = THIS_DIR / "init_self_improvement_db.py"
APPLY_SCRIPT = THIS_DIR / "apply_approved_proposal.py"


spec = importlib.util.spec_from_file_location("apply_approved_proposal", APPLY_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to import apply_approved_proposal.py for tests")
apply_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_mod)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _run_checked(command: list[str]) -> subprocess.CompletedProcess[str]:
    proc = _run(command)
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(command)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def _run_init(telemetry_root: Path) -> None:
    _run_checked([sys.executable, str(INIT_SCRIPT), "--telemetry-root", str(telemetry_root)])


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _packet(proposal_id: str) -> dict:
    return {
        "proposal_id": proposal_id,
        "title": f"Proposal {proposal_id}",
        "decision_requested": "approve",
        "owner_profile": "engineer",
        "tl_dr": "Synthetic packet for apply helper tests.",
        "proposed_change": "Do the thing safely.",
        "verification_plan": "Run deterministic checks.",
    }


def _seed_approved_row(db: Path, proposal_id: str) -> None:
    conn = sqlite3.connect(db)
    try:
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
            """,
            (
                proposal_id,
                "2026-05-25T00:00:00+00:00",
                "2026-05-25T00:00:00+00:00",
                "readiness_gate_fix",
                f"Proposal {proposal_id}",
                "approved",
                "engineer",
                "high",
                0.9,
                json.dumps({"test": True}, sort_keys=True),
                "approve",
                "Synthetic approved proposal.",
                "Synthetic problem.",
                "Synthetic change.",
                json.dumps({"metric": "task_success_rate"}, sort_keys=True),
                "low",
                "Synthetic risk",
                "Synthetic rollback",
                "Synthetic verification",
                "2026-05-25T01:00:00+00:00",
                None,
                "Chad Tao",
                None,
                None,
                None,
                None,
                "unknown",
                None,
                json.dumps(_packet(proposal_id), sort_keys=False),
            ),
        )
        conn.execute(
            """
            INSERT INTO proposal_decision_audit(
                proposal_id, decided_at, decision, approver, reason,
                previous_status, new_status, source, backup_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                "2026-05-25T01:00:00+00:00",
                "approve",
                "Chad Tao",
                "Looks good",
                "proposed",
                "approved",
                "slack:thread-1",
                "/tmp/pre-apply-backup",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _init_kanban_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT,
                status TEXT,
                created_at INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _query_one(db: Path, query: str, params: tuple = ()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _args(
    *,
    proposal_id: str,
    telemetry_root: Path,
    kanban_db: Path,
    execute: bool,
    operator: str | None = None,
    source: str = "manual",
    reason: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        proposal_id=proposal_id,
        execute=execute,
        operator=operator,
        source=source,
        reason=reason,
        telemetry_root=str(telemetry_root),
        proposal_dir=None,
        backup_dir=None,
        kanban_db=str(kanban_db),
        json=True,
    )


def case_dry_run_generates_artifact_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:test-dry-run"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)

        _write_json(telemetry_root / "proposals" / f"{proposal_id}.json", _packet(proposal_id))
        _seed_approved_row(telemetry_root / "experiments.db", proposal_id)

        payload = apply_mod.apply(
            _args(
                proposal_id=proposal_id,
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                execute=False,
            )
        )
        assert payload["ok"] is True, payload
        assert payload["mode"] == "dry_run", payload
        assert payload["action"] == "plan_generated", payload

        apply_path = Path(payload["apply_artifact_path"])
        assert apply_path.exists(), payload
        assert apply_path.name == f"{proposal_id}.apply.dry-run.json", payload
        apply_md = Path(payload["apply_markdown_path"])
        assert apply_md.exists(), payload
        assert apply_md.name == f"{proposal_id}.apply.dry-run.md", payload

        db = telemetry_root / "experiments.db"
        row = _query_one(db, "SELECT status, applied_at FROM proposals WHERE proposal_id=?", (proposal_id,))
        assert row == ("approved", None), row
        audit_count = _query_one(db, "SELECT COUNT(*) FROM proposal_apply_audit WHERE proposal_id=?", (proposal_id,))[0]
        assert audit_count == 0, audit_count


def case_missing_ledger_row_fails_even_with_packet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:test-missing-ledger"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _write_json(telemetry_root / "proposals" / f"{proposal_id}.json", _packet(proposal_id))

        try:
            apply_mod.apply(
                _args(
                    proposal_id=proposal_id,
                    telemetry_root=telemetry_root,
                    kanban_db=kanban_db,
                    execute=False,
                )
            )
        except ValueError as exc:
            assert "not found in proposal ledger" in str(exc), exc
        else:
            raise AssertionError("expected apply() to fail when proposal ledger row is missing")


def case_execute_requires_decision_audit_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:test-missing-decision-audit"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _write_json(telemetry_root / "proposals" / f"{proposal_id}.json", _packet(proposal_id))
        db = telemetry_root / "experiments.db"
        _seed_approved_row(db, proposal_id)

        conn = sqlite3.connect(db)
        try:
            conn.execute("DELETE FROM proposal_decision_audit WHERE proposal_id = ?", (proposal_id,))
            conn.commit()
        finally:
            conn.close()

        dry_run = apply_mod.apply(
            _args(
                proposal_id=proposal_id,
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                execute=False,
            )
        )
        assert dry_run["ok"] is True, dry_run
        assert dry_run["action"] == "plan_generated", dry_run

        try:
            apply_mod.apply(
                _args(
                    proposal_id=proposal_id,
                    telemetry_root=telemetry_root,
                    kanban_db=kanban_db,
                    execute=True,
                    operator="Chad Tao",
                )
            )
        except ValueError as exc:
            message = str(exc)
            assert "capture_proposal_decision.py" in message, exc
            assert "no proposal_decision_audit row" in message, exc
        else:
            raise AssertionError("expected apply() execute mode to fail without decision audit provenance")

        audit_count = _query_one(
            db,
            "SELECT COUNT(*) FROM proposal_apply_audit WHERE proposal_id=?",
            (proposal_id,),
        )[0]
        assert audit_count == 0, audit_count


def case_execute_is_idempotent_and_records_audit_backup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:test-execute"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _write_json(telemetry_root / "proposals" / f"{proposal_id}.json", _packet(proposal_id))
        _seed_approved_row(telemetry_root / "experiments.db", proposal_id)

        calls: list[str] = []

        def fake_create_kanban_task(*, hermes_home: Path, idempotency_key: str, task_payload: dict[str, str]) -> str:
            calls.append(idempotency_key)
            conn = sqlite3.connect(kanban_db)
            try:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return str(row[0])
                task_id = "t_apply_001"
                conn.execute(
                    "INSERT INTO tasks(id, idempotency_key, status, created_at) VALUES(?, ?, 'ready', 1)",
                    (task_id, idempotency_key),
                )
                conn.commit()
                return task_id
            finally:
                conn.close()

        original = apply_mod.create_kanban_task
        apply_mod.create_kanban_task = fake_create_kanban_task
        try:
            dry_payload = apply_mod.apply(
                _args(
                    proposal_id=proposal_id,
                    telemetry_root=telemetry_root,
                    kanban_db=kanban_db,
                    execute=False,
                )
            )
            assert dry_payload["ok"] is True, dry_payload
            assert dry_payload["action"] == "plan_generated", dry_payload
            dry_artifact_path = Path(dry_payload["apply_artifact_path"])
            assert dry_artifact_path.name.endswith(".apply.dry-run.json"), dry_payload
            dry_artifact_before = dry_artifact_path.read_text(encoding="utf-8")

            first_payload = apply_mod.apply(
                _args(
                    proposal_id=proposal_id,
                    telemetry_root=telemetry_root,
                    kanban_db=kanban_db,
                    execute=True,
                    operator="Chad Tao",
                    source="unit-test",
                )
            )
            assert first_payload["ok"] is True, first_payload
            assert first_payload["action"] == "applied", first_payload
            assert first_payload["kanban_task_id"] == "t_apply_001", first_payload
            assert len(calls) == 1, calls

            idempotency_key = first_payload["idempotency_key"]
            execute_artifact_path = Path(first_payload["apply_artifact_path"])
            assert execute_artifact_path.name == f"{proposal_id}.apply.json", first_payload
            assert execute_artifact_path != dry_artifact_path
            execute_artifact_before = execute_artifact_path.read_text(encoding="utf-8")
            assert dry_artifact_path.read_text(encoding="utf-8") == dry_artifact_before
            assert Path(first_payload["backup_path"]).exists(), first_payload
            assert Path(first_payload["kanban_backup_path"]).exists(), first_payload
            assert Path(first_payload["manifest_path"]).exists(), first_payload

            db = telemetry_root / "experiments.db"
            row = _query_one(db, "SELECT status, applied_at FROM proposals WHERE proposal_id=?", (proposal_id,))
            assert row[0] == "applied", row
            assert row[1], row

            audit = _query_one(
                db,
                "SELECT action, operator, source, idempotency_key, kanban_task_id, backup_path, kanban_backup_path, approver, approval_decided_at, approval_source FROM proposal_apply_audit WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
                (proposal_id,),
            )
            assert audit is not None
            assert audit[0] == "applied", audit
            assert audit[1] == "Chad Tao", audit
            assert audit[2] == "unit-test", audit
            assert audit[3] == idempotency_key, audit
            assert audit[4] == "t_apply_001", audit
            assert Path(audit[5]).exists(), audit
            assert Path(audit[6]).exists(), audit
            assert audit[7] == "Chad Tao", audit
            assert audit[8] == "2026-05-25T01:00:00+00:00", audit
            assert audit[9] == "slack:thread-1", audit

            count = _query_one(
                kanban_db,
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
                (idempotency_key,),
            )[0]
            assert count == 1, count

            second_payload = apply_mod.apply(
                _args(
                    proposal_id=proposal_id,
                    telemetry_root=telemetry_root,
                    kanban_db=kanban_db,
                    execute=True,
                    operator="Chad Tao",
                    source="unit-test",
                )
            )
            assert second_payload["ok"] is True, second_payload
            assert second_payload["action"] == "noop_already_applied", second_payload
            assert second_payload["apply_artifact_path"] == str(execute_artifact_path), second_payload
            assert Path(second_payload["apply_markdown_path"]).name == f"{proposal_id}.apply.md", second_payload
            assert execute_artifact_path.read_text(encoding="utf-8") == execute_artifact_before
            assert dry_artifact_path.read_text(encoding="utf-8") == dry_artifact_before
            assert len(calls) == 1, calls

            audit_count = _query_one(
                db,
                "SELECT COUNT(*) FROM proposal_apply_audit WHERE proposal_id=?",
                (proposal_id,),
            )[0]
            assert audit_count == 1, audit_count

            count_after = _query_one(
                kanban_db,
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
                (idempotency_key,),
            )[0]
            assert count_after == 1, count_after
        finally:
            apply_mod.create_kanban_task = original


def main() -> int:
    case_dry_run_generates_artifact_without_mutation()
    case_missing_ledger_row_fails_even_with_packet()
    case_execute_requires_decision_audit_provenance()
    case_execute_is_idempotent_and_records_audit_backup()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
