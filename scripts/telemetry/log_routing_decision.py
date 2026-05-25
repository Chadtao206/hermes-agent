#!/usr/bin/env python3
"""Log an explicit Jensen routing/substrate decision.

This is the lightweight source-of-truth hook for P1 routing telemetry: every
meaningful direct-vs-kanban or owner decision should carry a reason, not just a
later inferred owner/event. It writes the canonical `routing_decisions` row and,
when useful, a legacy-compatible `routing_events` row for older reports.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import append_jsonl, canonical_profile, events_connection, resolve_telemetry_root, utc_now

VALID_SURFACES = {"direct-ticket-hub", "kanban", "explicit-profile", "manual", "other"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Log Hermes routing/substrate decision telemetry.")
    p.add_argument("--telemetry-root", help="Override telemetry root path")
    p.add_argument("--task-id", required=True, help="Jira key, kanban task id, or durable local task id")
    p.add_argument("--initial-owner", required=True, help="Initial/default owner profile")
    p.add_argument("--decided-owner", required=True, help="Chosen owner profile or controller")
    p.add_argument("--final-owner", help="Final owner if known; defaults to decided owner")
    p.add_argument("--sequence-index", type=int, default=0, help="0 for initial route, >0 for reroute")
    p.add_argument("--reason", required=True, help="Concrete routing reason; do not leave as generic 'standard workflow'")
    p.add_argument("--ambiguity-class", default="none", help="none|repo-fit|requirements|ops|parallelism|multi-pr|review|other")
    p.add_argument("--surface", choices=sorted(VALID_SURFACES), default="direct-ticket-hub")
    p.add_argument("--correct", choices=["yes", "no", "unknown"], default="unknown", help="Whether initial owner proved correct, if known")
    p.add_argument("--evidence-source", help="Ticket/report/artifact/session backing this decision")
    p.add_argument("--source-event-id", type=int)
    p.add_argument("--source", default="jensen-routing")
    p.add_argument("--occurred-at", default=None)
    p.add_argument("--json", dest="json_payload", help="Optional extra JSON object for the JSONL audit record")
    return p.parse_args()


def correctness(raw: str) -> int | None:
    if raw == "yes":
        return 1
    if raw == "no":
        return 0
    return None


def parse_extra(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --json: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("--json must be a JSON object")
    return value


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    occurred_at = args.occurred_at or utc_now()
    initial_owner = canonical_profile(args.initial_owner)
    decided_owner = canonical_profile(args.decided_owner)
    final_owner = canonical_profile(args.final_owner or args.decided_owner)
    was_correct = correctness(args.correct)
    extra = parse_extra(args.json_payload)

    if not args.reason.strip() or len(args.reason.strip()) < 12:
        raise SystemExit("--reason must be specific enough to audit (>=12 chars)")

    record = {
        "task_id": args.task_id,
        "occurred_at": occurred_at,
        "sequence_index": args.sequence_index,
        "initial_owner": initial_owner,
        "decided_owner": decided_owner,
        "final_owner": final_owner,
        "reason": args.reason,
        "ambiguity_class": args.ambiguity_class,
        "was_initial_owner_correct": was_correct,
        "evidence_source": args.evidence_source,
        "source_event_id": args.source_event_id,
        "source": args.source,
        "surface": args.surface,
        "extra": extra,
    }

    with events_connection(telemetry_root) as conn:
        conn.execute(
            """
            INSERT INTO routing_decisions(
                task_id, occurred_at, sequence_index, initial_owner, decided_owner,
                final_owner, reason, ambiguity_class, was_initial_owner_correct,
                evidence_source, source_event_id, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, sequence_index) DO UPDATE SET
                occurred_at=excluded.occurred_at,
                initial_owner=excluded.initial_owner,
                decided_owner=excluded.decided_owner,
                final_owner=excluded.final_owner,
                reason=excluded.reason,
                ambiguity_class=excluded.ambiguity_class,
                was_initial_owner_correct=excluded.was_initial_owner_correct,
                evidence_source=excluded.evidence_source,
                source_event_id=excluded.source_event_id,
                source=excluded.source
            """,
            (
                args.task_id,
                occurred_at,
                args.sequence_index,
                initial_owner,
                decided_owner,
                final_owner,
                args.reason,
                args.ambiguity_class,
                was_correct,
                args.evidence_source,
                args.source_event_id,
                args.source,
            ),
        )
        # Legacy-compatible row for older reports/readiness tools. Keep this
        # intentionally simple; `routing_decisions` is canonical.
        conn.execute(
            """
            INSERT OR IGNORE INTO routing_events(
                task_id, occurred_at, initial_owner, current_owner, reroute_reason,
                ambiguity_class, was_initial_owner_correct, final_owner, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.task_id,
                occurred_at,
                initial_owner,
                decided_owner,
                f"{args.surface}: {args.reason}",
                args.ambiguity_class,
                was_correct,
                final_owner,
                "real",
            ),
        )

    append_jsonl(telemetry_root, record, filename="routing_decisions.jsonl")
    print(json.dumps({"ok": True, "task_id": args.task_id, "sequence_index": args.sequence_index}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1)
