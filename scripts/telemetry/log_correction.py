#!/usr/bin/env python3
import argparse

from common import append_jsonl, events_connection, json_dumps, parse_json_input, resolve_telemetry_root, utc_now

VALID_TYPES = {
    "wrong_owner",
    "wrong_repo",
    "wrong_scope",
    "wrong_facts",
    "too_much_action",
    "too_little_action",
    "weak_verification",
    "verbosity",
    "bad_handoff",
    "format_miss",
}
VALID_SEVERITIES = {"low", "medium", "high"}
VALID_SOURCES = {"explicit_user", "implicit_inferred", "reviewer", "evaluator"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log a correction event for Hermes telemetry.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--json", help="JSON payload for the correction record")
    parser.add_argument("--task-id")
    parser.add_argument("--source", default="explicit_user")
    parser.add_argument("--profile")
    parser.add_argument("--type")
    parser.add_argument("--severity", default="medium")
    parser.add_argument("--summary")
    parser.add_argument("--occurred-at")
    parser.add_argument("--resolved", action="store_true")
    return parser.parse_args()


def build_payload(args: argparse.Namespace) -> dict:
    if args.json:
        return parse_json_input(args.json, stdin_fallback=False)
    return {
        "task_id": args.task_id,
        "source": args.source,
        "profile": args.profile,
        "correction_type": args.type,
        "severity": args.severity,
        "summary": args.summary,
        "occurred_at": args.occurred_at or utc_now(),
        "resolved": args.resolved,
    }


def validate(payload: dict) -> dict:
    for key in ["task_id", "source", "correction_type", "severity", "summary"]:
        if not payload.get(key):
            raise ValueError(f"Missing required field: {key}")
    if payload["source"] not in VALID_SOURCES:
        raise ValueError(f"Invalid source: {payload['source']}")
    if payload["correction_type"] not in VALID_TYPES:
        raise ValueError(f"Invalid correction_type: {payload['correction_type']}")
    if payload["severity"] not in VALID_SEVERITIES:
        raise ValueError(f"Invalid severity: {payload['severity']}")
    payload.setdefault("occurred_at", utc_now())
    payload["resolved"] = bool(payload.get("resolved"))
    return payload


def insert(payload: dict, telemetry_root) -> None:
    with events_connection(telemetry_root) as conn:
        conn.execute(
            """
            INSERT INTO corrections(task_id, occurred_at, source, profile, correction_type, severity, summary, resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["task_id"],
                payload["occurred_at"],
                payload["source"],
                payload.get("profile"),
                payload["correction_type"],
                payload["severity"],
                payload["summary"],
                int(payload["resolved"]),
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                payload["task_id"],
                payload["occurred_at"],
                "user_corrected",
                payload.get("profile"),
                json_dumps({
                    "source": payload["source"],
                    "correction_type": payload["correction_type"],
                    "severity": payload["severity"],
                    "summary": payload["summary"],
                    "resolved": payload["resolved"],
                }),
            ),
        )
    append_jsonl(telemetry_root, {"record_type": "correction", "logged_at": utc_now(), **payload})


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    payload = validate(build_payload(args))
    insert(payload, telemetry_root)
    print(f"Logged correction for task: {payload['task_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
