"""Deterministic Kanban board doctor checks.

Read-only by default. Intended for operators, cron watchdogs, and dashboard
health surfaces that need machine-readable stall/corruption signals without
mutating the hot Kanban SQLite DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb

Issue = dict[str, Any]

_TERMINAL = {"done", "archived"}


def _alive(pid: Any) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _issue(severity: str, kind: str, message: str, **extra: Any) -> Issue:
    data: Issue = {"severity": severity, "kind": kind, "message": message}
    data.update({k: v for k, v in extra.items() if v is not None})
    return data


def _quick_check(path: Path) -> Issue | None:
    try:
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as handle:
                header = handle.read(16)
            if header != b"SQLite format 3\000":
                return _issue(
                    "critical",
                    "db_invalid_header",
                    "Kanban DB does not have a valid SQLite header",
                    first_16=header.hex(" "),
                    action="pause kanban-reading cron jobs, stop gateway/dashboard writers, recover DB, remove stale .db-wal/.db-shm sidecars after replacement, then resume after quick_check=ok",
                )
        with kb.snapshot_connect(path) as conn:
            rows: list[str] = []
            try:
                cur = conn.execute("PRAGMA quick_check")
                while True:
                    row = cur.fetchone()
                    if row is None:
                        break
                    rows.append(str(row[0]))
                    if len(rows) >= 50:
                        break
            except sqlite3.DatabaseError as exc:
                rows.append(f"{type(exc).__name__}: {exc}")

            if rows == ["ok"]:
                return None
            first = rows[0] if rows else "no row"
            joined = " | ".join(rows[:8])
            if any("kanban_notifier_heartbeats" in row for row in rows):
                return _issue(
                    "warning",
                    "notifier_heartbeat_integrity",
                    f"Non-critical notifier heartbeat telemetry failed quick_check: {first}",
                    quick_check_rows=rows[:8],
                    action="reset ephemeral notifier telemetry only: DELETE FROM kanban_notifier_heartbeats; drop/recreate idx_notifier_heartbeats_*; do not recover or replace the main board DB unless other tables also fail",
                )
            return _issue(
                "critical",
                "db_quick_check_failed",
                f"PRAGMA quick_check returned {joined}",
                action="stop gateway/dashboard/cron writers, recover with sqlite3 .recover or latest backup, replace the DB, remove stale .db-wal/.db-shm sidecars, then resume only after quick_check=ok",
            )
    except Exception as exc:
        return _issue(
            "critical",
            "db_unreadable",
            f"Kanban DB is unreadable: {type(exc).__name__}: {exc}",
            action="pause kanban-reading cron jobs, stop gateway/dashboard writers, recover DB, remove stale .db-wal/.db-shm sidecars after replacement, then resume after quick_check=ok",
        )
    return None


def run_board_doctor(*, board: str | None = None, ready_age_seconds: int = 15 * 60) -> dict[str, Any]:
    path = kb.kanban_db_path(board=board)
    now = int(time.time())
    issues: list[Issue] = []
    db_issue = _quick_check(path)
    if db_issue:
        issues.append(db_issue)
        if db_issue.get("severity") == "critical":
            return {"ok": False, "board": board or kb.get_current_board(), "db_path": str(path), "issues": issues, "as_of": now}

    with kb.snapshot_connect(board=board) as conn:
        # Orphan dependency/rollup links.
        for row in conn.execute(
            """
            SELECT l.parent_id, l.child_id, l.relation_type,
                   p.id AS parent_exists, c.id AS child_exists
              FROM task_links l
              LEFT JOIN tasks p ON p.id = l.parent_id
              LEFT JOIN tasks c ON c.id = l.child_id
             WHERE p.id IS NULL OR c.id IS NULL
             ORDER BY l.parent_id, l.child_id
            """
        ):
            missing = []
            if row["parent_exists"] is None:
                missing.append("parent")
            if row["child_exists"] is None:
                missing.append("child")
            issues.append(_issue(
                "error", "orphan_task_link",
                f"task_links references missing {'/'.join(missing)} row",
                parent_id=row["parent_id"], child_id=row["child_id"], relation_type=row["relation_type"],
                action="remove/recreate the orphan link before relying on dependency promotion",
            ))

        # Profile event subscriptions pointing at missing tasks.
        for row in conn.execute(
            """
            SELECT s.task_id, s.profile, s.name
              FROM kanban_profile_event_subs s
              LEFT JOIN tasks t ON t.id = s.task_id
             WHERE t.id IS NULL
             ORDER BY s.task_id, s.profile, s.name
            """
        ):
            issues.append(_issue(
                "error", "orphan_profile_event_subscription",
                "profile wake subscription references a missing task",
                task_id=row["task_id"], profile=row["profile"], name=row["name"],
                action="remove the subscription or recreate the task before enabling notifier wakes",
            ))

        # Running tasks with expired claim/dead worker.
        for row in conn.execute(
            """
            SELECT id, title, assignee, worker_pid, claim_expires, last_heartbeat_at, current_run_id
              FROM tasks
             WHERE status = 'running'
             ORDER BY started_at, created_at
            """
        ):
            pid_alive = _alive(row["worker_pid"])
            expired = bool(row["claim_expires"] and int(row["claim_expires"]) < now)
            stale_hb = bool(row["last_heartbeat_at"] and now - int(row["last_heartbeat_at"]) > 15 * 60)
            if not pid_alive or expired or stale_hb:
                issues.append(_issue(
                    "critical" if expired or not pid_alive else "warning",
                    "stale_running_task",
                    "running task has dead/missing worker, expired claim, or stale heartbeat",
                    task_id=row["id"], assignee=row["assignee"], worker_pid=row["worker_pid"],
                    pid_alive=pid_alive, claim_expired=expired,
                    heartbeat_age_seconds=(now - int(row["last_heartbeat_at"])) if row["last_heartbeat_at"] else None,
                    action="reclaim or inspect worker logs before retrying",
                ))

        # Stale run rows left marked running after task already moved on.
        for row in conn.execute(
            """
            SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at,
                   t.status AS task_status, t.current_run_id
              FROM task_runs r
              JOIN tasks t ON t.id = r.task_id
             WHERE r.status = 'running'
               AND (t.status != 'running' OR t.current_run_id IS NULL OR t.current_run_id != r.id)
             ORDER BY r.started_at DESC
            """
        ):
            issues.append(_issue(
                "warning", "stale_running_run",
                "task_run is still marked running but is not the task current running run",
                task_id=row["task_id"], run_id=row["run_id"], profile=row["profile"], task_status=row["task_status"],
                worker_pid=row["worker_pid"], pid_alive=_alive(row["worker_pid"]),
                action="mark/reconcile stale run metadata; do not treat it as an active worker",
            ))

        # Blocked tasks whose dependency parents are all terminal: likely needs explicit requeue/unblock.
        for row in conn.execute(
            """
            SELECT c.id, c.title, c.assignee, COUNT(l.parent_id) AS parents,
                   SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) AS terminal_parents,
                   GROUP_CONCAT(p.id || ':' || p.status, ', ') AS parent_state
              FROM tasks c
              JOIN task_links l ON l.child_id = c.id
              JOIN tasks p ON p.id = l.parent_id
             WHERE c.status = 'blocked'
               AND COALESCE(l.relation_type, 'dependency') = 'dependency'
             GROUP BY c.id
            HAVING parents > 0 AND parents = terminal_parents
             ORDER BY c.created_at
            """
        ):
            issues.append(_issue(
                "warning", "blocked_with_completed_parents",
                "blocked task has all dependency parents completed; likely needs an explicit unblock/re-review decision",
                task_id=row["id"], assignee=row["assignee"], parents=row["parent_state"],
                action="if remediation evidence is sufficient, run `hermes kanban unblock <task>`; otherwise park with a fresh blocker comment",
            ))

        # Ready tasks old enough that dispatcher may not be picking them up.
        for row in conn.execute(
            """
            SELECT id, title, assignee, created_at
              FROM tasks
             WHERE status = 'ready'
             ORDER BY created_at
            """
        ):
            age = now - int(row["created_at"])
            if age >= ready_age_seconds:
                issues.append(_issue(
                    "warning", "old_ready_task",
                    "ready task has not been claimed within the threshold",
                    task_id=row["id"], assignee=row["assignee"], age_seconds=age,
                    action="check gateway dispatcher health and whether assignee profile exists",
                ))

    reconcile_summary = _reconcile_summary(board=board, ready_age_seconds=ready_age_seconds)
    suppressed_blocked_tasks = {
        str(packet.get("task_id"))
        for packet in reconcile_summary.get("suppressed_decision_packets") or []
        if "blocked_with_completed_parents_decision" in (packet.get("kinds") or [])
    }
    if suppressed_blocked_tasks:
        before = len(issues)
        issues = [
            issue for issue in issues
            if not (
                issue.get("kind") == "blocked_with_completed_parents"
                and str(issue.get("task_id")) in suppressed_blocked_tasks
            )
        ]
        suppressed_count = before - len(issues)
        if suppressed_count:
            reconcile_summary["suppressed_doctor_issue_count"] = suppressed_count
    return {"ok": not issues, "board": board or kb.get_current_board(), "db_path": str(path), "issues": issues, "reconcile_summary": reconcile_summary, "as_of": now}


def _reconcile_summary(*, board: str | None, ready_age_seconds: int) -> dict[str, Any]:
    """Embed a compact reconciler summary without changing doctor ok semantics."""
    try:
        from hermes_cli import kanban_reconciler as rec

        result = rec.run_reconciler(
            board=board,
            ready_age_seconds=max(1, int(ready_age_seconds or 900)),
        )
        triage = result.get("wake_triage") or {}
        actions = result.get("actions") or []
        kinds: dict[str, int] = {}
        for action in actions:
            if isinstance(action, dict):
                kind = str(action.get("kind") or "unknown")
                kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "ok": bool(result.get("ok")),
            "action_count": len(actions),
            "wake_mode": triage.get("mode"),
            "wake_agent": bool(triage.get("wake_agent")),
            "suppressed_decision_packet_count": int(triage.get("suppressed_decision_packet_count") or 0),
            "suppressed_decision_packets": list(triage.get("suppressed_decision_packets") or []),
            "kinds": kinds,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def format_doctor_text(result: dict[str, Any]) -> str:
    issues = result.get("issues") or []
    if not issues:
        return f"Kanban board doctor: ok ({result.get('board')})"
    lines = [f"Kanban board doctor: {len(issues)} issue(s) on {result.get('board')} ({result.get('db_path')})"]
    for item in issues:
        loc = item.get("task_id") or item.get("parent_id") or item.get("run_id") or "board"
        lines.append(f"- {item['severity'].upper()} {item['kind']} [{loc}]: {item['message']}")
        if item.get("action"):
            lines.append(f"  action: {item['action']}")
        details = {k: v for k, v in item.items() if k not in {"severity", "kind", "message", "action"}}
        if details:
            lines.append("  details: " + json.dumps(details, sort_keys=True, default=str))
    return "\n".join(lines)
