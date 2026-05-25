#!/usr/bin/env python3
import argparse

from common import append_jsonl, events_connection, json_dumps, parse_json_input, resolve_telemetry_root, utc_now

VALID_TYPES = {"memory", "fact", "skill"}
VALID_QUALITY = {"promising", "useful", "noisy", "stale", "high_value"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log a learning artifact for Hermes telemetry.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--json", help="JSON payload for the artifact record")
    parser.add_argument("--type")
    parser.add_argument("--key")
    parser.add_argument("--source-profile")
    parser.add_argument("--source-task-id")
    parser.add_argument("--topic")
    parser.add_argument("--created-at")
    parser.add_argument("--reused-count", type=int, default=0)
    parser.add_argument("--last-reused-at")
    parser.add_argument("--contradicted", action="store_true")
    parser.add_argument("--archived", action="store_true")
    parser.add_argument("--quality-label")
    parser.add_argument("--notes-json")
    return parser.parse_args()


def build_payload(args: argparse.Namespace) -> dict:
    if args.json:
        return parse_json_input(args.json, stdin_fallback=False)
    notes = {}
    if args.notes_json:
        import json
        notes = json.loads(args.notes_json)
    return {
        "artifact_type": args.type,
        "artifact_key": args.key,
        "source_profile": args.source_profile,
        "source_task_id": args.source_task_id,
        "topic": args.topic,
        "created_at": args.created_at or utc_now(),
        "reused_count": args.reused_count,
        "last_reused_at": args.last_reused_at,
        "contradicted": args.contradicted,
        "archived": args.archived,
        "quality_label": args.quality_label,
        "notes": notes,
    }


def validate(payload: dict) -> dict:
    for key in ["artifact_type", "artifact_key", "source_profile"]:
        if not payload.get(key):
            raise ValueError(f"Missing required field: {key}")
    if payload["artifact_type"] not in VALID_TYPES:
        raise ValueError(f"Invalid artifact_type: {payload['artifact_type']}")
    if payload.get("quality_label") and payload["quality_label"] not in VALID_QUALITY:
        raise ValueError(f"Invalid quality_label: {payload['quality_label']}")
    payload.setdefault("created_at", utc_now())
    payload.setdefault("reused_count", 0)
    payload.setdefault("notes", {})
    payload["contradicted"] = bool(payload.get("contradicted"))
    payload["archived"] = bool(payload.get("archived"))
    return payload


def upsert(payload: dict, telemetry_root) -> None:
    with events_connection(telemetry_root) as conn:
        conn.execute(
            """
            INSERT INTO learning_artifacts(
                artifact_type, artifact_key, created_at, source_profile, source_task_id, topic,
                reused_count, last_reused_at, contradicted, archived, quality_label, notes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_type, artifact_key) DO UPDATE SET
                source_profile = excluded.source_profile,
                source_task_id = COALESCE(excluded.source_task_id, learning_artifacts.source_task_id),
                topic = COALESCE(excluded.topic, learning_artifacts.topic),
                reused_count = excluded.reused_count,
                last_reused_at = COALESCE(excluded.last_reused_at, learning_artifacts.last_reused_at),
                contradicted = excluded.contradicted,
                archived = excluded.archived,
                quality_label = COALESCE(excluded.quality_label, learning_artifacts.quality_label),
                notes_json = excluded.notes_json
            """,
            (
                payload["artifact_type"],
                payload["artifact_key"],
                payload["created_at"],
                payload["source_profile"],
                payload.get("source_task_id"),
                payload.get("topic"),
                payload["reused_count"],
                payload.get("last_reused_at"),
                int(payload["contradicted"]),
                int(payload["archived"]),
                payload.get("quality_label"),
                json_dumps(payload["notes"]),
            ),
        )
        if payload.get("source_task_id"):
            conn.execute(
                "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    payload["source_task_id"],
                    payload["created_at"],
                    "learning_artifact_logged",
                    payload["source_profile"],
                    json_dumps({
                        "artifact_type": payload["artifact_type"],
                        "artifact_key": payload["artifact_key"],
                        "quality_label": payload.get("quality_label"),
                    }),
                ),
            )
    append_jsonl(telemetry_root, {"record_type": "learning_artifact", "logged_at": utc_now(), **payload})


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    payload = validate(build_payload(args))
    upsert(payload, telemetry_root)
    print(f"Logged learning artifact: {payload['artifact_type']} {payload['artifact_key']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
