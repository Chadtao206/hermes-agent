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
RECONCILE_SCRIPT = THIS_DIR / "reconcile_proposal_outcomes.py"

spec = importlib.util.spec_from_file_location("reconcile_proposal_outcomes", RECONCILE_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to import reconcile_proposal_outcomes.py for tests")
reconcile_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile_mod)


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
                created_at INTEGER,
                completed_at TEXT,
                consecutive_failures INTEGER,
                result TEXT
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


def _seed_applied_row(db: Path, proposal_id: str, kanban_task_id: str | None) -> None:
    packet = {
        "proposal_id": proposal_id,
        "title": f"Proposal {proposal_id}",
        "decision_requested": "approve",
        "owner_profile": "engineer",
        "tl_dr": "Synthetic packet for reconcile tests.",
        "proposed_change": "Apply deterministic proposal transition.",
        "verification_plan": "Run reconcile tests.",
    }
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
                "applied",
                "engineer",
                "high",
                0.9,
                json.dumps({"test": True}, sort_keys=True),
                "approve",
                "Synthetic applied proposal.",
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
                "2026-05-25T02:00:00+00:00",
                None,
                None,
                None,
                None,
                json.dumps(packet, sort_keys=False),
            ),
        )
        conn.execute(
            """
            INSERT INTO proposal_apply_audit(
                proposal_id, applied_at, action, operator, approver,
                approval_decided_at, approval_source, reason,
                previous_status, new_status, source, idempotency_key,
                backup_path, kanban_backup_path, apply_artifact_path,
                kanban_task_id, manifest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                "2026-05-25T02:00:00+00:00",
                "applied",
                "Chad Tao",
                "Chad Tao",
                "2026-05-25T01:00:00+00:00",
                "slack:thread-1",
                "Approved and applied",
                "approved",
                "applied",
                "unit-test",
                f"proposal-apply:{proposal_id}",
                "/tmp/experiments.bak",
                "/tmp/kanban.bak",
                f"/tmp/{proposal_id}.apply.json",
                kanban_task_id,
                f"/tmp/{proposal_id}.manifest.json",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_task(kanban_db: Path, task_id: str, status: str, *, completed_at: str | None, consecutive_failures: int) -> None:
    conn = sqlite3.connect(kanban_db)
    try:
        conn.execute(
            "INSERT INTO tasks(id, idempotency_key, status, created_at, completed_at, consecutive_failures, result) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (task_id, f"proposal-apply:seed:{task_id}", status, completed_at, consecutive_failures, status),
        )
        conn.commit()
    finally:
        conn.close()


def _args(
    *,
    telemetry_root: Path,
    kanban_db: Path,
    proposal_id: str | None,
    execute: bool,
    operator: str | None = None,
    source: str = "unit-test",
    reason: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        proposal_id=proposal_id,
        execute=execute,
        operator=operator,
        source=source,
        reason=reason,
        telemetry_root=str(telemetry_root),
        backup_dir=None,
        kanban_db=str(kanban_db),
        json=True,
    )


def case_dry_run_has_no_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-dry-run"
        task_id = "t_reconcile_dry_run"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)
        _insert_task(kanban_db, task_id, "done", completed_at="2026-05-25T03:00:00+00:00", consecutive_failures=0)

        payload = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=False,
            )
        )
        assert payload["ok"] is True, payload
        assert payload["mode"] == "dry_run", payload
        assert payload["eligible_updates"] == 1, payload

        row = _query_one(
            telemetry_root / "experiments.db",
            "SELECT status, outcome, verified_at FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        )
        assert row == ("applied", None, None), row
        audit_count = _query_one(
            telemetry_root / "experiments.db",
            "SELECT COUNT(*) FROM proposal_outcome_audit WHERE proposal_id=?",
            (proposal_id,),
        )[0]
        assert audit_count == 0, audit_count


def case_execute_done_sets_verified_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-done"
        task_id = "t_reconcile_done"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)
        _insert_task(kanban_db, task_id, "done", completed_at="2026-05-25T03:00:00+00:00", consecutive_failures=0)

        payload = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=True,
                operator="Chad Tao",
                reason="Close outcome loop",
            )
        )
        assert payload["ok"] is True, payload
        assert payload["updated"] == 1, payload
        assert Path(payload["backup_path"]).exists(), payload
        assert Path(payload["kanban_backup_path"]).exists(), payload
        assert Path(payload["manifest_path"]).exists(), payload

        row = _query_one(
            telemetry_root / "experiments.db",
            "SELECT status, outcome, verified_at, scored_at FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        )
        assert row[0] == "verified", row
        assert row[1] == "success", row
        assert row[2], row
        assert row[3], row

        audit = _query_one(
            telemetry_root / "experiments.db",
            "SELECT action, new_status, new_outcome FROM proposal_outcome_audit WHERE proposal_id=?",
            (proposal_id,),
        )
        assert audit == ("transitioned_verified", "verified", "success"), audit


def case_execute_blocked_sets_needs_review() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-blocked"
        task_id = "t_reconcile_blocked"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)
        _insert_task(kanban_db, task_id, "blocked", completed_at="2026-05-25T03:00:00+00:00", consecutive_failures=2)

        payload = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=True,
                operator="Chad Tao",
                reason="Blocked reconciliation",
            )
        )
        assert payload["updated"] == 1, payload

        row = _query_one(
            telemetry_root / "experiments.db",
            "SELECT status, outcome FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        )
        assert row == ("needs_review", "needs_review"), row


def case_execute_non_terminal_is_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-non-terminal"
        task_id = "t_reconcile_non_terminal"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)
        _insert_task(kanban_db, task_id, "ready", completed_at=None, consecutive_failures=0)

        payload = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=True,
                operator="Chad Tao",
                reason="Non-terminal should no-op",
            )
        )
        assert payload["updated"] == 0, payload

        row = _query_one(
            telemetry_root / "experiments.db",
            "SELECT status, outcome FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        )
        assert row == ("applied", None), row
        audit_count = _query_one(
            telemetry_root / "experiments.db",
            "SELECT COUNT(*) FROM proposal_outcome_audit WHERE proposal_id=?",
            (proposal_id,),
        )[0]
        assert audit_count == 0, audit_count


def case_execute_missing_task_sets_stale_attention() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-missing-task"
        task_id = "t_reconcile_missing"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)

        payload = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=True,
                operator="Chad Tao",
                reason="Missing task should flag stale",
            )
        )
        assert payload["updated"] == 1, payload

        row = _query_one(
            telemetry_root / "experiments.db",
            "SELECT status, outcome FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        )
        assert row == ("stale", "needs_attention"), row


def case_quick_check_failure_aborts_execute() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-quick-check"
        task_id = "t_reconcile_quick_check"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)
        _insert_task(kanban_db, task_id, "done", completed_at="2026-05-25T03:00:00+00:00", consecutive_failures=0)

        original = getattr(reconcile_mod, "require_quick_check_ok")

        def fake_require_quick_check_ok(db_path: Path, *, stage: str, label: str) -> None:
            if stage == "preflight":
                raise ValueError(f"{label} quick_check failed at {stage}: synthetic failure")
            return original(db_path, stage=stage, label=label)

        setattr(reconcile_mod, "require_quick_check_ok", fake_require_quick_check_ok)
        try:
            try:
                reconcile_mod.reconcile(
                    _args(
                        telemetry_root=telemetry_root,
                        kanban_db=kanban_db,
                        proposal_id=proposal_id,
                        execute=True,
                        operator="Chad Tao",
                        reason="Expect quick_check abort",
                    )
                )
            except ValueError as exc:
                assert "quick_check failed" in str(exc), exc
            else:
                raise AssertionError("expected execute reconcile to fail on preflight quick_check")
        finally:
            setattr(reconcile_mod, "require_quick_check_ok", original)

        row = _query_one(
            telemetry_root / "experiments.db",
            "SELECT status, outcome FROM proposals WHERE proposal_id=?",
            (proposal_id,),
        )
        assert row == ("applied", None), row
        audit_count = _query_one(
            telemetry_root / "experiments.db",
            "SELECT COUNT(*) FROM proposal_outcome_audit WHERE proposal_id=?",
            (proposal_id,),
        )[0]
        assert audit_count == 0, audit_count


def case_execute_is_idempotent_no_duplicate_audit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        telemetry_root = root / "telemetry"
        kanban_db = root / "kanban.db"
        proposal_id = "proposal:reconcile-idempotent"
        task_id = "t_reconcile_idempotent"

        _run_init(telemetry_root)
        _init_kanban_db(kanban_db)
        _seed_applied_row(telemetry_root / "experiments.db", proposal_id, task_id)
        _insert_task(kanban_db, task_id, "done", completed_at="2026-05-25T03:00:00+00:00", consecutive_failures=0)

        first = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=True,
                operator="Chad Tao",
                reason="First reconcile",
            )
        )
        assert first["updated"] == 1, first

        second = reconcile_mod.reconcile(
            _args(
                telemetry_root=telemetry_root,
                kanban_db=kanban_db,
                proposal_id=proposal_id,
                execute=True,
                operator="Chad Tao",
                reason="Second reconcile should no-op",
            )
        )
        assert second["updated"] == 0, second

        audit_count = _query_one(
            telemetry_root / "experiments.db",
            "SELECT COUNT(*) FROM proposal_outcome_audit WHERE proposal_id=?",
            (proposal_id,),
        )[0]
        assert audit_count == 1, audit_count


def main() -> int:
    case_dry_run_has_no_mutation()
    case_execute_done_sets_verified_success()
    case_execute_blocked_sets_needs_review()
    case_execute_non_terminal_is_noop()
    case_execute_missing_task_sets_stale_attention()
    case_quick_check_failure_aborts_execute()
    case_execute_is_idempotent_no_duplicate_audit()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
