#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from common import resolve_telemetry_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List tasks with incomplete telemetry so operators can repair closeouts "
            "before trusting reports. Defaults to closed real+substantial tasks only."
        )
    )
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument(
        "--scope",
        choices=("eligible", "all_closed", "all"),
        default="eligible",
        help=(
            "eligible = closed real+substantial tasks only (default); "
            "all_closed = every closed task; all = include open tasks too"
        ),
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum tasks to print")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def load_rows(db_path: Path, scope: str, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        base_where = []
        if scope in {"eligible", "all_closed"}:
            base_where.append("closed_at IS NOT NULL")
        if scope == "eligible":
            base_where.append("lower(coalesce(provenance, '')) = 'real'")
            base_where.append("lower(coalesce(substantiality, '')) = 'substantial'")

        required_handoff_sql = """
            (task_id LIKE 'jira:%' OR task_type = 'jira_ticket' OR surface = 'direct_ticket_hub' OR notes_json LIKE '%"handoff_required": true%')
            AND (
                NOT EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_started')
                OR NOT EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_accepted')
                OR NOT EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_resolved')
                OR NOT EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_sent')
            )
        """
        where = [f"(COALESCE(telemetry_complete, 0) != 1 OR ({required_handoff_sql}))", *base_where]

        query = f"""
            SELECT
                task_id,
                title,
                status,
                owner_profile,
                provenance,
                substantiality,
                closeout_source,
                telemetry_complete,
                telemetry_gaps_json,
                opened_at,
                closed_at,
                first_action_at,
                last_activity_at,
                latest_run_id,
                (task_id LIKE 'jira:%' OR task_type = 'jira_ticket' OR surface = 'direct_ticket_hub' OR notes_json LIKE '%"handoff_required": true%') AS needs_handoff_events,
                EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_started') AS has_handoff_started,
                EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_accepted') AS has_handoff_accepted,
                EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_resolved') AS has_handoff_resolved,
                EXISTS (SELECT 1 FROM task_events te WHERE te.task_id = tasks.task_id AND te.event_type = 'handoff_sent') AS has_handoff_sent
            FROM tasks
            WHERE {' AND '.join(where)}
            ORDER BY closed_at DESC NULLS LAST, opened_at DESC NULLS LAST, task_id
            LIMIT ?
        """
        rows = [dict(row) for row in conn.execute(query, (limit,)).fetchall()]

        base_where = []
        if scope in {"eligible", "all_closed"}:
            base_where.append("closed_at IS NOT NULL")
        if scope == "eligible":
            base_where.append("lower(coalesce(provenance, '')) = 'real'")
            base_where.append("lower(coalesce(substantiality, '')) = 'substantial'")
        base_filter = f"WHERE {' AND '.join(base_where)}" if base_where else ""
        complete_filter = f"WHERE {' AND '.join([*base_where, 'COALESCE(telemetry_complete, 0) = 1'])}"
        issue_filter = f"WHERE {' AND '.join([f'(COALESCE(telemetry_complete, 0) != 1 OR ({required_handoff_sql}))', *base_where])}"
        summary_row = conn.execute(
            f"""
            SELECT
                (SELECT COUNT(*) FROM tasks {base_filter}) AS considered_tasks,
                (SELECT COUNT(*) FROM tasks {complete_filter}) AS complete_tasks,
                (SELECT COUNT(*) FROM tasks {issue_filter}) AS incomplete_tasks
            """
        ).fetchone()
        summary = dict(summary_row)
        # `complete_tasks` must use the same stricter definition as the issue
        # list: telemetry_complete plus all mandatory handoff events. Older
        # code counted telemetry_complete alone, which overstated direct-ticket
        # readiness after the handoff chain grew to include handoff_sent.
        summary["complete_tasks"] = max(0, int(summary.get("considered_tasks") or 0) - int(summary.get("incomplete_tasks") or 0))
    finally:
        conn.close()

    for row in rows:
        raw = row.get("telemetry_gaps_json")
        try:
            parsed = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            parsed = [f"unparseable:{raw}"]
        row["telemetry_gaps"] = parsed if isinstance(parsed, list) else [str(parsed)]
        if row.get("needs_handoff_events"):
            for event_name, column in (
                ("handoff_started", "has_handoff_started"),
                ("handoff_accepted", "has_handoff_accepted"),
                ("handoff_resolved", "has_handoff_resolved"),
                ("handoff_sent", "has_handoff_sent"),
            ):
                if column in row and not row.get(column):
                    row["telemetry_gaps"].append(f"missing_{event_name}")
        row["closeout_ready"] = bool(row.get("closed_at"))
        row.pop("telemetry_gaps_json", None)
    return summary, rows


def print_text(summary: dict[str, Any], rows: list[dict[str, Any]], scope: str) -> None:
    considered = int(summary.get("considered_tasks") or 0)
    complete = int(summary.get("complete_tasks") or 0)
    incomplete = int(summary.get("incomplete_tasks") or 0)
    print(f"scope={scope} considered={considered} complete={complete} incomplete={incomplete}")
    if not rows:
        print("No incomplete telemetry tasks in scope.")
        return
    print()
    for row in rows:
        gaps = ", ".join(row.get("telemetry_gaps") or []) or "none-listed"
        print(f"{row['task_id']} | {row['status']} | owner={row['owner_profile']} | closeout_source={row.get('closeout_source') or 'n/a'} | closeout_ready={str(bool(row.get('closeout_ready'))).lower()}")
        print(f"  title: {row['title']}")
        print(f"  classification: provenance={row.get('provenance') or 'unknown'} substantiality={row.get('substantiality') or 'unknown'}")
        print(f"  gaps: {gaps}")
        print(f"  opened_at={row.get('opened_at') or 'n/a'} closed_at={row.get('closed_at') or 'n/a'} latest_run_id={row.get('latest_run_id') or 'n/a'}")
        print()


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    db_path = telemetry_root / 'events.db'
    summary, rows = load_rows(db_path, args.scope, args.limit)
    if args.json:
        print(json.dumps({
            'telemetry_root': str(telemetry_root),
            'scope': args.scope,
            'summary': summary,
            'tasks': rows,
        }, indent=2, sort_keys=True))
    else:
        print_text(summary, rows, args.scope)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
