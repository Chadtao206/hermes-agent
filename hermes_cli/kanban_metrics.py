"""Deterministic Kanban reliability metrics and optional sidecar snapshots."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from hermes_cli import kanban_board_doctor as kdoc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_reconciler as krec

_FAILURE_OUTCOMES = {
    "crashed",
    "timed_out",
    "spawn_failed",
    "reclaimed",
    "operator_cleanup",
}
_FAILURE_EVENT_KINDS = {
    "completion_blocked_hallucination",
    "completion_blocked_pr_head_gate",
    "crashed",
    "gave_up",
    "protocol_violation",
    "reclaimed",
    "respawn_guarded",
    "spawn_auto_blocked",
    "spawn_failed",
    "timed_out",
}
_DEFAULT_WINDOWS = (
    ("24h", 24 * 60 * 60),
    ("7d", 7 * 24 * 60 * 60),
    ("all", None),
)


def _count_map(rows: list[sqlite3.Row], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row[key] or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _percentile(sorted_values: list[int], percentile: float) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = int(round((len(sorted_values) - 1) * percentile))
    idx = max(0, min(len(sorted_values) - 1, idx))
    return sorted_values[idx]


def _top_counts(counts: dict[str, int], *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"key": key, "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _run_window_metrics(
    conn: sqlite3.Connection,
    *,
    label: str,
    cutoff: Optional[int],
    now: int,
) -> dict[str, Any]:
    if cutoff is None:
        rows = conn.execute("SELECT * FROM task_runs").fetchall()
        event_rows = conn.execute("SELECT kind FROM task_events").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE COALESCE(started_at, 0) >= ?",
            (cutoff,),
        ).fetchall()
        event_rows = conn.execute(
            "SELECT kind FROM task_events WHERE COALESCE(created_at, 0) >= ?",
            (cutoff,),
        ).fetchall()

    total_runs = len(rows)
    outcome_counts = _count_map(rows, "outcome")
    status_counts = _count_map(rows, "status")
    attempts_by_task: dict[str, int] = {}
    attempts_by_profile: dict[str, int] = {}
    failure_or_reclaim_count = 0
    completion_count = 0
    blocked_count = 0
    error_count = 0
    durations: list[int] = []

    for row in rows:
        task_id = str(row["task_id"] or "")
        profile = str(row["profile"] or "unassigned")
        if task_id:
            attempts_by_task[task_id] = attempts_by_task.get(task_id, 0) + 1
        attempts_by_profile[profile] = attempts_by_profile.get(profile, 0) + 1
        outcome = str(row["outcome"] or row["status"] or "unknown")
        if outcome == "completed":
            completion_count += 1
        if outcome == "blocked":
            blocked_count += 1
        if outcome in _FAILURE_OUTCOMES or row["error"]:
            failure_or_reclaim_count += 1
        if row["error"]:
            error_count += 1
        started = int(row["started_at"] or 0)
        ended = int(row["ended_at"] or now)
        if started > 0 and ended >= started:
            durations.append(ended - started)

    event_counts: dict[str, int] = {}
    failure_event_counts: dict[str, int] = {}
    for row in event_rows:
        kind = str(row["kind"] or "unknown")
        event_counts[kind] = event_counts.get(kind, 0) + 1
        if kind in _FAILURE_EVENT_KINDS:
            failure_event_counts[kind] = failure_event_counts.get(kind, 0) + 1

    durations.sort()
    tasks_attempted = len(attempts_by_task)
    max_attempts = max(attempts_by_task.values()) if attempts_by_task else 0
    return {
        "label": label,
        "cutoff": cutoff,
        "total_runs": total_runs,
        "tasks_attempted": tasks_attempted,
        "avg_attempts_per_task": round(total_runs / tasks_attempted, 2) if tasks_attempted else 0.0,
        "max_attempts_per_task": max_attempts,
        "task_attempt_hotspots": _top_counts(attempts_by_task, limit=10),
        "attempts_by_profile": _top_counts(attempts_by_profile, limit=10),
        "outcome_counts": outcome_counts,
        "status_counts": status_counts,
        "completion_count": completion_count,
        "completion_rate": _rate(completion_count, total_runs),
        "blocked_count": blocked_count,
        "blocked_rate": _rate(blocked_count, total_runs),
        "failure_or_reclaim_count": failure_or_reclaim_count,
        "failure_or_reclaim_rate": _rate(failure_or_reclaim_count, total_runs),
        "error_count": error_count,
        "duration_seconds": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else 0,
        },
        "event_counts": dict(sorted(event_counts.items(), key=lambda item: (-item[1], item[0]))),
        "failure_event_counts": dict(sorted(failure_event_counts.items(), key=lambda item: (-item[1], item[0]))),
    }


def _current_state_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    task_status_counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
        )
    }
    run_status_counts = {
        f"{row['status']}:{row['outcome'] or 'none'}": int(row["count"])
        for row in conn.execute(
            "SELECT status, outcome, COUNT(*) AS count FROM task_runs GROUP BY status, outcome ORDER BY status, outcome"
        )
    }
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_tasks,
            SUM(CASE WHEN status IN ('ready','review') THEN 1 ELSE 0 END) AS spawnable_pending_tasks,
            SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_tasks,
            SUM(CASE WHEN current_run_id IS NOT NULL THEN 1 ELSE 0 END) AS current_run_pointers,
            COALESCE(MAX(consecutive_failures), 0) AS max_consecutive_failures,
            COALESCE(SUM(consecutive_failures), 0) AS total_consecutive_failures
          FROM tasks
        """
    ).fetchone()
    running_run_rows = int(conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE status = 'running'"
    ).fetchone()[0])
    return {
        "task_status_counts": task_status_counts,
        "run_status_counts": run_status_counts,
        "running_tasks": int(row["running_tasks"] or 0),
        "spawnable_pending_tasks": int(row["spawnable_pending_tasks"] or 0),
        "blocked_tasks": int(row["blocked_tasks"] or 0),
        "current_run_pointers": int(row["current_run_pointers"] or 0),
        "running_run_rows": running_run_rows,
        "max_consecutive_failures": int(row["max_consecutive_failures"] or 0),
        "total_consecutive_failures": int(row["total_consecutive_failures"] or 0),
    }


def default_snapshot_db_path() -> Path:
    return get_hermes_home() / "kanban_metrics_snapshots.db"


def _init_snapshot_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kanban_metrics_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board TEXT NOT NULL,
            captured_at INTEGER NOT NULL,
            source_db_path TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kanban_metrics_snapshots_board_time "
        "ON kanban_metrics_snapshots(board, captured_at)"
    )


def write_metrics_snapshot(
    result: dict[str, Any],
    *,
    snapshot_db: Optional[Path] = None,
) -> dict[str, Any]:
    path = Path(snapshot_db) if snapshot_db else default_snapshot_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload.pop("persisted_snapshot", None)
    with sqlite3.connect(path) as conn:
        _init_snapshot_db(conn)
        cur = conn.execute(
            """
            INSERT INTO kanban_metrics_snapshots
                (board, captured_at, source_db_path, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(result.get("board") or kb.DEFAULT_BOARD),
                int(result.get("captured_at") or time.time()),
                str(result.get("db_path") or ""),
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
            ),
        )
        conn.commit()
        row_id = int(cur.lastrowid or 0)
    return {"id": row_id, "db_path": str(path)}


def collect_metrics(
    *,
    board: Optional[str] = None,
    since_epoch: Optional[int] = None,
    ready_age_seconds: int = 15 * 60,
    now: Optional[int] = None,
    write_snapshot: bool = False,
    snapshot_db: Optional[Path] = None,
) -> dict[str, Any]:
    as_of = int(now if now is not None else time.time())
    path = kb.kanban_db_path(board=board)
    board_name = board or kb.get_current_board()
    with kb.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        current_state = _current_state_metrics(conn)
        windows = [
            _run_window_metrics(
                conn,
                label=label,
                cutoff=None if seconds is None else as_of - int(seconds),
                now=as_of,
            )
            for label, seconds in _DEFAULT_WINDOWS
        ]
        if since_epoch is not None:
            windows.append(
                _run_window_metrics(
                    conn,
                    label="since",
                    cutoff=int(since_epoch),
                    now=as_of,
                )
            )
    doctor = kdoc.run_board_doctor(
        board=board,
        ready_age_seconds=max(1, int(ready_age_seconds or 1)),
    )
    reconcile = krec.run_reconciler(
        board=board,
        ready_age_seconds=max(1, int(ready_age_seconds or 1)),
    )
    wake_triage = reconcile.get("wake_triage") or {}
    result: dict[str, Any] = {
        "ok": bool(doctor.get("ok")) and bool(reconcile.get("ok")),
        "board": board_name,
        "db_path": str(path),
        "captured_at": as_of,
        "current_state": current_state,
        "health": {
            "doctor_ok": bool(doctor.get("ok")),
            "doctor_issue_count": len(doctor.get("issues") or []),
            "doctor_critical_count": sum(
                1 for issue in doctor.get("issues") or []
                if issue.get("severity") == "critical"
            ),
            "reconcile_ok": bool(reconcile.get("ok")),
            "reconcile_action_count": len(reconcile.get("actions") or []),
            "wake_mode": wake_triage.get("mode"),
            "wake_agent": bool(wake_triage.get("wake_agent")),
            "suppressed_decision_packet_count": int(
                wake_triage.get("suppressed_decision_packet_count") or 0
            ),
            "reconcile_kind_counts": {
                str(kind): int(count)
                for kind, count in (
                    (doctor.get("reconcile_summary") or {}).get("kinds") or {}
                ).items()
            },
        },
        "windows": windows,
        "schema_version": 1,
    }
    if write_snapshot:
        result["persisted_snapshot"] = write_metrics_snapshot(
            result,
            snapshot_db=snapshot_db,
        )
    return result


def format_metrics_text(result: dict[str, Any]) -> str:
    health = result.get("health") or {}
    current = result.get("current_state") or {}
    lines = [
        "Kanban reliability metrics",
        f"- board: {result.get('board')}",
        f"- ok: {bool(result.get('ok'))}",
        (
            "- health: "
            f"doctor_ok={health.get('doctor_ok')} "
            f"reconcile_ok={health.get('reconcile_ok')} "
            f"actions={health.get('reconcile_action_count')} "
            f"wake_mode={health.get('wake_mode')} "
            f"suppressed={health.get('suppressed_decision_packet_count')}"
        ),
        (
            "- current: "
            f"running_tasks={current.get('running_tasks')} "
            f"pending={current.get('spawnable_pending_tasks')} "
            f"blocked={current.get('blocked_tasks')} "
            f"running_run_rows={current.get('running_run_rows')} "
            f"max_consecutive_failures={current.get('max_consecutive_failures')}"
        ),
    ]
    for window in result.get("windows") or []:
        lines.append(
            f"- {window.get('label')}: "
            f"runs={window.get('total_runs')} "
            f"tasks={window.get('tasks_attempted')} "
            f"avg_attempts={window.get('avg_attempts_per_task')} "
            f"max_attempts={window.get('max_attempts_per_task')} "
            f"completed={window.get('completion_count')} "
            f"blocked={window.get('blocked_count')} "
            f"failure_or_reclaim_rate={window.get('failure_or_reclaim_rate')} "
            f"p95_s={((window.get('duration_seconds') or {}).get('p95'))}"
        )
    if result.get("persisted_snapshot"):
        snap = result["persisted_snapshot"]
        lines.append(f"- snapshot: id={snap.get('id')} db={snap.get('db_path')}")
    return "\n".join(lines)
