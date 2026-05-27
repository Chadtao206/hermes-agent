#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import resolve_telemetry_root

SOURCE = "operator_backfill:role_title_match_completed_lane_v2"
REPORT_PREFIX = "routing_correctness_backfill_v2"

OWNER_PATTERNS: dict[str, list[str]] = {
    "reviewer": [r"\breview\b", r"\bboris\b", r"\bgate\b", r"\breadiness\b"],
    "engineer": [
        r"\bandrej\b",
        r"\bimpl\b",
        r"\bimplement\b",
        r"\bimplementation\b",
        r"\bfix\b",
        r"\bremediate\b",
        r"\brepair\b",
        r"\bpackage\b",
        r"\btechnical\b",
        r"\bprototype\b",
        r"\bcode\b",
    ],
    "ops": [
        r"\bops\b",
        r"\bgrace\b",
        r"\bwatchdog\b",
        r"\bcron\b",
        r"\bschedule\b",
        r"\bruntime\b",
        r"\bdiagnose\b",
        r"\brollback\b",
        r"\bdeploy\b",
        r"\bstaging\b",
        r"\bmonitor\b",
        r"\bsafety\b",
    ],
    "researcher": [
        r"\bresearch\b",
        r"\bmorty\b",
        r"\bevidence\b",
        r"\bdiscovery\b",
        r"\bsource\b",
        r"\bmap\b",
        r"\bpreflight\b",
        r"\banalysis\b",
        r"\binvestigate\b",
    ],
    "designer": [r"\bdesign\b", r"\biris\b", r"\bux\b", r"\bui\b", r"\bvisual\b", r"\bpreflight\b"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conservatively backfill explicit routing correctness for completed kanban lanes."
    )
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--days", type=int, default=7, help="Only consider tasks closed in the last N days")
    parser.add_argument(
        "--owners",
        default="reviewer,engineer,ops,researcher,designer",
        help="Comma-separated owner profiles to consider",
    )
    parser.add_argument("--limit", type=int, default=500, help="Maximum candidates to consider")
    parser.add_argument("--dry-run", action="store_true", help="Write only the report; do not insert rows")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    return parser.parse_args()


def canonical(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    aliases = {
        "andrej": "engineer",
        "boris": "reviewer",
        "grace": "ops",
        "morty": "researcher",
        "iris": "designer",
        "jensen": "default",
    }
    return aliases.get(text, text) or None


def title_match(owner: str, title: str) -> str | None:
    text = title.lower()
    for pattern in OWNER_PATTERNS.get(owner, []):
        if re.search(pattern, text):
            return pattern
    return None


def load_candidates(conn: sqlite3.Connection, owners: set[str], days: int, limit: int) -> list[sqlite3.Row]:
    owner_placeholders = ",".join("?" for _ in owners)
    params: list[Any] = [f"-{days} days", *sorted(owners), limit]
    return conn.execute(
        f"""
        WITH known AS (
          SELECT task_id, MAX(CASE WHEN was_initial_owner_correct IS NOT NULL THEN 1 ELSE 0 END) known
          FROM (
            SELECT task_id, was_initial_owner_correct FROM routing_decisions
            UNION ALL
            SELECT task_id, was_initial_owner_correct FROM routing_events
          ) GROUP BY task_id
        ), routed AS (
          SELECT DISTINCT task_id FROM routing_decisions
          UNION
          SELECT DISTINCT task_id FROM routing_events
        ), rd AS (
          SELECT task_id,
                 COUNT(*) rd_count,
                 MAX(COALESCE(sequence_index, 0)) max_sequence,
                 GROUP_CONCAT(DISTINCT initial_owner) initial_owners,
                 GROUP_CONCAT(DISTINCT decided_owner) decided_owners,
                 GROUP_CONCAT(DISTINCT final_owner) final_owners,
                 MIN(ambiguity_class) ambiguity_class,
                 MIN(id) first_decision_id
          FROM routing_decisions
          GROUP BY task_id
        ), re AS (
          SELECT task_id,
                 COUNT(*) re_count,
                 GROUP_CONCAT(DISTINCT initial_owner) event_initial_owners,
                 GROUP_CONCAT(DISTINCT current_owner) event_current_owners,
                 GROUP_CONCAT(DISTINCT final_owner) event_final_owners,
                 MIN(ambiguity_class) event_ambiguity_class
          FROM routing_events
          GROUP BY task_id
        )
        SELECT t.*, rd.*, re.*
        FROM tasks t
        JOIN routed USING(task_id)
        LEFT JOIN known USING(task_id)
        LEFT JOIN rd USING(task_id)
        LEFT JOIN re USING(task_id)
        WHERE t.task_id LIKE 'kanban:%'
          AND t.closed_at IS NOT NULL
          AND datetime(substr(t.closed_at, 1, 19)) >= datetime('now', ?)
          AND lower(coalesce(t.provenance, '')) = 'real'
          AND lower(coalesce(t.substantiality, '')) = 'substantial'
          AND lower(coalesce(t.outcome, '')) = 'success'
          AND lower(coalesce(t.owner_profile, '')) IN ({owner_placeholders})
          AND COALESCE(known.known, 0) = 0
        ORDER BY CASE lower(t.owner_profile)
                   WHEN 'reviewer' THEN 0
                   WHEN 'engineer' THEN 1
                   WHEN 'ops' THEN 2
                   WHEN 'researcher' THEN 3
                   WHEN 'designer' THEN 4
                   ELSE 9
                 END,
                 t.closed_at DESC,
                 t.task_id
        LIMIT ?
        """,
        params,
    ).fetchall()


def split_distinct(value: Any) -> set[str]:
    if value is None:
        return set()
    return {item.strip() for item in str(value).split(",") if item.strip()}


def route_consistent(row: sqlite3.Row, owner: str) -> tuple[bool, str | None]:
    owner = canonical(owner) or owner
    checks = [
        ("initial_owners", False),
        ("decided_owners", False),
        ("final_owners", True),
        ("event_initial_owners", False),
        ("event_current_owners", False),
        ("event_final_owners", True),
    ]
    for field, nullable_ok in checks:
        values = {canonical(item) for item in split_distinct(row[field])}
        values.discard(None)
        if not values:
            if nullable_ok:
                continue
            # Some tasks only have v1 or v2 routing; missing counterpart is fine.
            if field.startswith("event_") and row["rd_count"]:
                continue
            if not field.startswith("event_") and row["re_count"]:
                continue
            return False, f"missing_{field}"
        if values != {owner}:
            return False, f"inconsistent_{field}:{sorted(values)}"
    return True, None


def insert_backfill(conn: sqlite3.Connection, row: sqlite3.Row, owner: str, pattern: str) -> int:
    sequence_index = int(row["max_sequence"] or 0) + 1
    ambiguity_class = row["ambiguity_class"] or row["event_ambiguity_class"] or "lane_owner_backfill"
    cur = conn.execute(
        """
        INSERT INTO routing_decisions (
            task_id, occurred_at, sequence_index, initial_owner, decided_owner, final_owner,
            reason, ambiguity_class, was_initial_owner_correct, evidence_source, source_event_id, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'operator_backfill')
        """,
        (
            row["task_id"],
            row["closed_at"],
            sequence_index,
            owner,
            owner,
            owner,
            f"routing correctness backfill: completed {owner} lane matched title pattern {pattern}",
            ambiguity_class,
            SOURCE,
            row["first_decision_id"],
        ),
    )
    return int(cur.lastrowid)


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    db_path = telemetry_root / "events.db"
    reports_dir = telemetry_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    owners: set[str] = {canonical(item) or "" for item in args.owners.split(",") if item.strip()}
    owners.discard("")

    generated_at = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "generated_at": generated_at,
        "db": str(db_path),
        "source": SOURCE,
        "dry_run": bool(args.dry_run),
        "days": args.days,
        "owners": sorted(owners),
        "candidate_count": 0,
        "backfilled_count": 0,
        "skipped_count": 0,
        "backfilled": [],
        "skipped": [],
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        candidates = load_candidates(conn, owners, args.days, args.limit)
        report["candidate_count"] = len(candidates)
        for row in candidates:
            owner = canonical(row["owner_profile"])
            title = row["title"] or ""
            pattern = title_match(owner or "", title)
            consistent, reason = route_consistent(row, owner or "")
            base = {
                "task_id": row["task_id"],
                "title": row["title"],
                "owner_profile": row["owner_profile"],
                "closed_at": row["closed_at"],
                "matched_pattern": pattern,
                "routing_decision_count": int(row["rd_count"] or 0),
                "routing_event_count": int(row["re_count"] or 0),
            }
            if not pattern:
                report["skipped"].append({**base, "reason": "no_owner_title_pattern"})
                continue
            if not consistent:
                report["skipped"].append({**base, "reason": reason})
                continue
            if args.dry_run:
                report["backfilled"].append({**base, "routing_decision_id": None})
            else:
                decision_id = insert_backfill(conn, row, owner or str(row["owner_profile"]), pattern)
                report["backfilled"].append({**base, "routing_decision_id": decision_id})
        report["backfilled_count"] = len(report["backfilled"])
        report["skipped_count"] = len(report["skipped"])
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    stamp = generated_at.replace(":", "").replace("+", "Z").replace(".", "_")
    report_path = reports_dir / f"{REPORT_PREFIX}_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["report_path"] = str(report_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"wrote {report_path}")
        print(f"candidates={report['candidate_count']} backfilled={report['backfilled_count']} skipped={report['skipped_count']} dry_run={report['dry_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
