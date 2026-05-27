#!/usr/bin/env python3
"""Normalize review-block telemetry into explicit block/unblock pairs.

This script is intentionally idempotent. It derives canonical
`review_blocked`/`review_unblocked` events from two source shapes already in
telemetry:

1. direct ticket closeouts that record `review_blocked` plus later remediation or
   acceptance events; and
2. kanban tasks blocked with a `review-required:` reason and later unblocked.

The normalizer keeps the original source events intact and adds compact derived
rows with `payload_json.source = review_block_normalizer`, keyed by the source
blocked event id. Reports can then count explicit review-block cycles without
mixing review waits with generic blocked-state noise.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from common import events_connection, json_dumps, resolve_telemetry_root

SOURCE = "review_block_normalizer"
REVIEW_REQUIRED_MARKER = "review-required"
RESOLUTION_EVENTS = (
    "unblocked",
    "review_unblocked",
    "remediation_pushed",
    "review_accepted",
    "handoff_accepted",
    "handoff_resolved",
    "task_completed",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive explicit review_blocked/review_unblocked pairs from existing telemetry."
    )
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_review_required_block(row: sqlite3.Row) -> bool:
    if row["event_type"] != "blocked":
        return False
    reason = str(parse_payload(row["payload_json"]).get("reason") or "").lower()
    return REVIEW_REQUIRED_MARKER in reason


def derived_event_exists(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    blocked_event_id: int,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM task_events
        WHERE task_id = ?
          AND event_type = ?
          AND json_valid(payload_json)
          AND json_extract(payload_json, '$.source') = ?
          AND CAST(json_extract(payload_json, '$.blocked_event_id') AS INTEGER) = ?
        LIMIT 1
        """,
        (task_id, event_type, SOURCE, blocked_event_id),
    ).fetchone()
    return row is not None


def any_event_after(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    occurred_after: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM task_events
        WHERE task_id = ?
          AND event_type = ?
          AND occurred_at > ?
        LIMIT 1
        """,
        (task_id, event_type, occurred_after),
    ).fetchone()
    return row is not None


def insert_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    occurred_at: str,
    event_type: str,
    profile: str | None,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO task_events(task_id, occurred_at, event_type, profile, provenance, payload_json)
        VALUES (?, ?, ?, ?, 'real', ?)
        """,
        (task_id, occurred_at, event_type, profile, json_dumps({"source": SOURCE, **payload})),
    )


def ensure_review_blocked_from_review_required_blocks(conn: sqlite3.Connection) -> int:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM task_events
        WHERE event_type = 'blocked'
        ORDER BY occurred_at, id
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        if not is_review_required_block(row):
            continue
        if derived_event_exists(
            conn,
            task_id=row["task_id"],
            event_type="review_blocked",
            blocked_event_id=int(row["id"]),
        ):
            continue
        payload = parse_payload(row["payload_json"])
        insert_event(
            conn,
            task_id=row["task_id"],
            occurred_at=row["occurred_at"],
            event_type="review_blocked",
            profile=row["profile"],
            payload={
                "blocked_event_id": int(row["id"]),
                "blocked_at": row["occurred_at"],
                "derived_from_event_type": "blocked",
                "reason": payload.get("reason"),
            },
        )
        inserted += 1
    return inserted


def first_resolution_after(conn: sqlite3.Connection, task_id: str, blocked_at: str) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in RESOLUTION_EVENTS)
    return conn.execute(
        f"""
        SELECT *
        FROM task_events
        WHERE task_id = ?
          AND occurred_at > ?
          AND event_type IN ({placeholders})
        ORDER BY occurred_at, id
        LIMIT 1
        """,
        (task_id, blocked_at, *RESOLUTION_EVENTS),
    ).fetchone()


def ensure_review_unblocks(conn: sqlite3.Connection) -> tuple[int, int]:
    conn.row_factory = sqlite3.Row
    review_blocks = conn.execute(
        """
        SELECT *
        FROM task_events
        WHERE event_type = 'review_blocked'
        ORDER BY occurred_at, id
        """
    ).fetchall()
    review_unblocked_inserted = 0
    generic_unblocked_inserted = 0
    for block in review_blocks:
        block_id = int(block["id"])
        resolution = first_resolution_after(conn, block["task_id"], block["occurred_at"])
        if resolution is None:
            continue
        resolution_payload = parse_payload(resolution["payload_json"])
        base_payload = {
            "blocked_event_id": block_id,
            "blocked_at": block["occurred_at"],
            "resolution_event_id": int(resolution["id"]),
            "resolution_event_type": resolution["event_type"],
            "resolution_at": resolution["occurred_at"],
            "reason": "review_blocked_resolved",
            "summary": resolution_payload.get("summary") or resolution_payload.get("resolution"),
            "url": resolution_payload.get("url"),
        }
        if not derived_event_exists(
            conn,
            task_id=block["task_id"],
            event_type="review_unblocked",
            blocked_event_id=block_id,
        ):
            insert_event(
                conn,
                task_id=block["task_id"],
                occurred_at=resolution["occurred_at"],
                event_type="review_unblocked",
                profile=resolution["profile"] or block["profile"],
                payload=base_payload,
            )
            review_unblocked_inserted += 1
        if not any_event_after(
            conn,
            task_id=block["task_id"],
            event_type="unblocked",
            occurred_after=block["occurred_at"],
        ) and not derived_event_exists(
            conn,
            task_id=block["task_id"],
            event_type="unblocked",
            blocked_event_id=block_id,
        ):
            insert_event(
                conn,
                task_id=block["task_id"],
                occurred_at=resolution["occurred_at"],
                event_type="unblocked",
                profile=resolution["profile"] or block["profile"],
                payload=base_payload,
            )
            generic_unblocked_inserted += 1
    return review_unblocked_inserted, generic_unblocked_inserted


def normalize(telemetry_root: Path) -> dict[str, int]:
    with events_connection(telemetry_root) as conn:
        review_blocked_inserted = ensure_review_blocked_from_review_required_blocks(conn)
        review_unblocked_inserted, generic_unblocked_inserted = ensure_review_unblocks(conn)
    return {
        "review_blocked_inserted": review_blocked_inserted,
        "review_unblocked_inserted": review_unblocked_inserted,
        "generic_unblocked_inserted": generic_unblocked_inserted,
    }


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    result = normalize(telemetry_root)
    if args.json:
        print(json.dumps({"telemetry_root": str(telemetry_root), **result}, indent=2, sort_keys=True))
    else:
        print(
            "normalized review block events: "
            f"review_blocked_inserted={result['review_blocked_inserted']} "
            f"review_unblocked_inserted={result['review_unblocked_inserted']} "
            f"generic_unblocked_inserted={result['generic_unblocked_inserted']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
