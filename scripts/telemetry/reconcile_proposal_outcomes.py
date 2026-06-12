#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from apply_approved_proposal import (
    backup_database,
    compact_timestamp,
    require_quick_check_ok,
    safe_name,
    utc_now,
)
from common import ensure_initialized, resolve_telemetry_root
from init_self_improvement_db import ensure_directories
from kanban_access import resolve_kanban_access

OUTCOME_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposal_outcome_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    action TEXT NOT NULL,
    operator TEXT NOT NULL,
    source TEXT,
    reason TEXT,
    kanban_task_id TEXT,
    kanban_task_status TEXT,
    kanban_completed_at TEXT,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    previous_outcome TEXT,
    new_outcome TEXT,
    verified_at TEXT,
    backup_path TEXT,
    kanban_backup_path TEXT,
    manifest_path TEXT
)
"""
OUTCOME_AUDIT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_proposal_outcome_audit_proposal_id "
    "ON proposal_outcome_audit(proposal_id)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile applied proposal outcomes against linked kanban task state. "
            "Default mode is dry-run planning only."
        )
    )
    parser.add_argument("--proposal-id", help="Optional single proposal_id to reconcile")
    parser.add_argument("--execute", action="store_true", help="Execute ledger updates (default is dry-run)")
    parser.add_argument("--operator", help="Human/operator identity running execute mode")
    parser.add_argument("--source", default="manual", help="Provenance source, e.g. slack thread id")
    parser.add_argument("--reason", help="Required execute rationale for reconciliation run")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--backup-dir", help="Directory for execute-mode DB backups")
    parser.add_argument("--kanban-db", help="Override kanban DB path (default: $HERMES_HOME/kanban.db)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser.parse_args()


def ensure_outcome_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(OUTCOME_AUDIT_SCHEMA)
    conn.execute(OUTCOME_AUDIT_INDEX)


def proposal_rows(conn: sqlite3.Connection, proposal_id: str | None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if proposal_id:
        row = conn.execute(
            """
            SELECT proposal_id, status, outcome, verified_at, scored_at, updated_at
            FROM proposals
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        return [row] if row else []

    rows = conn.execute(
        """
        SELECT proposal_id, status, outcome, verified_at, scored_at, updated_at
        FROM proposals
        WHERE status = 'applied'
        ORDER BY proposal_id ASC
        """
    ).fetchall()
    return list(rows)


def latest_apply_link(conn: sqlite3.Connection, proposal_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT id, proposal_id, applied_at, action, idempotency_key, kanban_task_id
        FROM proposal_apply_audit
        WHERE proposal_id = ?
          AND action IN ('applied', 'noop_update_guard', 'noop_already_applied')
        ORDER BY id DESC
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()


def _status_equal(left: str | None, right: str | None) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def derive_transition(
    *,
    proposal: sqlite3.Row,
    apply_link: sqlite3.Row | None,
    task: dict[str, Any] | None,
    resolved_kanban_task_id: str | None = None,
) -> dict[str, Any]:
    proposal_id = str(proposal["proposal_id"])
    previous_status = str(proposal["status"] or "")
    previous_outcome = proposal["outcome"]
    previous_verified_at = proposal["verified_at"]

    if apply_link is None:
        return {
            "proposal_id": proposal_id,
            "action": "noop_no_apply_link",
            "terminal": False,
            "stale": False,
            "kanban_task_id": None,
            "kanban_task_status": None,
            "kanban_completed_at": None,
            "previous_status": previous_status,
            "previous_outcome": previous_outcome,
            "new_status": previous_status,
            "new_outcome": previous_outcome,
            "verified_at": previous_verified_at,
            "execute_update": False,
            "reason": "No proposal_apply_audit linkage found for proposal.",
        }

    kanban_task_id = (
        str(resolved_kanban_task_id).strip()
        if resolved_kanban_task_id
        else str(apply_link["kanban_task_id"] or "").strip()
    )
    if not kanban_task_id:
        return {
            "proposal_id": proposal_id,
            "action": "noop_no_kanban_link",
            "terminal": False,
            "stale": False,
            "kanban_task_id": None,
            "kanban_task_status": None,
            "kanban_completed_at": None,
            "previous_status": previous_status,
            "previous_outcome": previous_outcome,
            "new_status": previous_status,
            "new_outcome": previous_outcome,
            "verified_at": previous_verified_at,
            "execute_update": False,
            "reason": "Apply audit row exists but has no kanban_task_id.",
        }

    if task is None:
        new_status = "stale"
        new_outcome = "needs_attention"
        execute_update = not (
            _status_equal(previous_status, new_status)
            and _status_equal(str(previous_outcome or ""), new_outcome)
        )
        return {
            "proposal_id": proposal_id,
            "action": "missing_task",
            "terminal": True,
            "stale": execute_update,
            "kanban_task_id": kanban_task_id,
            "kanban_task_status": None,
            "kanban_completed_at": None,
            "previous_status": previous_status,
            "previous_outcome": previous_outcome,
            "new_status": new_status,
            "new_outcome": new_outcome,
            "verified_at": previous_verified_at,
            "execute_update": execute_update,
            "reason": "Linked kanban task was not found.",
        }

    kanban_status = str(task["status"] or "")
    completed_at = task["completed_at"]
    failures = int(task["consecutive_failures"] or 0)

    if kanban_status == "done":
        new_status = "verified"
        new_outcome = "success"
        new_verified_at = str(completed_at or utc_now())
        execute_update = not (
            _status_equal(previous_status, new_status)
            and _status_equal(str(previous_outcome or ""), new_outcome)
            and bool(previous_verified_at)
        )
        return {
            "proposal_id": proposal_id,
            "action": "transitioned_verified" if execute_update else "noop_no_change",
            "terminal": True,
            "stale": execute_update,
            "kanban_task_id": kanban_task_id,
            "kanban_task_status": kanban_status,
            "kanban_completed_at": completed_at,
            "previous_status": previous_status,
            "previous_outcome": previous_outcome,
            "new_status": new_status,
            "new_outcome": new_outcome,
            "verified_at": new_verified_at,
            "execute_update": execute_update,
            "reason": "Linked kanban task is done.",
        }

    if kanban_status == "failed":
        new_status = "failed"
        new_outcome = "failed"
        execute_update = not (
            _status_equal(previous_status, new_status)
            and _status_equal(str(previous_outcome or ""), new_outcome)
        )
        return {
            "proposal_id": proposal_id,
            "action": "transitioned_failed" if execute_update else "noop_no_change",
            "terminal": True,
            "stale": execute_update,
            "kanban_task_id": kanban_task_id,
            "kanban_task_status": kanban_status,
            "kanban_completed_at": completed_at,
            "previous_status": previous_status,
            "previous_outcome": previous_outcome,
            "new_status": new_status,
            "new_outcome": new_outcome,
            "verified_at": previous_verified_at,
            "execute_update": execute_update,
            "reason": "Linked kanban task is failed.",
        }

    if kanban_status == "blocked" and failures > 0:
        new_status = "needs_review"
        new_outcome = "needs_review"
        execute_update = not (
            _status_equal(previous_status, new_status)
            and _status_equal(str(previous_outcome or ""), new_outcome)
        )
        return {
            "proposal_id": proposal_id,
            "action": "transitioned_needs_review" if execute_update else "noop_no_change",
            "terminal": True,
            "stale": execute_update,
            "kanban_task_id": kanban_task_id,
            "kanban_task_status": kanban_status,
            "kanban_completed_at": completed_at,
            "previous_status": previous_status,
            "previous_outcome": previous_outcome,
            "new_status": new_status,
            "new_outcome": new_outcome,
            "verified_at": previous_verified_at,
            "execute_update": execute_update,
            "reason": "Linked kanban task is blocked with consecutive_failures > 0.",
        }

    return {
        "proposal_id": proposal_id,
        "action": "noop_non_terminal",
        "terminal": False,
        "stale": False,
        "kanban_task_id": kanban_task_id,
        "kanban_task_status": kanban_status,
        "kanban_completed_at": completed_at,
        "previous_status": previous_status,
        "previous_outcome": previous_outcome,
        "new_status": previous_status,
        "new_outcome": previous_outcome,
        "verified_at": previous_verified_at,
        "execute_update": False,
        "reason": "Linked kanban task is not in terminal reconciliable status.",
    }


def capture_execute_backups(
    *,
    backup_root: Path,
    proposal_scope: str,
    experiments_db: Path,
    kanban_access: Any,
    operator: str,
    source: str,
    reason: str,
    observations: list[dict[str, Any]],
) -> tuple[Path, Path | None, Path]:
    base_name = f"{compact_timestamp()}-{safe_name(proposal_scope)}"
    run_dir = backup_root / base_name
    if run_dir.exists():
        suffix = 1
        while True:
            candidate = backup_root / f"{base_name}-{suffix}"
            if not candidate.exists():
                run_dir = candidate
                break
            suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)

    experiments_backup = run_dir / "experiments.db.bak"
    backup_database(experiments_db, experiments_backup)
    # Store-backed boards (postgres) are not file-backupable from here; the
    # audit rows record the per-task transitions needed for manual rollback.
    kanban_backup = kanban_access.backup_to(run_dir)

    manifest = {
        "started_at": utc_now(),
        "proposal_scope": proposal_scope,
        "operator": operator,
        "source": source,
        "reason": reason,
        "kanban_access": kanban_access.describe(),
        "db_paths": {
            "experiments_db": str(experiments_db),
            "kanban_db": kanban_access.describe()["kanban_db"],
        },
        "backup_paths": {
            "experiments_db": str(experiments_backup),
            "kanban_db": str(kanban_backup) if kanban_backup else None,
        },
        "observations": observations,
        "updated_proposals": [],
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return experiments_backup, kanban_backup, manifest_path


def update_manifest(manifest_path: Path, updated_proposals: list[str]) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["updated_proposals"] = sorted(updated_proposals)
    payload["completed_at"] = utc_now()
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    hermes_home = Path(os.environ.get("HERMES_HOME") or telemetry_root.parent).expanduser().resolve()
    experiments_db = telemetry_root / "experiments.db"
    kanban = resolve_kanban_access(args.kanban_db, hermes_home=hermes_home)
    backup_root = (
        Path(args.backup_dir).expanduser().resolve()
        if args.backup_dir
        else telemetry_root / "backups" / "proposal_outcome_reconcile"
    )

    ensure_directories(telemetry_root)
    ensure_initialized(telemetry_root)

    conn = sqlite3.connect(experiments_db)
    conn.row_factory = sqlite3.Row
    try:
        proposals = proposal_rows(conn, args.proposal_id)
        if args.proposal_id and not proposals:
            raise ValueError(f"proposal_id {args.proposal_id!r} was not found in proposals ledger")

        observations: list[dict[str, Any]] = []
        eligible: list[dict[str, Any]] = []
        for proposal in proposals:
            proposal_id = str(proposal["proposal_id"])
            link = latest_apply_link(conn, proposal_id)
            link_task_id = str(link["kanban_task_id"] or "").strip() if link else ""
            link_idempotency_key = str(link["idempotency_key"] or "").strip() if link else ""

            task = kanban.task_state(link_task_id) if link_task_id else None
            resolved_task_id: str | None = link_task_id or None
            link_source: str | None = "apply_audit_id" if task is not None else None

            if task is None and link_idempotency_key:
                fallback_task = kanban.task_by_idempotency_key(link_idempotency_key)
                if fallback_task is not None:
                    task = fallback_task
                    resolved_task_id = str(fallback_task["id"])
                    link_source = (
                        "idempotency_key_corroborated"
                        if link_task_id
                        else "idempotency_key_fallback"
                    )

            transition = derive_transition(
                proposal=proposal,
                apply_link=link,
                task=task,
                resolved_kanban_task_id=resolved_task_id,
            )
            observation = {
                "proposal_id": proposal_id,
                "proposal": {
                    "status": proposal["status"],
                    "outcome": proposal["outcome"],
                    "verified_at": proposal["verified_at"],
                    "scored_at": proposal["scored_at"],
                    "updated_at": proposal["updated_at"],
                },
                "apply_link": {
                    "kanban_task_id": link_task_id or None,
                    "idempotency_key": link_idempotency_key or None,
                    "action": str(link["action"] or "") if link else None,
                    "applied_at": str(link["applied_at"] or "") if link else None,
                }
                if link
                else None,
                "kanban": {
                    "status": str(task["status"] or "") if task else None,
                    "completed_at": task["completed_at"] if task else None,
                    "consecutive_failures": int(task["consecutive_failures"] or 0) if task else None,
                    "resolved_task_id": resolved_task_id,
                    "link_source": link_source,
                }
                if link
                else None,
                "transition": transition,
            }
            observations.append(observation)
            if transition["execute_update"]:
                eligible.append(observation)
    finally:
        conn.close()

    if not args.execute:
        return {
            "ok": True,
            "mode": "dry_run",
            "proposal_scope": args.proposal_id or "all-applied",
            "observed": len(observations),
            "eligible_updates": len(eligible),
            "updated": 0,
            "observations": observations,
        }

    operator = (args.operator or "").strip()
    source = (args.source or "").strip()
    reason = (args.reason or "").strip()
    if not operator:
        raise ValueError("--operator is required with --execute")
    if not source:
        raise ValueError("--source is required with --execute")
    if not reason:
        raise ValueError("--reason is required with --execute")

    require_quick_check_ok(experiments_db, stage="preflight", label="experiments.db")
    kanban.verify_health(stage="preflight")

    proposal_scope = args.proposal_id or "all-applied"
    experiments_backup, kanban_backup, manifest_path = capture_execute_backups(
        backup_root=backup_root,
        proposal_scope=proposal_scope,
        experiments_db=experiments_db,
        kanban_access=kanban,
        operator=operator,
        source=source,
        reason=reason,
        observations=observations,
    )

    updated_proposals: list[str] = []
    audits_written = 0

    conn = sqlite3.connect(experiments_db)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_outcome_audit_schema(conn)

        for observation in eligible:
            transition = observation["transition"]
            proposal_id = str(observation["proposal_id"])

            current = conn.execute(
                "SELECT status, outcome, verified_at FROM proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if current is None:
                continue

            previous_status = str(current["status"] or "")
            previous_outcome = current["outcome"]
            previous_verified_at = current["verified_at"]

            guarded = conn.execute(
                """
                UPDATE proposals
                SET status = ?, outcome = ?, verified_at = ?, scored_at = ?, updated_at = ?
                WHERE proposal_id = ?
                  AND status = ?
                  AND COALESCE(outcome, '') = COALESCE(?, '')
                """,
                (
                    transition["new_status"],
                    transition["new_outcome"],
                    transition["verified_at"],
                    utc_now(),
                    utc_now(),
                    proposal_id,
                    previous_status,
                    previous_outcome,
                ),
            )

            if guarded.rowcount == 0:
                continue

            updated_proposals.append(proposal_id)
            conn.execute(
                """
                INSERT INTO proposal_outcome_audit(
                    proposal_id, observed_at, action, operator, source, reason,
                    kanban_task_id, kanban_task_status, kanban_completed_at,
                    previous_status, new_status, previous_outcome, new_outcome,
                    verified_at, backup_path, kanban_backup_path, manifest_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    utc_now(),
                    transition["action"],
                    operator,
                    source,
                    reason,
                    transition["kanban_task_id"],
                    transition["kanban_task_status"],
                    transition["kanban_completed_at"],
                    previous_status,
                    transition["new_status"],
                    previous_outcome,
                    transition["new_outcome"],
                    transition["verified_at"] or previous_verified_at,
                    str(experiments_backup),
                    str(kanban_backup) if kanban_backup else None,
                    str(manifest_path),
                ),
            )
            audits_written += 1

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    require_quick_check_ok(experiments_db, stage="post_reconcile", label="experiments.db")
    kanban.verify_health(stage="post_reconcile")
    update_manifest(manifest_path, updated_proposals)

    return {
        "ok": True,
        "mode": "execute",
        "proposal_scope": proposal_scope,
        "observed": len(observations),
        "eligible_updates": len(eligible),
        "updated": len(updated_proposals),
        "audits_written": audits_written,
        "backup_path": str(experiments_backup),
        "kanban_backup_path": str(kanban_backup) if kanban_backup else None,
        "manifest_path": str(manifest_path),
        "updated_proposals": sorted(updated_proposals),
        "observations": observations,
    }


def main() -> int:
    args = parse_args()
    try:
        result = reconcile(args)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Proposal outcome reconciliation")
        print(f"Mode: {result['mode']}")
        print(f"Observed proposals: {result['observed']}")
        print(f"Eligible updates: {result['eligible_updates']}")
        print(f"Updated proposals: {result.get('updated', 0)}")
        if result.get("updated_proposals"):
            print("Updated proposal IDs: " + ", ".join(result["updated_proposals"]))
        if result.get("backup_path"):
            print(f"experiments.db backup: {result['backup_path']}")
        if result.get("kanban_backup_path"):
            print(f"kanban.db backup: {result['kanban_backup_path']}")
        if result.get("manifest_path"):
            print(f"manifest: {result['manifest_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
