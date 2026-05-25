#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any

from common import events_connection, resolve_telemetry_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill telemetry task provenance/substantiality labels.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing labels")
    parser.add_argument(
        "--overrides-json",
        help="JSON object keyed by task_id with explicit labels, e.g. {'kanban:t_x': {'provenance':'synthetic','substantiality':'n/a'}}",
    )
    return parser.parse_args()


def load_overrides(raw: str | None) -> dict[str, dict[str, str]]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--overrides-json must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for task_id, value in parsed.items():
        if not isinstance(value, dict):
            continue
        provenance = str(value.get("provenance") or "").strip().lower()
        substantiality = str(value.get("substantiality") or "").strip().lower()
        if provenance and substantiality:
            normalized[str(task_id)] = {
                "provenance": provenance,
                "substantiality": substantiality,
            }
    return normalized


def classify_default(task_id: str, title: str) -> tuple[str | None, str | None]:
    lowered_title = (title or "").strip().lower()
    if task_id.startswith("demo-") or lowered_title.startswith("telemetry bootstrap"):
        return ("bootstrap", "n/a")
    if lowered_title.startswith("synthetic "):
        return ("synthetic", "n/a")
    if lowered_title.startswith("seed "):
        return ("seed", "n/a")
    if task_id.startswith("kanban:"):
        return ("real", "substantial")
    # Safe fallback: leave unknown so downstream report marks contamination instead of over-claiming.
    return (None, None)


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    overrides = load_overrides(args.overrides_json)

    with events_connection(telemetry_root) as conn:
        conn.row_factory = sqlite3.Row

        required = table_columns(conn, "tasks")
        if "provenance" not in required or "substantiality" not in required:
            raise RuntimeError("tasks table missing provenance/substantiality columns. Run init_self_improvement_db.py first.")

        rows = conn.execute("SELECT task_id, title, provenance, substantiality FROM tasks ORDER BY task_id").fetchall()
        updates: list[tuple[str, str, str]] = []
        for row in rows:
            task_id = row["task_id"]
            current_p = (row["provenance"] or "").strip().lower()
            current_s = (row["substantiality"] or "").strip().lower()
            if not args.force and current_p and current_s:
                continue

            target = overrides.get(task_id)
            if target:
                provenance = target["provenance"]
                substantiality = target["substantiality"]
            else:
                provenance, substantiality = classify_default(task_id, row["title"] or "")
            if provenance is None and substantiality is None:
                continue
            updates.append((provenance, substantiality, task_id))

        if args.dry_run:
            preview = [
                {"task_id": task_id, "provenance": p, "substantiality": s}
                for p, s, task_id in updates[:50]
            ]
            print(json.dumps({"dry_run": True, "update_count": len(updates), "preview": preview}, indent=2))
            return 0

        if updates:
            conn.executemany(
                "UPDATE tasks SET provenance = ?, substantiality = ? WHERE task_id = ?",
                updates,
            )

        task_lookup = "SELECT provenance FROM tasks WHERE tasks.task_id = {task_id_expr}"

        if "provenance" in table_columns(conn, "routing_events"):
            conn.execute(
                f"UPDATE routing_events SET provenance = ({task_lookup.format(task_id_expr='routing_events.task_id')}) WHERE ({task_lookup.format(task_id_expr='routing_events.task_id')}) IS NOT NULL"
            )

        if "provenance" in table_columns(conn, "corrections"):
            conn.execute(
                f"UPDATE corrections SET provenance = ({task_lookup.format(task_id_expr='corrections.task_id')}) WHERE ({task_lookup.format(task_id_expr='corrections.task_id')}) IS NOT NULL"
            )

        if "provenance" in table_columns(conn, "task_events"):
            conn.execute(
                f"UPDATE task_events SET provenance = ({task_lookup.format(task_id_expr='task_events.task_id')}) WHERE ({task_lookup.format(task_id_expr='task_events.task_id')}) IS NOT NULL"
            )

        if "provenance" in table_columns(conn, "learning_artifacts"):
            conn.execute(
                """
                UPDATE learning_artifacts
                SET provenance = (
                    SELECT provenance
                    FROM tasks
                    WHERE tasks.task_id = learning_artifacts.source_task_id
                )
                WHERE source_task_id IS NOT NULL
                  AND (
                    SELECT provenance
                    FROM tasks
                    WHERE tasks.task_id = learning_artifacts.source_task_id
                  ) IS NOT NULL
                """
            )

        counts = conn.execute(
            "SELECT provenance, substantiality, COUNT(*) FROM tasks GROUP BY provenance, substantiality ORDER BY COUNT(*) DESC"
        ).fetchall()
        print(
            json.dumps(
                {
                    "dry_run": False,
                    "updated_tasks": len(updates),
                    "counts": [list(row) for row in counts],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
