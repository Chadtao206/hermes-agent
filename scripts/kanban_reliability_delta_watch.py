#!/usr/bin/env python3
"""Compact Kanban runtime reliability delta watchdog.

No-agent cron script: prints only when a new real/substantial runtime
reliability signal needs attention. Empty stdout means silent success.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or "/Users/ctao/.hermes")
KANBAN_DB = HERMES_HOME / "kanban.db"
STATE_PATH = HERMES_HOME / "kanban_reliability_delta_watch_state.json"

RELIABILITY_KINDS = {
    "crashed",
    "gave_up",
    "protocol_violation",
    "reclaimed",
    "respawn_guarded",
    "double_close_attempt",
    "pre_spawn_validation_failed",
    "completion_blocked_pr_head_gate",
    "reconcile_stale_run_metadata_closed",
    "reconcile_orphan_claim_lock_cleared",
}
REAL_PROFILES = {"default", "designer", "engineer", "ops", "researcher", "reviewer"}
SYNTHETIC_TITLE_TOKENS = (
    "repro",
    "stale-dispatch",
    "test",
    "demo",
    "x",
)
OPEN_STATUSES = {"ready", "running", "blocked", "scheduled", "todo", "triage", "review"}


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(payload: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(KANBAN_DB))
    conn.row_factory = sqlite3.Row
    return conn


def is_synthetic(row: sqlite3.Row) -> bool:
    title = str(row["title"] or "").strip().lower()
    assignee = str(row["assignee"] or "")
    if assignee and assignee not in REAL_PROFILES:
        return True
    if title in SYNTHETIC_TITLE_TOKENS:
        return True
    if any(token in title for token in ("repro", "stale-dispatch")):
        return True
    return False


def fetch_events(conn: sqlite3.Connection, cutoff: int) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in RELIABILITY_KINDS)
    return list(
        conn.execute(
            f"""
            SELECT e.id, e.task_id, e.kind, e.created_at, e.payload,
                   t.title, t.assignee, t.status, t.completed_at, t.consecutive_failures,
                   t.last_failure_error
              FROM task_events e
              LEFT JOIN tasks t ON t.id = e.task_id
             WHERE e.created_at >= ?
               AND e.kind IN ({placeholders})
             ORDER BY e.created_at ASC, e.id ASC
            """,
            [cutoff, *sorted(RELIABILITY_KINDS)],
        )
    )


def summarize(rows: list[sqlite3.Row]) -> dict[str, Any]:
    kind_counts: dict[str, int] = {}
    real_rows: list[sqlite3.Row] = []
    synthetic_rows: list[sqlite3.Row] = []
    for row in rows:
        kind = str(row["kind"])
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if is_synthetic(row):
            synthetic_rows.append(row)
        else:
            real_rows.append(row)

    open_real: dict[str, dict[str, Any]] = {}
    closed_real: dict[str, dict[str, Any]] = {}
    for row in real_rows:
        task_id = str(row["task_id"])
        bucket = open_real if str(row["status"] or "") in OPEN_STATUSES else closed_real
        entry = bucket.setdefault(
            task_id,
            {
                "task_id": task_id,
                "title": row["title"] or "",
                "assignee": row["assignee"] or "",
                "status": row["status"] or "unknown",
                "kinds": {},
                "last_event_at": 0,
                "last_event_id": 0,
            },
        )
        entry["kinds"][row["kind"]] = entry["kinds"].get(row["kind"], 0) + 1
        entry["last_event_at"] = max(int(entry["last_event_at"]), int(row["created_at"] or 0))
        entry["last_event_id"] = max(int(entry["last_event_id"]), int(row["id"] or 0))

    return {
        "total_events": len(rows),
        "kind_counts": kind_counts,
        "real_event_count": len(real_rows),
        "synthetic_event_count": len(synthetic_rows),
        "open_real_tasks": sorted(open_real.values(), key=lambda r: (-r["last_event_at"], r["task_id"])),
        "closed_real_task_count": len(closed_real),
        "latest_event_id": max((int(r["id"]) for r in rows), default=0),
    }


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def main() -> int:
    if not KANBAN_DB.exists():
        print(f"Kanban reliability watch error: missing {KANBAN_DB}")
        return 1

    now = int(time.time())
    cutoff_24h = now - 24 * 60 * 60
    with connect() as conn:
        rows_24h = fetch_events(conn, cutoff_24h)
        summary_24h = summarize(rows_24h)

    previous = load_state()
    previous_latest = int(previous.get("latest_event_id") or 0)
    first_run = not previous
    latest_event_id = int(summary_24h["latest_event_id"] or 0)

    new_rows = [r for r in rows_24h if int(r["id"] or 0) > previous_latest]
    new_summary = summarize(new_rows)
    open_real_tasks = summary_24h["open_real_tasks"]

    state = {
        "checked_at": now,
        "latest_event_id": latest_event_id,
        "last_24h": {
            "total_events": summary_24h["total_events"],
            "real_event_count": summary_24h["real_event_count"],
            "synthetic_event_count": summary_24h["synthetic_event_count"],
            "kind_counts": summary_24h["kind_counts"],
            "open_real_task_ids": [t["task_id"] for t in open_real_tasks],
        },
    }
    save_state(state)

    # Noise policy: after baseline, stay silent unless there are new reliability
    # events or a real open task currently has reliability churn in the 24h window.
    if not first_run and not new_rows and not open_real_tasks:
        return 0

    lines: list[str] = []
    if first_run:
        lines.append("Kanban reliability delta watch baseline established.")
    else:
        lines.append("Kanban reliability delta watch signal.")
    lines.append(
        "24h: "
        f"events={summary_24h['total_events']} "
        f"real={summary_24h['real_event_count']} "
        f"synthetic={summary_24h['synthetic_event_count']} "
        f"kinds=({format_counts(summary_24h['kind_counts'])})"
    )
    if new_rows:
        lines.append(
            "New since last check: "
            f"events={new_summary['total_events']} "
            f"real={new_summary['real_event_count']} "
            f"synthetic={new_summary['synthetic_event_count']} "
            f"kinds=({format_counts(new_summary['kind_counts'])})"
        )
    if open_real_tasks:
        lines.append("Open real tasks with 24h reliability churn:")
        for task in open_real_tasks[:8]:
            lines.append(
                f"- {task['task_id']} [{task['assignee']}/{task['status']}]: "
                f"{task['title']} | {format_counts(task['kinds'])}"
            )
        if len(open_real_tasks) > 8:
            lines.append(f"- ... {len(open_real_tasks) - 8} more")
    else:
        lines.append("Open real tasks with 24h reliability churn: none")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
