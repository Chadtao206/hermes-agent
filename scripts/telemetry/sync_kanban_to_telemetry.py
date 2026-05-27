#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from common import canonical_profile, events_connection, json_dumps, resolve_telemetry_root

KANBAN_DB = Path.home() / ".hermes" / "kanban.db"

STATUS_TO_OUTCOME = {
    "done": "success",
    "archived": "success",
}


def is_completed_kanban_task(task_row: sqlite3.Row) -> bool:
    """Return true when kanban has a durable terminal success timestamp.

    Kanban tasks are often archived after completion. The live board keeps the
    original `completed_at` timestamp, but the mutable `status` becomes
    `archived`. Treating archived+completed rows as open makes closed telemetry
    look incomplete and prevents routing correctness finalization.
    """
    return task_row["status"] == "done" or (task_row["status"] == "archived" and task_row["completed_at"] is not None)

RUN_STATE_MAP = {
    "promoted": "promoted",
    "claimed": "claimed",
    "spawned": "spawned",
    "heartbeat": "heartbeat",
    "claim_extended": "claim_extended",
    "blocked": "blocked",
    "unblocked": "unblocked",
    "released": "released",
    "completed": "completed",
    "spawn_failed": "spawn_failed",
    "respawn_guarded": "respawn_guarded",
    "protocol_violation": "protocol_violation",
    "crashed": "crashed",
    "gave_up": "gave_up",
    "reclaimed": "reclaimed",
}



def task_profile(task_row: sqlite3.Row, key: str, default: str = "unassigned") -> str:
    return canonical_profile(task_row[key], default=default)


def run_profile(run_row: sqlite3.Row, default: str = "unassigned") -> str:
    return canonical_profile(run_row["profile"], default=default)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Hermes kanban state into self-improvement telemetry.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--kanban-db", default=str(KANBAN_DB), help="Path to kanban SQLite database")
    parser.add_argument("--task-id", help="Sync only one kanban task id")
    return parser.parse_args()


def iso_from_epoch(value):
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()


def load_rows(conn: sqlite3.Connection, query: str, params=()):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.row_factory = None
    return rows


def parse_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def run_id_from_kanban_run(run_row: sqlite3.Row) -> str:
    return f"kanban_run:{run_row['id']}"


def select_run_id_for_event(run_rows: list[sqlite3.Row], event_row: sqlite3.Row) -> str | None:
    explicit = event_row["run_id"]
    if explicit is not None:
        for run in run_rows:
            if run["id"] == explicit:
                return run_id_from_kanban_run(run)
        return f"kanban_run:{explicit}"
    return None


def task_status_projection(task_row: sqlite3.Row) -> tuple[str, str | None, int]:
    kanban_status = task_row["status"]
    if is_completed_kanban_task(task_row):
        telemetry_status = "completed"
        outcome = "success"
    elif kanban_status == "blocked" and task_row["consecutive_failures"]:
        telemetry_status = "failed"
        outcome = "fail"
    else:
        telemetry_status = "open"
        outcome = None
    reopened = 1 if (task_row["consecutive_failures"] or 0) > 0 else 0
    return telemetry_status, outcome, reopened


def ensure_task(telemetry_conn: sqlite3.Connection, task_row: sqlite3.Row, run_rows: list[sqlite3.Row], link_rows: list[sqlite3.Row], comment_count: int) -> None:
    task_id = f"kanban:{task_row['id']}"
    workflow_type = "kanban"
    notes = {
        "kanban_status": task_row["status"],
        "priority": task_row["priority"],
        "workspace_kind": task_row["workspace_kind"],
        "workspace_path": task_row["workspace_path"],
        "created_by": task_profile(task_row, "created_by", default=""),
        "tenant": task_row["tenant"],
        "consecutive_failures": task_row["consecutive_failures"],
        "comment_count": comment_count,
        "parent_count": len([row for row in link_rows if row['child_id'] == task_row['id']]),
        "child_count": len([row for row in link_rows if row['parent_id'] == task_row['id']]),
        "run_count": len(run_rows),
    }
    if task_row["result"]:
        notes["kanban_result"] = task_row["result"]
    if task_row["skills"]:
        notes["kanban_skills"] = task_row["skills"]

    status, outcome, reopened = task_status_projection(task_row)

    existing_row = telemetry_conn.execute(
        "SELECT user_goal_summary, notes_json, closeout_source FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    existing = 1 if existing_row else 0
    if existing:
        merged_notes = parse_payload(existing_row[1])
        merged_notes.update(notes)
        summary_value = (
            existing_row[0]
            if existing_row[2] == 'closeout' and existing_row[0]
            else (task_row["body"] or task_row["title"])
        )
        telemetry_conn.execute(
            """
            UPDATE tasks
            SET closed_at = ?,
                status = ?,
                surface = 'kanban',
                kanban_task_id = ?,
                title = ?,
                user_goal_summary = ?,
                owner_profile = ?,
                task_type = ?,
                workdir = COALESCE(?, workdir),
                verification_strength = COALESCE(verification_strength, 'moderate'),
                outcome = ?,
                reopened = ?,
                notes_json = ?
            WHERE task_id = ?
            """,
            (
                iso_from_epoch(task_row["completed_at"]),
                status,
                task_row["id"],
                task_row["title"],
                summary_value,
                task_profile(task_row, "assignee"),
                workflow_type,
                task_row["workspace_path"],
                outcome,
                reopened,
                json_dumps(merged_notes),
                task_id,
            ),
        )
    else:
        telemetry_conn.execute(
            """
            INSERT INTO tasks(
                task_id, opened_at, closed_at, status, surface, kanban_task_id, title,
                user_goal_summary, owner_profile, assisting_profiles, task_type, workdir,
                repo_hint, verification_strength, outcome, reopened, final_confidence, notes_json
            ) VALUES (?, ?, ?, ?, 'kanban', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                iso_from_epoch(task_row["created_at"]),
                iso_from_epoch(task_row["completed_at"]),
                status,
                task_row["id"],
                task_row["title"],
                task_row["body"] or task_row["title"],
                task_profile(task_row, "assignee"),
                json_dumps([]),
                workflow_type,
                task_row["workspace_path"],
                task_row["tenant"],
                "moderate",
                outcome,
                reopened,
                None,
                json_dumps(notes),
            ),
        )
        telemetry_conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, 'task_opened', ?, ?)",
            (
                task_id,
                iso_from_epoch(task_row["created_at"]),
                task_profile(task_row, "created_by", default=task_profile(task_row, "assignee")),
                json_dumps({"source": "kanban", "kanban_status": task_row["status"]}),
            ),
        )


def sync_events(telemetry_conn: sqlite3.Connection, task_row: sqlite3.Row, event_rows: list[sqlite3.Row], run_rows: list[sqlite3.Row]) -> None:
    telemetry_task_id = f"kanban:{task_row['id']}"
    existing_keys = {
        (row[0], row[1], row[2] or "")
        for row in telemetry_conn.execute(
            "SELECT occurred_at, event_type, COALESCE(payload_json, '') FROM task_events WHERE task_id = ?",
            (telemetry_task_id,),
        ).fetchall()
    }

    kind_map = {
        "promoted": "kanban_promoted",
        "claimed": "execution_started",
        "spawned": "worker_spawned",
        "heartbeat": "heartbeat",
        "claim_extended": "claim_extended",
        "blocked": "blocked",
        "unblocked": "unblocked",
        "completed": "kanban_completed",
        "commented": "comment_added",
        "assigned": "owner_assigned",
        "reassigned": "owner_rerouted",
        "released": "execution_released",
        "spawn_failed": "spawn_failed",
        "respawn_guarded": "respawn_guarded",
        "protocol_violation": "protocol_violation",
    }

    for row in event_rows:
        payload = row["payload"] or ""
        occurred_at = iso_from_epoch(row["created_at"])
        event_type = kind_map.get(row["kind"], f"kanban_{row['kind']}")
        key = (occurred_at, event_type, payload)
        if key in existing_keys:
            continue
        telemetry_conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
            (telemetry_task_id, occurred_at, event_type, task_profile(task_row, "assignee"), payload),
        )
        existing_keys.add(key)

    existing_route_keys = {
        (row[0], row[1], row[2])
        for row in telemetry_conn.execute(
            "SELECT occurred_at, initial_owner, current_owner FROM routing_events WHERE task_id = ?",
            (telemetry_task_id,),
        ).fetchall()
    }
    created_event = next((row for row in event_rows if row["kind"] == "created"), None)
    initial_owner = task_profile(task_row, "assignee")
    if created_event and created_event["payload"]:
        try:
            created_payload = json.loads(created_event["payload"])
            initial_owner = canonical_profile(created_payload.get("assignee") or initial_owner, default="unassigned")
        except json.JSONDecodeError:
            pass
    for row in event_rows:
        if row["kind"] not in {"assigned", "reassigned"}:
            continue
        payload = {}
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {"raw": row["payload"]}
        current_owner = canonical_profile(payload.get("assignee") or payload.get("current_owner") or task_row["assignee"], default="unassigned")
        route_key = (iso_from_epoch(row["created_at"]), initial_owner, current_owner)
        if route_key in existing_route_keys:
            continue
        telemetry_conn.execute(
            """
            INSERT INTO routing_events(task_id, occurred_at, initial_owner, current_owner, reroute_reason, ambiguity_class, was_initial_owner_correct, final_owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telemetry_task_id,
                iso_from_epoch(row["created_at"]),
                initial_owner,
                current_owner,
                payload.get("reason") or row["kind"],
                "kanban",
                None,
                current_owner if task_row["status"] == "done" else None,
            ),
        )
        existing_route_keys.add(route_key)

    existing_run_rows = telemetry_conn.execute(
        """
        SELECT id, event_type, CAST(json_extract(payload_json, '$.run_id') AS INTEGER)
        FROM task_events
        WHERE task_id = ? AND event_type IN ('run_started', 'run_finished')
        ORDER BY id
        """,
        (telemetry_task_id,),
    ).fetchall()
    existing_run_events: dict[tuple[str, int], int] = {}
    for row_id, event_type, run_id in existing_run_rows:
        if run_id is None:
            continue
        dedupe_key = (event_type, run_id)
        if dedupe_key in existing_run_events:
            telemetry_conn.execute("DELETE FROM task_events WHERE id = ?", (row_id,))
            continue
        existing_run_events[dedupe_key] = row_id

    for run in run_rows:
        payload = {
            "run_id": run["id"],
            "status": run["status"],
            "outcome": run["outcome"],
            "summary": run["summary"],
            "error": run["error"],
            "worker_pid": run["worker_pid"],
            "claim_expires": iso_from_epoch(run["claim_expires"]),
            "max_runtime_seconds": run["max_runtime_seconds"],
            "last_heartbeat_at": iso_from_epoch(run["last_heartbeat_at"]),
        }
        occurred_at = iso_from_epoch(run["started_at"])
        started_payload = json_dumps(payload)
        started_key = ("run_started", run["id"])
        existing_started_id = existing_run_events.get(started_key)
        if existing_started_id:
            telemetry_conn.execute(
                "UPDATE task_events SET occurred_at = ?, profile = ?, payload_json = ? WHERE id = ?",
                (occurred_at, run_profile(run), started_payload, existing_started_id),
            )
        else:
            cur = telemetry_conn.execute(
                "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, 'run_started', ?, ?)",
                (telemetry_task_id, occurred_at, run_profile(run), started_payload),
            )
            existing_run_events[started_key] = cur.lastrowid

        if run["ended_at"]:
            ended_at = iso_from_epoch(run["ended_at"])
            end_payload = payload.copy()
            end_payload["ended_at"] = ended_at
            finished_payload = json_dumps(end_payload)
            finished_key = ("run_finished", run["id"])
            existing_finished_id = existing_run_events.get(finished_key)
            if existing_finished_id:
                telemetry_conn.execute(
                    "UPDATE task_events SET occurred_at = ?, profile = ?, payload_json = ? WHERE id = ?",
                    (ended_at, run_profile(run), finished_payload, existing_finished_id),
                )
            else:
                cur = telemetry_conn.execute(
                    "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, 'run_finished', ?, ?)",
                    (telemetry_task_id, ended_at, run_profile(run), finished_payload),
                )
                existing_run_events[finished_key] = cur.lastrowid


def sync_terminal_event(telemetry_conn: sqlite3.Connection, task_row: sqlite3.Row) -> None:
    if task_row["status"] != "done":
        return
    telemetry_task_id = f"kanban:{task_row['id']}"
    closed_at = iso_from_epoch(task_row["completed_at"]) or iso_from_epoch(task_row["created_at"])
    payload = json_dumps(
        {
            "source": "kanban_sync",
            "kanban_status": task_row["status"],
            "outcome": STATUS_TO_OUTCOME.get(task_row["status"]),
        }
    )
    telemetry_conn.execute(
        "DELETE FROM task_events WHERE task_id = ? AND event_type IN ('task_completed', 'task_closed')",
        (telemetry_task_id,),
    )
    telemetry_conn.execute(
        "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, 'task_completed', ?, ?)",
        (telemetry_task_id, closed_at, task_profile(task_row, "assignee"), payload),
    )


def finalize_routing_accuracy(telemetry_conn: sqlite3.Connection, task_row: sqlite3.Row) -> None:
    """Evaluate was_initial_owner_correct for all routing rows of this kanban task."""
    telemetry_task_id = f"kanban:{task_row['id']}"
    rows = telemetry_conn.execute(
        "SELECT id, initial_owner, current_owner FROM routing_events WHERE task_id = ? ORDER BY occurred_at, id",
        (telemetry_task_id,),
    ).fetchall()
    if not rows:
        return
    status = task_row["status"]
    final_owner = task_profile(task_row, "assignee")
    initial_owner = rows[0][1]
    reroute_occurred = any(row[1] != row[2] for row in rows) or any(
        row[1] != rows[0][1] or row[2] != rows[0][2] for row in rows[1:]
    )
    if status == "done" and final_owner:
        was_correct = 1 if (initial_owner == final_owner and not reroute_occurred) else 0
        telemetry_conn.execute(
            "UPDATE routing_events SET was_initial_owner_correct = ?, final_owner = COALESCE(final_owner, ?) WHERE task_id = ?",
            (was_correct, final_owner, telemetry_task_id),
        )


def apply_kanban_skill_reuse(telemetry_conn: sqlite3.Connection, task_row: sqlite3.Row) -> None:
    if task_row["status"] != "done":
        return
    skills_raw = task_row["skills"]
    if not skills_raw:
        return
    try:
        skills = json.loads(skills_raw)
    except (json.JSONDecodeError, TypeError):
        skills = [chunk.strip() for chunk in str(skills_raw).split(",") if chunk.strip()]
    if not isinstance(skills, list):
        return
    telemetry_task_id = f"kanban:{task_row['id']}"
    completed_at = iso_from_epoch(task_row["completed_at"]) or iso_from_epoch(task_row["created_at"])
    profile = task_profile(task_row, "assignee")
    for raw_key in skills:
        if not raw_key:
            continue
        artifact_key = str(raw_key).strip()
        if not artifact_key:
            continue
        already = telemetry_conn.execute(
            """
            SELECT 1 FROM task_events
            WHERE task_id = ? AND event_type = 'learning_artifact_reused'
              AND json_extract(payload_json, '$.artifact_type') = 'skill'
              AND json_extract(payload_json, '$.artifact_key') = ?
            """,
            (
                telemetry_task_id,
                artifact_key,
            ),
        ).fetchone()
        if already:
            continue
        cur = telemetry_conn.execute(
            """
            UPDATE learning_artifacts
            SET last_reused_at = ?, reused_count = reused_count + 1
            WHERE artifact_type = 'skill' AND artifact_key = ?
            """,
            (completed_at, artifact_key),
        )
        payload = {"artifact_type": "skill", "artifact_key": artifact_key}
        if cur.rowcount == 0:
            payload["matched"] = False
        telemetry_conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, 'learning_artifact_reused', ?, ?)",
            (telemetry_task_id, completed_at, profile, json_dumps(payload)),
        )


def upsert_execution_runs(telemetry_conn: sqlite3.Connection, task_row: sqlite3.Row, run_rows: list[sqlite3.Row]) -> None:
    telemetry_task_id = f"kanban:{task_row['id']}"
    for run in run_rows:
        run_id = run_id_from_kanban_run(run)
        telemetry_conn.execute(
            """
            INSERT INTO execution_runs(
                task_id, run_id, profile, status, outcome,
                started_at, ended_at, summary, error, metadata_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'kanban_sync')
            ON CONFLICT(task_id, run_id) DO UPDATE SET
                profile = excluded.profile,
                status = excluded.status,
                outcome = excluded.outcome,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                summary = excluded.summary,
                error = excluded.error,
                metadata_json = excluded.metadata_json,
                source = excluded.source
            """,
            (
                telemetry_task_id,
                run_id,
                run_profile(run),
                run["status"],
                run["outcome"],
                iso_from_epoch(run["started_at"]),
                iso_from_epoch(run["ended_at"]),
                run["summary"],
                run["error"],
                json_dumps({
                    **parse_payload(run["metadata"]),
                    "worker_pid": run["worker_pid"],
                    "claim_expires": iso_from_epoch(run["claim_expires"]),
                    "max_runtime_seconds": run["max_runtime_seconds"],
                    "last_heartbeat_at": iso_from_epoch(run["last_heartbeat_at"]),
                }),
            ),
        )


def sync_run_state_events(
    telemetry_conn: sqlite3.Connection,
    task_row: sqlite3.Row,
    event_rows: list[sqlite3.Row],
    run_rows: list[sqlite3.Row],
) -> None:
    telemetry_task_id = f"kanban:{task_row['id']}"
    existing = {
        (row[0], row[1] or "", row[2], row[3] or "")
        for row in telemetry_conn.execute(
            "SELECT occurred_at, COALESCE(run_id, ''), state, COALESCE(details_json, '') FROM run_state_events WHERE task_id = ?",
            (telemetry_task_id,),
        ).fetchall()
    }

    for event_row in event_rows:
        state = RUN_STATE_MAP.get(event_row["kind"])
        if not state:
            continue
        occurred_at = iso_from_epoch(event_row["created_at"])
        details = parse_payload(event_row["payload"])
        details["kanban_event_id"] = event_row["id"]
        details["kanban_kind"] = event_row["kind"]
        details_json = json_dumps(details)
        run_id = select_run_id_for_event(run_rows, event_row)
        dedupe_key = (occurred_at, run_id or "", state, details_json)
        if dedupe_key in existing:
            continue
        telemetry_conn.execute(
            """
            INSERT INTO run_state_events(task_id, run_id, occurred_at, state, profile, details_json, source)
            VALUES (?, ?, ?, ?, ?, ?, 'kanban_sync')
            """,
            (
                telemetry_task_id,
                run_id,
                occurred_at,
                state,
                task_profile(task_row, "assignee"),
                details_json,
            ),
        )
        existing.add(dedupe_key)

    for run in run_rows:
        run_id = run_id_from_kanban_run(run)
        details_json = json_dumps({"status": run["status"], "outcome": run["outcome"]})
        started_at = iso_from_epoch(run["started_at"])
        if started_at:
            started_key = (started_at, run_id, "started", details_json)
            if started_key not in existing:
                telemetry_conn.execute(
                    """
                    INSERT INTO run_state_events(task_id, run_id, occurred_at, state, profile, details_json, source)
                    VALUES (?, ?, ?, 'started', ?, ?, 'kanban_runs')
                    """,
                    (telemetry_task_id, run_id, started_at, run_profile(run), details_json),
                )
                existing.add(started_key)

        ended_at = iso_from_epoch(run["ended_at"])
        if ended_at:
            finished_key = (ended_at, run_id, "finished", details_json)
            if finished_key not in existing:
                telemetry_conn.execute(
                    """
                    INSERT INTO run_state_events(task_id, run_id, occurred_at, state, profile, details_json, source)
                    VALUES (?, ?, ?, 'finished', ?, ?, 'kanban_runs')
                    """,
                    (telemetry_task_id, run_id, ended_at, run_profile(run), details_json),
                )
                existing.add(finished_key)


def sync_routing_decisions(
    telemetry_conn: sqlite3.Connection,
    task_row: sqlite3.Row,
    event_rows: list[sqlite3.Row],
) -> None:
    telemetry_task_id = f"kanban:{task_row['id']}"
    created_event = next((row for row in event_rows if row["kind"] == "created"), None)
    created_payload = parse_payload(created_event["payload"] if created_event else None)
    initial_owner = canonical_profile(created_payload.get("assignee") or task_row["assignee"], default="unassigned")
    initial_time = iso_from_epoch(created_event["created_at"]) if created_event else iso_from_epoch(task_row["created_at"])

    explicit_correctness = telemetry_conn.execute(
        """
        SELECT was_initial_owner_correct
        FROM routing_events
        WHERE task_id = ? AND was_initial_owner_correct IS NOT NULL
        ORDER BY occurred_at, id
        LIMIT 1
        """,
        (telemetry_task_id,),
    ).fetchone()
    correctness_value = explicit_correctness[0] if explicit_correctness else None

    decisions = [
        {
            "sequence_index": 0,
            "occurred_at": initial_time,
            "initial_owner": initial_owner,
            "decided_owner": initial_owner,
            "reason": "initial_assignment",
            "source_event_id": created_event["id"] if created_event else None,
            "was_initial_owner_correct": correctness_value,
            "evidence_source": "explicit" if correctness_value is not None else None,
        }
    ]

    reroute_events = [row for row in event_rows if row["kind"] in {"assigned", "reassigned"}]
    for idx, row in enumerate(reroute_events, start=1):
        payload = parse_payload(row["payload"])
        decided_owner = canonical_profile(payload.get("assignee") or payload.get("current_owner") or task_row["assignee"], default="unassigned")
        decisions.append(
            {
                "sequence_index": idx,
                "occurred_at": iso_from_epoch(row["created_at"]),
                "initial_owner": initial_owner,
                "decided_owner": decided_owner,
                "reason": payload.get("reason") or row["kind"],
                "source_event_id": row["id"],
                "was_initial_owner_correct": None,
                "evidence_source": None,
            }
        )

    final_owner = task_profile(task_row, "assignee") if task_row["status"] == "done" else None
    for decision in decisions:
        telemetry_conn.execute(
            """
            INSERT INTO routing_decisions(
                task_id, occurred_at, sequence_index, initial_owner, decided_owner, final_owner,
                reason, ambiguity_class, was_initial_owner_correct, evidence_source, source_event_id, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'kanban', ?, ?, ?, 'kanban_sync')
            ON CONFLICT(task_id, sequence_index) DO UPDATE SET
                occurred_at = excluded.occurred_at,
                initial_owner = excluded.initial_owner,
                decided_owner = excluded.decided_owner,
                final_owner = excluded.final_owner,
                reason = excluded.reason,
                ambiguity_class = excluded.ambiguity_class,
                was_initial_owner_correct = excluded.was_initial_owner_correct,
                evidence_source = excluded.evidence_source,
                source_event_id = excluded.source_event_id,
                source = excluded.source
            """,
            (
                telemetry_task_id,
                decision["occurred_at"],
                decision["sequence_index"],
                decision["initial_owner"],
                decision["decided_owner"],
                final_owner,
                decision["reason"],
                decision["was_initial_owner_correct"],
                decision["evidence_source"],
                decision["source_event_id"],
            ),
        )


def upsert_task_participant(
    telemetry_conn: sqlite3.Connection,
    task_id: str,
    profile: str,
    role: str,
    first_seen_at: str | None,
    last_seen_at: str | None,
    source: str,
) -> None:
    if not profile:
        return
    telemetry_conn.execute(
        """
        INSERT INTO task_participants(task_id, profile, role, first_seen_at, last_seen_at, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id, profile, role) DO UPDATE SET
            first_seen_at = COALESCE(task_participants.first_seen_at, excluded.first_seen_at),
            last_seen_at = CASE
                WHEN task_participants.last_seen_at IS NULL THEN excluded.last_seen_at
                WHEN excluded.last_seen_at IS NULL THEN task_participants.last_seen_at
                WHEN excluded.last_seen_at > task_participants.last_seen_at THEN excluded.last_seen_at
                ELSE task_participants.last_seen_at
            END,
            source = excluded.source
        """,
        (task_id, profile, role, first_seen_at, last_seen_at, source),
    )


def sync_task_participants(
    telemetry_conn: sqlite3.Connection,
    task_row: sqlite3.Row,
    event_rows: list[sqlite3.Row],
    run_rows: list[sqlite3.Row],
) -> None:
    telemetry_task_id = f"kanban:{task_row['id']}"
    created_at = iso_from_epoch(task_row["created_at"])
    last_activity = iso_from_epoch(max((row["created_at"] for row in event_rows), default=task_row["created_at"]))

    created_event = next((row for row in event_rows if row["kind"] == "created"), None)
    created_payload = parse_payload(created_event["payload"] if created_event else None)
    created_by = canonical_profile(task_row["created_by"] or created_payload.get("created_by"), default="")
    assignee = canonical_profile(task_row["assignee"] or created_payload.get("assignee"), default="")

    if created_by:
        upsert_task_participant(telemetry_conn, telemetry_task_id, created_by, "creator", created_at, created_at, "kanban_sync")
    if assignee:
        upsert_task_participant(telemetry_conn, telemetry_task_id, assignee, "owner", created_at, last_activity, "kanban_sync")

    for row in event_rows:
        if row["kind"] not in {"assigned", "reassigned"}:
            continue
        payload = parse_payload(row["payload"])
        profile = canonical_profile(payload.get("assignee") or payload.get("current_owner"), default="")
        occurred_at = iso_from_epoch(row["created_at"])
        if profile:
            upsert_task_participant(telemetry_conn, telemetry_task_id, profile, "owner", occurred_at, last_activity, "kanban_sync")

    for run in run_rows:
        profile = run_profile(run, default="")
        if not profile:
            continue
        first_seen = iso_from_epoch(run["started_at"]) or created_at
        last_seen = iso_from_epoch(run["ended_at"]) or iso_from_epoch(run["last_heartbeat_at"]) or first_seen
        upsert_task_participant(telemetry_conn, telemetry_task_id, profile, "runner", first_seen, last_seen, "kanban_sync")


def task_telemetry_completeness(
    telemetry_task_row: sqlite3.Row | None,
    task_row: sqlite3.Row,
    event_rows: list[sqlite3.Row],
    run_rows: list[sqlite3.Row],
) -> tuple[int, str]:
    gaps: list[str] = []
    notes = parse_payload(telemetry_task_row["notes_json"] if telemetry_task_row is not None else None)
    completed_task = is_completed_kanban_task(task_row)
    if completed_task:
        notes.setdefault("correction_state", "unknown")
        notes.setdefault("learning_artifact_state", "unknown")
    provenance = telemetry_task_row["provenance"] if telemetry_task_row is not None else None
    substantiality = telemetry_task_row["substantiality"] if telemetry_task_row is not None else None
    if completed_task:
        provenance = provenance or "real"
        substantiality = substantiality or "substantial"
    created_by_profile = None
    if telemetry_task_row is not None:
        try:
            created_by_profile = telemetry_task_row["created_by_profile"]
        except Exception:
            created_by_profile = None
    if not provenance:
        gaps.append("provenance")
    if not substantiality:
        gaps.append("substantiality")
    if not (canonical_profile(task_row["created_by"], default="") or created_by_profile or notes.get("created_by")):
        gaps.append("created_by_profile")
    if not event_rows:
        gaps.append("activity_events")
    # Historical archived root/wrapper rows can be completed without a worker run.
    # Keep the original strict run/completed_at requirement for live `done` rows
    # but do not reintroduce gaps when syncing archived+completed history.
    if task_row["status"] == "done" and not run_rows:
        gaps.append("execution_runs")
    if task_row["status"] == "done" and task_row["completed_at"] is None:
        gaps.append("completed_at")
    if completed_task and notes.get("correction_state") is None:
        gaps.append("correction_state")
    if completed_task and notes.get("learning_artifact_state") is None:
        gaps.append("learning_artifact_state")
    return (1 if not gaps else 0, json_dumps(gaps))


def update_task_hardening_fields(
    telemetry_conn: sqlite3.Connection,
    task_row: sqlite3.Row,
    event_rows: list[sqlite3.Row],
    run_rows: list[sqlite3.Row],
) -> None:
    telemetry_task_id = f"kanban:{task_row['id']}"
    created_event = next((row for row in event_rows if row["kind"] == "created"), None)
    created_payload = parse_payload(created_event["payload"] if created_event else None)
    created_by_profile = canonical_profile(task_row["created_by"] or created_payload.get("created_by"), default="")
    telemetry_conn.row_factory = sqlite3.Row
    try:
        telemetry_task_row = telemetry_conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (telemetry_task_id,),
        ).fetchone()
    finally:
        telemetry_conn.row_factory = None
    existing_notes = parse_payload(telemetry_task_row["notes_json"] if telemetry_task_row is not None else None)
    if is_completed_kanban_task(task_row):
        existing_notes.setdefault("correction_state", "unknown")
        existing_notes.setdefault("learning_artifact_state", "unknown")
        existing_notes.setdefault("closeout_declaration_source", "kanban_sync_default")

    first_action_epoch = min((row["created_at"] for row in event_rows), default=task_row["created_at"])
    last_activity_epoch = max((row["created_at"] for row in event_rows), default=task_row["created_at"])
    latest_run = max(run_rows, key=lambda row: row["id"], default=None)
    latest_run_id = run_id_from_kanban_run(latest_run) if latest_run else None

    review_required = 1 if any(
        "review-required" in str(parse_payload(row["payload"]).get("reason") or "").lower()
        for row in event_rows
        if row["kind"] == "blocked"
    ) else 0
    if telemetry_task_row is not None:
        review_required = max(review_required, int(telemetry_task_row["review_required"] or 0))

    telemetry_complete, telemetry_gaps_json = task_telemetry_completeness(telemetry_task_row, task_row, event_rows, run_rows)
    closeout_source = 'closeout' if (
        existing_notes.get("correction_state") is not None or existing_notes.get("learning_artifact_state") is not None
    ) else 'kanban_sync'

    telemetry_conn.execute(
        """
        UPDATE tasks
        SET created_by_profile = COALESCE(?, created_by_profile),
            provenance = CASE WHEN ? THEN COALESCE(provenance, 'real') ELSE provenance END,
            substantiality = CASE WHEN ? THEN COALESCE(substantiality, 'substantial') ELSE substantiality END,
            notes_json = ?,
            first_action_at = COALESCE(first_action_at, ?),
            last_activity_at = CASE
                WHEN last_activity_at IS NULL THEN ?
                WHEN ? > last_activity_at THEN ?
                ELSE last_activity_at
            END,
            latest_run_id = COALESCE(?, latest_run_id),
            closeout_source = ?,
            review_required = ?,
            telemetry_complete = ?,
            telemetry_gaps_json = ?
        WHERE task_id = ?
        """,
        (
            created_by_profile or None,
            int(is_completed_kanban_task(task_row)),
            int(is_completed_kanban_task(task_row)),
            json_dumps(existing_notes),
            iso_from_epoch(first_action_epoch),
            iso_from_epoch(last_activity_epoch),
            iso_from_epoch(last_activity_epoch),
            iso_from_epoch(last_activity_epoch),
            latest_run_id,
            closeout_source,
            review_required,
            telemetry_complete,
            telemetry_gaps_json,
            telemetry_task_id,
        ),
    )


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    kanban_db = Path(os.path.expanduser(args.kanban_db)).resolve()
    if not kanban_db.exists():
        raise SystemExit(f"Kanban DB not found: {kanban_db}")

    kanban_conn = sqlite3.connect(kanban_db)
    try:
        kanban_conn.row_factory = sqlite3.Row
        task_query = "SELECT * FROM tasks"
        params = ()
        if args.task_id:
            task_query += " WHERE id = ?"
            params = (args.task_id,)
        task_query += " ORDER BY created_at"
        task_rows = kanban_conn.execute(task_query, params).fetchall()

        with events_connection(telemetry_root) as telemetry_conn:
            for task_row in task_rows:
                event_rows = kanban_conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at, id",
                    (task_row["id"],),
                ).fetchall()
                run_rows = kanban_conn.execute(
                    "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, id",
                    (task_row["id"],),
                ).fetchall()
                link_rows = kanban_conn.execute(
                    "SELECT * FROM task_links WHERE parent_id = ? OR child_id = ?",
                    (task_row["id"], task_row["id"]),
                ).fetchall()
                comment_count = kanban_conn.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                    (task_row["id"],),
                ).fetchone()[0]

                ensure_task(telemetry_conn, task_row, run_rows, link_rows, comment_count)
                sync_events(telemetry_conn, task_row, event_rows, run_rows)
                sync_terminal_event(telemetry_conn, task_row)
                finalize_routing_accuracy(telemetry_conn, task_row)
                apply_kanban_skill_reuse(telemetry_conn, task_row)

                upsert_execution_runs(telemetry_conn, task_row, run_rows)
                sync_run_state_events(telemetry_conn, task_row, event_rows, run_rows)
                sync_routing_decisions(telemetry_conn, task_row, event_rows)
                sync_task_participants(telemetry_conn, task_row, event_rows, run_rows)
                update_task_hardening_fields(telemetry_conn, task_row, event_rows, run_rows)
    finally:
        kanban_conn.close()

    print(json.dumps({"synced_task_count": len(task_rows), "task_ids": [row["id"] for row in task_rows]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
