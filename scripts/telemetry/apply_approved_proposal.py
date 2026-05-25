#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import ensure_initialized, resolve_telemetry_root
from init_self_improvement_db import ensure_directories

APPLY_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposal_apply_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    action TEXT NOT NULL,
    operator TEXT NOT NULL,
    approver TEXT,
    approval_decided_at TEXT,
    approval_source TEXT,
    reason TEXT,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    source TEXT,
    idempotency_key TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    kanban_backup_path TEXT,
    apply_artifact_path TEXT,
    kanban_task_id TEXT,
    manifest_path TEXT
)
"""
APPLY_AUDIT_INDEX = "CREATE INDEX IF NOT EXISTS idx_proposal_apply_audit_proposal_id ON proposal_apply_audit(proposal_id)"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compact_timestamp() -> str:
    return utc_now().replace(":", "").replace("+00:00", "Z")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)[:120]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply exactly one approved self-improvement proposal. "
            "Default mode is dry-run plan generation only; --execute creates one idempotent kanban root task "
            "and updates proposal lifecycle/audit state."
        )
    )
    parser.add_argument("proposal_id", help="Exact proposal_id to plan/apply")
    parser.add_argument("--execute", action="store_true", help="Execute live apply (default is dry-run artifact only)")
    parser.add_argument("--operator", help="Human/operator identity running the apply command")
    parser.add_argument("--source", default="manual", help="Apply source/provenance, e.g. slack thread id")
    parser.add_argument("--reason", help="Optional operator note for apply audit")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--proposal-dir", help="Directory containing proposal packet files")
    parser.add_argument("--backup-dir", help="Directory for execute-mode DB backups")
    parser.add_argument("--kanban-db", help="Override kanban DB path (default: $HERMES_HOME/kanban.db)")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def find_packet(proposal_id: str, proposal_dir: Path) -> tuple[Path, dict[str, Any]]:
    candidates = [
        proposal_dir / f"{proposal_id}.row.json",
        proposal_dir / f"{proposal_id}.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        payload = load_json(path)
        if payload.get("proposal_id") != proposal_id:
            raise ValueError(f"Packet {path} has proposal_id={payload.get('proposal_id')!r}, expected {proposal_id!r}")
        return path, payload
    raise ValueError(f"No matching packet artifacts found for {proposal_id!r} under {proposal_dir}")


def require_quick_check_ok(db_path: Path, *, stage: str, label: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    finally:
        conn.close()
    if rows != ["ok"]:
        detail = "; ".join(rows) if rows else "no result"
        raise ValueError(f"{label} quick_check failed at {stage}: {detail}")


def backup_database(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def ensure_apply_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(APPLY_AUDIT_SCHEMA)
    conn.execute(APPLY_AUDIT_INDEX)


def proposal_row(conn: sqlite3.Connection, proposal_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT proposal_id, title, status, owner_profile, tl_dr, proposed_change,
               verification_plan, approved_at, denied_at, approver, denial_reason,
               applied_at, updated_at
        FROM proposals
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()


def latest_decision_audit(conn: sqlite3.Connection, proposal_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT decided_at, decision, approver, reason, previous_status, new_status, source
        FROM proposal_decision_audit
        WHERE proposal_id = ?
        ORDER BY decided_at DESC, id DESC
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()


def build_task_payload(row: sqlite3.Row, packet: dict[str, Any], proposal_id: str, apply_artifact: Path) -> dict[str, str]:
    owner = str(row["owner_profile"] or packet.get("owner_profile") or packet.get("owner") or "engineer")
    title = f"proposal apply: {proposal_id} — {row['title'] or packet.get('title') or proposal_id}"
    body = "\n".join(
        [
            f"Proposal apply pilot root task for {proposal_id}.",
            "",
            f"Source proposal artifact: {apply_artifact}",
            f"Owner profile: {owner}",
            "",
            "TL;DR:",
            str(row["tl_dr"] or packet.get("tl_dr") or ""),
            "",
            "Proposed change:",
            str(row["proposed_change"] or packet.get("proposed_change") or ""),
            "",
            "Verification plan:",
            str(row["verification_plan"] or packet.get("verification_plan") or packet.get("verification") or ""),
            "",
            "Safety constraints:",
            "- This card is the only live Phase 3A apply side effect.",
            "- No automatic source/runtime/cron mutation from proposal generation.",
            "- Keep /proposals dashboard read-only.",
        ]
    ).strip()
    return {"title": title, "body": body, "assignee": owner}


def write_apply_artifacts(
    *,
    proposal_id: str,
    proposal_dir: Path,
    packet_path: Path,
    row: sqlite3.Row,
    decision: sqlite3.Row | None,
    mode: str,
    idempotency_key: str,
    task_payload: dict[str, str],
) -> tuple[Path, Path]:
    proposal_dir.mkdir(parents=True, exist_ok=True)
    apply_json_path = proposal_dir / f"{proposal_id}.apply.json"
    apply_md_path = proposal_dir / f"{proposal_id}.apply.md"

    payload = {
        "proposal_id": proposal_id,
        "mode": mode,
        "created_at": utc_now(),
        "source_packet": str(packet_path),
        "title": row["title"],
        "owner_profile": row["owner_profile"],
        "status": row["status"],
        "approved_at": row["approved_at"],
        "approver": row["approver"],
        "approval_decision": {
            "decision": decision["decision"] if decision else None,
            "decided_at": decision["decided_at"] if decision else None,
            "source": decision["source"] if decision else None,
            "reason": decision["reason"] if decision else None,
        },
        "apply_target": {
            "kind": "kanban_root_task",
            "idempotency_key": idempotency_key,
            "title": task_payload["title"],
            "assignee": task_payload["assignee"],
            "body_preview": task_payload["body"][:7000],
        },
        "rollback_note": "Restore experiments.db and kanban.db from execute-mode backups if rollback is required.",
        "verification_checklist": [
            "Confirm idempotency key proposal-apply:<proposal_id> has at most one root task.",
            "Confirm proposal status transition approved -> applied only after task creation.",
            "Confirm experiments.db and kanban.db quick_check are both ok after apply.",
            "Confirm /api/control-center/proposals reflects applied/task-link metadata in read-only mode.",
        ],
    }

    apply_json_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    markdown = "\n".join(
        [
            f"# Apply plan: {proposal_id}",
            "",
            f"- Mode: {mode}",
            f"- Proposal: {row['title']}",
            f"- Status: {row['status']}",
            f"- Approved at: {row['approved_at'] or 'n/a'}",
            f"- Approver: {row['approver'] or 'n/a'}",
            f"- Source packet: {packet_path}",
            f"- Idempotency key: {idempotency_key}",
            f"- Target assignee: {task_payload['assignee']}",
            "",
            "## Safety",
            "- Manual invocation only",
            "- One proposal per run",
            "- No dashboard write controls",
            "- No generator/digest auto-apply",
            "",
            "## Root task preview",
            f"- Title: {task_payload['title']}",
            "",
            "```",
            task_payload["body"],
            "```",
        ]
    )
    apply_md_path.write_text(markdown, encoding="utf-8")
    return apply_json_path, apply_md_path


def existing_task_for_key(kanban_db: Path, idempotency_key: str) -> str | None:
    if not kanban_db.exists():
        return None
    conn = sqlite3.connect(kanban_db)
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return str(row[0])


def create_kanban_task(
    *,
    hermes_home: Path,
    idempotency_key: str,
    task_payload: dict[str, str],
) -> str:
    cmd = [
        "hermes",
        "kanban",
        "create",
        task_payload["title"],
        "--body",
        task_payload["body"],
        "--assignee",
        task_payload["assignee"],
        "--idempotency-key",
        idempotency_key,
        "--json",
    ]
    env = os.environ.copy()
    env.setdefault("HERMES_HOME", str(hermes_home))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"kanban create failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip() or 'no output'}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kanban create returned non-JSON output: {proc.stdout!r}") from exc

    task_id = payload.get("id")
    if not task_id:
        raise RuntimeError(f"kanban create JSON missing task id: {payload}")
    return str(task_id)


def capture_execute_backups(
    *,
    backup_root: Path,
    proposal_id: str,
    experiments_db: Path,
    kanban_db: Path,
    operator_source: str,
    approved_row_before: dict[str, Any],
    packet_path: Path,
    idempotency_key: str,
) -> tuple[Path, Path, Path]:
    run_dir = backup_root / f"{compact_timestamp()}-{safe_name(proposal_id)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    experiments_backup = run_dir / "experiments.db.bak"
    kanban_backup = run_dir / "kanban.db.bak"
    backup_database(experiments_db, experiments_backup)
    backup_database(kanban_db, kanban_backup)

    manifest = {
        "proposal_id": proposal_id,
        "started_at": utc_now(),
        "db_paths": {
            "experiments_db": str(experiments_db),
            "kanban_db": str(kanban_db),
        },
        "backup_paths": {
            "experiments_db": str(experiments_backup),
            "kanban_db": str(kanban_backup),
        },
        "approved_row_before": approved_row_before,
        "packet_paths": [str(packet_path)],
        "planned_idempotency_keys": [idempotency_key],
        "operator_source": operator_source,
        "created_task_ids": [],
        "preflight_quick_check": {
            "experiments_db": "ok",
            "kanban_db": "ok",
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8")
    return experiments_backup, kanban_backup, manifest_path


def update_manifest(manifest_path: Path, *, task_id: str | None) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if task_id:
        payload["created_task_ids"] = [task_id]
    payload["completed_at"] = utc_now()
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def apply(args: argparse.Namespace) -> dict[str, Any]:
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    proposal_dir = Path(args.proposal_dir).expanduser().resolve() if args.proposal_dir else telemetry_root / "proposals"
    hermes_home = Path(os.environ.get("HERMES_HOME") or telemetry_root.parent).expanduser().resolve()
    experiments_db = telemetry_root / "experiments.db"
    kanban_db = Path(args.kanban_db).expanduser().resolve() if args.kanban_db else hermes_home / "kanban.db"
    backup_root = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else telemetry_root / "backups" / "proposal_applies"

    ensure_directories(telemetry_root)
    ensure_initialized(telemetry_root)

    require_quick_check_ok(experiments_db, stage="preflight", label="experiments.db")
    require_quick_check_ok(kanban_db, stage="preflight", label="kanban.db")

    packet_path, packet = find_packet(args.proposal_id, proposal_dir)

    conn = sqlite3.connect(experiments_db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_apply_audit_schema(conn)
        row = proposal_row(conn, args.proposal_id)
        if row is None:
            raise ValueError(
                f"proposal_id {args.proposal_id!r} not found in proposal ledger. "
                "Capture an explicit approval first via capture_proposal_decision.py."
            )
        decision = latest_decision_audit(conn, args.proposal_id)
    finally:
        conn.close()

    idempotency_key = f"proposal-apply:{args.proposal_id}"
    task_payload = build_task_payload(row, packet, args.proposal_id, proposal_dir / f"{args.proposal_id}.apply.json")
    apply_json_path, apply_md_path = write_apply_artifacts(
        proposal_id=args.proposal_id,
        proposal_dir=proposal_dir,
        packet_path=packet_path,
        row=row,
        decision=decision,
        mode="execute" if args.execute else "dry_run",
        idempotency_key=idempotency_key,
        task_payload=task_payload,
    )

    if row["status"] == "applied" and row["applied_at"]:
        existing_task = existing_task_for_key(kanban_db, idempotency_key)
        return {
            "proposal_id": args.proposal_id,
            "mode": "execute" if args.execute else "dry_run",
            "ok": True,
            "action": "noop_already_applied",
            "status": "applied",
            "applied_at": row["applied_at"],
            "kanban_task_id": existing_task,
            "apply_artifact_path": str(apply_json_path),
            "apply_markdown_path": str(apply_md_path),
            "idempotency_key": idempotency_key,
        }

    if row["status"] != "approved" or not row["approved_at"]:
        raise ValueError(
            f"proposal_id {args.proposal_id!r} is not eligible for apply. "
            f"Expected approved status with approved_at set, got status={row['status']!r}, approved_at={row['approved_at']!r}."
        )

    if not args.execute:
        return {
            "proposal_id": args.proposal_id,
            "mode": "dry_run",
            "ok": True,
            "action": "plan_generated",
            "status": str(row["status"]),
            "approved_at": row["approved_at"],
            "approver": row["approver"],
            "apply_artifact_path": str(apply_json_path),
            "apply_markdown_path": str(apply_md_path),
            "idempotency_key": idempotency_key,
            "mutated": {
                "kanban": False,
                "proposal_ledger": False,
                "proposal_apply_audit": False,
            },
        }

    operator = (args.operator or "").strip()
    if not operator:
        raise ValueError("--operator is required with --execute")
    if decision is None:
        raise ValueError(
            f"proposal_id {args.proposal_id!r} has no proposal_decision_audit row. "
            "Capture an explicit approval first via capture_proposal_decision.py."
        )
    if str(decision["decision"] or "").lower() != "approve":
        raise ValueError(
            f"proposal_id {args.proposal_id!r} latest decision audit is {decision['decision']!r}; "
            "expected 'approve'. Re-run capture_proposal_decision.py to record approval provenance."
        )
    if not decision["decided_at"] or not decision["source"]:
        raise ValueError(
            f"proposal_id {args.proposal_id!r} decision audit is missing decided_at/source provenance. "
            "Re-run capture_proposal_decision.py."
        )
    if not row["approver"]:
        raise ValueError(
            f"proposal_id {args.proposal_id!r} ledger row is missing approver provenance. "
            "Re-run capture_proposal_decision.py."
        )

    approved_row_before = {
        "proposal_id": row["proposal_id"],
        "status": row["status"],
        "approved_at": row["approved_at"],
        "applied_at": row["applied_at"],
        "approver": row["approver"],
        "updated_at": row["updated_at"],
    }

    experiments_backup, kanban_backup, manifest_path = capture_execute_backups(
        backup_root=backup_root,
        proposal_id=args.proposal_id,
        experiments_db=experiments_db,
        kanban_db=kanban_db,
        operator_source=args.source,
        approved_row_before=approved_row_before,
        packet_path=packet_path,
        idempotency_key=idempotency_key,
    )

    task_id = create_kanban_task(
        hermes_home=hermes_home,
        idempotency_key=idempotency_key,
        task_payload=task_payload,
    )

    applied_at = utc_now()
    final_action = "applied"
    final_status = "applied"
    conn = sqlite3.connect(experiments_db)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_apply_audit_schema(conn)
        current = proposal_row(conn, args.proposal_id)
        if current is None:
            raise ValueError(f"proposal_id {args.proposal_id!r} disappeared during apply transaction")

        previous_status = str(current["status"] or "")
        new_status = previous_status
        action = "applied"

        if previous_status == "approved" and current["applied_at"] is None:
            update_cur = conn.execute(
                """
                UPDATE proposals
                SET status = 'applied', applied_at = ?, updated_at = ?
                WHERE proposal_id = ? AND status = 'approved' AND applied_at IS NULL
                """,
                (applied_at, applied_at, args.proposal_id),
            )
            if update_cur.rowcount == 0:
                action = "noop_update_guard"
            else:
                new_status = "applied"
        elif previous_status == "applied" and current["applied_at"]:
            action = "noop_already_applied"
            new_status = "applied"
            applied_at = str(current["applied_at"])
        else:
            raise ValueError(
                f"proposal status changed before apply write; expected approved/applied, got {previous_status!r}"
            )

        final_action = action
        final_status = new_status

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
                args.proposal_id,
                applied_at,
                action,
                operator,
                current["approver"],
                decision["decided_at"] if decision else None,
                decision["source"] if decision else None,
                args.reason,
                previous_status,
                new_status,
                args.source,
                idempotency_key,
                str(experiments_backup),
                str(kanban_backup),
                str(apply_json_path),
                task_id,
                str(manifest_path),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    require_quick_check_ok(experiments_db, stage="post_apply", label="experiments.db")
    require_quick_check_ok(kanban_db, stage="post_apply", label="kanban.db")
    update_manifest(manifest_path, task_id=task_id)

    return {
        "proposal_id": args.proposal_id,
        "mode": "execute",
        "ok": True,
        "action": final_action,
        "status": final_status,
        "applied_at": applied_at,
        "kanban_task_id": task_id,
        "idempotency_key": idempotency_key,
        "apply_artifact_path": str(apply_json_path),
        "apply_markdown_path": str(apply_md_path),
        "backup_path": str(experiments_backup),
        "kanban_backup_path": str(kanban_backup),
        "manifest_path": str(manifest_path),
        "mutated": {
            "kanban": True,
            "proposal_ledger": True,
            "proposal_apply_audit": True,
        },
    }


def main() -> int:
    args = parse_args()
    try:
        result = apply(args)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Apply helper result for {result['proposal_id']}: {result['action']}")
        print(f"Mode: {result['mode']}")
        print(f"Apply artifact: {result['apply_artifact_path']}")
        if result.get("kanban_task_id"):
            print(f"Kanban task: {result['kanban_task_id']}")
        if result.get("backup_path"):
            print(f"experiments.db backup: {result['backup_path']}")
        if result.get("kanban_backup_path"):
            print(f"kanban.db backup: {result['kanban_backup_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
