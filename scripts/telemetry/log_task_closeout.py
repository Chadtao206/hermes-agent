#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from common import append_jsonl, canonical_profile, canonical_profiles, events_connection, json_dumps, parse_json_input, resolve_telemetry_root, utc_now

VALID_OUTCOMES = {"success", "partial", "fail"}
VALID_STATUSES = {"open", "completed", "partial", "failed", "cancelled"}
VALID_VERIFICATION = {"none", "weak", "moderate", "strong"}
# Closeout records must declare presence/absence explicitly. ``unknown``
# means the operator did not review and wants that recorded as an
# absence-of-evidence (distinct from "none observed").
VALID_ABSENCE_STATES = {"present", "none", "unknown"}
# Provenance / substantiality classify whether a task is eligible for
# workflow-metrics KPIs (see export_weekly_report.py eligible filter).
# "unknown" is a first-class value so non-kanban callers that omit the
# fields still produce a normalized declaration instead of NULL.
VALID_PROVENANCE = {"real", "synthetic", "bootstrap", "seed", "unknown"}
VALID_SUBSTANTIALITY = {"substantial", "trivial", "n/a", "unknown"}

CLOSEOUT_ROUTING_REASON = "closeout"
REUSE_NOTE_KEYS = {
    "memory": ("memories_used", "memory_keys_used"),
    "fact": ("facts_used", "fact_keys_used"),
    "skill": ("skills_used", "skill_keys_used"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log a Hermes task closeout to telemetry storage.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--json", help="JSON payload for the closeout record")
    parser.add_argument("--task-id")
    parser.add_argument("--title")
    parser.add_argument("--summary")
    parser.add_argument("--owner-profile")
    parser.add_argument("--surface", default="cli")
    parser.add_argument("--status", default="completed")
    parser.add_argument("--outcome", default="success")
    parser.add_argument("--verification-strength", default="moderate")
    parser.add_argument("--opened-at")
    parser.add_argument("--closed-at")
    parser.add_argument("--session-id")
    parser.add_argument("--kanban-task-id")
    parser.add_argument("--task-type")
    parser.add_argument("--workdir")
    parser.add_argument("--repo-hint")
    parser.add_argument("--reopened", action="store_true")
    parser.add_argument("--final-confidence", type=float)
    parser.add_argument("--assisting-profiles", help="JSON array or comma-separated string")
    parser.add_argument("--notes-json", help="Freeform JSON object for extra metadata")
    parser.add_argument("--turn-count", type=int)
    parser.add_argument("--tool-calls", type=int)
    parser.add_argument("--tokens-in", type=int)
    parser.add_argument("--tokens-out", type=int)
    parser.add_argument("--memory-saved-count", type=int, default=0)
    parser.add_argument("--fact-saved-count", type=int, default=0)
    parser.add_argument("--skill-saved-or-patched-count", type=int, default=0)
    parser.add_argument("--correct-owner")
    parser.add_argument("--user-corrected")
    parser.add_argument(
        "--correction-state",
        choices=sorted(VALID_ABSENCE_STATES),
        default=None,
        help=(
            "Explicit presence/absence of corrections discovered during closeout. "
            "'none' records that the operator looked and found nothing — distinct "
            "from omitting the flag, which is treated as 'unknown'."
        ),
    )
    parser.add_argument(
        "--learning-artifact-state",
        choices=sorted(VALID_ABSENCE_STATES),
        default=None,
        help=(
            "Explicit presence/absence of learning artifacts produced/reused for this "
            "closeout. 'none' records that the operator looked and found nothing."
        ),
    )
    parser.add_argument(
        "--provenance",
        choices=sorted(VALID_PROVENANCE),
        default=None,
        help=(
            "Provenance label persisted to tasks.provenance. Workflow-metrics "
            "eligibility filters on 'real'; defaults to 'unknown' when omitted."
        ),
    )
    parser.add_argument(
        "--substantiality",
        choices=sorted(VALID_SUBSTANTIALITY),
        default=None,
        help=(
            "Substantiality label persisted to tasks.substantiality. Workflow-metrics "
            "eligibility filters on 'substantial'; defaults to 'unknown' when omitted."
        ),
    )
    return parser.parse_args()


def normalize_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def normalize_profiles(value):
    if value is None:
        return []
    if isinstance(value, list):
        return canonical_profiles(value)
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("assisting_profiles JSON must be an array")
        return canonical_profiles(parsed)
    return canonical_profiles(item.strip() for item in text.split(",") if item.strip())


def build_payload(args: argparse.Namespace) -> dict:
    if args.json:
        payload = parse_json_input(args.json, stdin_fallback=False)
    else:
        payload = {
            "task_id": args.task_id,
            "title": args.title,
            "user_goal_summary": args.summary,
            "owner_profile": args.owner_profile,
            "surface": args.surface,
            "status": args.status,
            "outcome": args.outcome,
            "verification_strength": args.verification_strength,
            "opened_at": args.opened_at,
            "closed_at": args.closed_at,
            "session_id": args.session_id,
            "kanban_task_id": args.kanban_task_id,
            "task_type": args.task_type,
            "workdir": args.workdir,
            "repo_hint": args.repo_hint,
            "reopened": args.reopened,
            "final_confidence": args.final_confidence,
            "assisting_profiles": normalize_profiles(args.assisting_profiles),
            "notes": json.loads(args.notes_json) if args.notes_json else {},
            "turn_count": args.turn_count,
            "tool_calls": args.tool_calls,
            "tokens_in": args.tokens_in,
            "tokens_out": args.tokens_out,
            "memory_saved_count": args.memory_saved_count,
            "fact_saved_count": args.fact_saved_count,
            "skill_saved_or_patched_count": args.skill_saved_or_patched_count,
            "correct_owner": normalize_bool(args.correct_owner),
            "user_corrected": normalize_bool(args.user_corrected),
            "correction_state": args.correction_state,
            "learning_artifact_state": args.learning_artifact_state,
            "provenance": args.provenance,
            "substantiality": args.substantiality,
        }
    return payload


def validate(payload: dict) -> dict:
    required = ["task_id", "title", "user_goal_summary", "owner_profile"]
    for key in required:
        if not payload.get(key):
            raise ValueError(f"Missing required field: {key}")

    payload.setdefault("surface", "cli")
    payload.setdefault("status", "completed")
    payload.setdefault("outcome", "success")
    payload.setdefault("verification_strength", "moderate")
    if not payload.get("opened_at"):
        payload["opened_at"] = utc_now()
    if not payload.get("closed_at"):
        payload["closed_at"] = utc_now()
    payload.setdefault("assisting_profiles", [])
    payload.setdefault("reopened", False)
    payload.setdefault("notes", {})
    payload.setdefault("memory_saved_count", 0)
    payload.setdefault("fact_saved_count", 0)
    payload.setdefault("skill_saved_or_patched_count", 0)

    if payload["outcome"] not in VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome: {payload['outcome']}")
    if payload["status"] not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {payload['status']}")
    if payload["verification_strength"] not in VALID_VERIFICATION:
        raise ValueError(f"Invalid verification strength: {payload['verification_strength']}")

    payload["owner_profile"] = canonical_profile(payload.get("owner_profile"))
    payload["assisting_profiles"] = normalize_profiles(payload.get("assisting_profiles"))
    notes = payload.get("notes")
    if isinstance(notes, dict):
        for key in ("initial_owner", "current_owner", "routed_from", "routed_to", "created_by", "reviewer"):
            if notes.get(key):
                notes[key] = canonical_profile(notes[key]) if key != "reviewer" else canonical_profile(notes[key], default="reviewer")
    reopened_flag = normalize_bool(payload.get("reopened"))
    payload["reopened"] = bool(reopened_flag) if reopened_flag is not None else False
    payload["correct_owner"] = normalize_bool(payload.get("correct_owner"))
    payload["user_corrected"] = normalize_bool(payload.get("user_corrected"))
    # Default explicit-absence states to "unknown" so closeouts always record
    # *some* declaration. Silent omission (the prior behavior) made it
    # impossible to distinguish "operator looked, found nothing" from
    # "operator never checked" downstream.
    for key in ("correction_state", "learning_artifact_state"):
        value = payload.get(key)
        if value is None or value == "":
            payload[key] = "unknown"
        elif value not in VALID_ABSENCE_STATES:
            raise ValueError(f"Invalid {key}: {value}")
    # Provenance / substantiality default to "unknown" for non-kanban callers
    # that haven't been updated yet. Kanban closeout (hermes_cli/kanban.py)
    # injects "real"/"substantial" explicitly so completed kanban tasks become
    # workflow-metrics eligible without manual flag-flipping.
    _normalize_classification(payload, "provenance", VALID_PROVENANCE)
    _normalize_classification(payload, "substantiality", VALID_SUBSTANTIALITY)
    if payload.get("notes") is None:
        payload["notes"] = {}
    return payload


def _normalize_classification(payload: dict, key: str, valid: set) -> None:
    value = payload.get(key)
    if value is None or value == "":
        payload[key] = "unknown"
        return
    lowered = str(value).strip().lower()
    if lowered not in valid:
        raise ValueError(f"Invalid {key}: {value}")
    payload[key] = lowered


def _coerce_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if item is not None and str(item).strip()]
            except json.JSONDecodeError:
                pass
        return [chunk.strip() for chunk in text.split(",") if chunk.strip()]
    return []


def write_routing_from_closeout(conn, payload: dict, notes: dict) -> None:
    initial_owner = notes.get("initial_owner") or notes.get("routed_from") or payload["owner_profile"]
    current_owner = notes.get("current_owner") or notes.get("routed_to") or payload["owner_profile"]
    correct = notes.get("correct_owner")
    user_corrected = notes.get("user_corrected")
    if isinstance(correct, bool):
        flag = 1 if correct else 0
    elif user_corrected is True:
        flag = 0
    elif user_corrected is False:
        flag = 1
    elif initial_owner != current_owner:
        # Explicit reroute evidence: initial owner was not correct.
        flag = 0
    else:
        # No explicit routing evidence; keep unknown instead of assuming correct.
        flag = None
    final_owner = current_owner if payload["outcome"] == "success" else None
    existing = conn.execute(
        "SELECT id FROM routing_events WHERE task_id = ? AND reroute_reason = ?",
        (payload["task_id"], CLOSEOUT_ROUTING_REASON),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE routing_events
            SET occurred_at = ?, initial_owner = ?, current_owner = ?, was_initial_owner_correct = ?, final_owner = ?
            WHERE id = ?
            """,
            (payload["closed_at"], initial_owner, current_owner, flag, final_owner, existing[0]),
        )
    else:
        conn.execute(
            """
            INSERT INTO routing_events(task_id, occurred_at, initial_owner, current_owner, reroute_reason, ambiguity_class, was_initial_owner_correct, final_owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["task_id"],
                payload["closed_at"],
                initial_owner,
                current_owner,
                CLOSEOUT_ROUTING_REASON,
                payload.get("surface"),
                flag,
                final_owner,
            ),
        )


_ABSENCE_EVENT_TYPES = {
    "correction_state": "correction_absent_declared",
    "learning_artifact_state": "learning_artifact_absent_declared",
}


def write_absence_events(conn, payload: dict) -> None:
    """Record explicit absence/unknown declarations as durable task_events.

    A closeout that says "no correction observed" is operationally
    different from a closeout that simply omits the field. Persist that
    declaration here so downstream queries can count "operator checked
    and found none" without inferring from silence.
    """
    for field, event_type in _ABSENCE_EVENT_TYPES.items():
        state = payload.get(field)
        # Always clear prior closeout-sourced absence events first — otherwise
        # re-running closeout with state=present would leave stale "absent"
        # markers from an earlier none/unknown declaration.
        conn.execute(
            """
            DELETE FROM task_events
            WHERE task_id = ?
              AND event_type = ?
              AND COALESCE(json_extract(payload_json, '$.source'), 'closeout') = 'closeout'
            """,
            (payload["task_id"], event_type),
        )
        if state not in {"none", "unknown"}:
            continue
        conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                payload["task_id"],
                payload["closed_at"],
                event_type,
                payload["owner_profile"],
                json_dumps({"source": "closeout", "state": state, "field": field}),
            ),
        )


def write_handoff_events_from_closeout(conn, payload: dict, notes: dict) -> None:
    """Emit explicit handoff telemetry when generic closeout carries PR/ticket context.

    Direct ticket-hub closeout (`log_ticket_closeout.py`) emits richer ticket-specific
    handoff events itself, so skip those to avoid duplicate source streams. Generic
    closeout paths, including kanban closeout, can opt in by carrying ticket/PR notes.
    """
    if notes.get("source") == "ticket_closeout":
        return
    ticket_key = notes.get("ticket_key") or notes.get("jira_key")
    pr_url = notes.get("pr_url")
    acceptance_url = notes.get("accepted_comment_url") or notes.get("acceptance_artifact_url")
    reviewed_commit = notes.get("reviewed_commit") or notes.get("final_commit")
    if not ticket_key and not pr_url and not notes.get("handoff_required"):
        return
    conn.execute(
        """
        DELETE FROM task_events
        WHERE task_id = ?
          AND event_type IN ('handoff_started', 'handoff_accepted', 'handoff_resolved')
          AND COALESCE(json_extract(payload_json, '$.source'), 'closeout') = 'closeout'
        """,
        (payload["task_id"],),
    )
    base = {
        "source": "closeout",
        "ticket_key": ticket_key,
        "pr_url": pr_url,
        "reviewed_commit": reviewed_commit,
    }
    conn.execute(
        "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json, provenance) VALUES (?, ?, 'handoff_started', ?, ?, 'real')",
        (payload["task_id"], payload["opened_at"], payload["owner_profile"], json_dumps({**base, "handoff_target": "human_engineering_review"})),
    )
    if acceptance_url or reviewed_commit:
        conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json, provenance) VALUES (?, ?, 'handoff_accepted', 'reviewer', ?, 'real')",
            (payload["task_id"], payload["closed_at"], json_dumps({**base, "url": acceptance_url, "accepted_by": notes.get("reviewer") or "Boris/reviewer"})),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json, provenance) VALUES (?, ?, 'handoff_resolved', ?, ?, 'real')",
            (payload["task_id"], payload["closed_at"], payload["owner_profile"], json_dumps({**base, "resolution": "ready_for_human_review_notification"})),
        )


def apply_reuse_signals(conn, payload: dict, notes: dict) -> None:
    profile = payload["owner_profile"]
    when = payload["closed_at"]
    for artifact_type, note_keys in REUSE_NOTE_KEYS.items():
        seen: set[str] = set()
        candidates: list[str] = []
        for key in note_keys:
            for item in _coerce_str_list(notes.get(key)):
                if item not in seen:
                    seen.add(item)
                    candidates.append(item)
        for artifact_key in candidates:
            event_payload = json_dumps({"artifact_type": artifact_type, "artifact_key": artifact_key})
            already = conn.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id = ? AND event_type = 'learning_artifact_reused'
                  AND json_extract(payload_json, '$.artifact_type') = ?
                  AND json_extract(payload_json, '$.artifact_key') = ?
                """,
                (
                    payload["task_id"],
                    artifact_type,
                    artifact_key,
                ),
            ).fetchone()
            if already:
                continue
            cur = conn.execute(
                """
                UPDATE learning_artifacts
                SET last_reused_at = ?, reused_count = reused_count + 1
                WHERE artifact_type = ? AND artifact_key = ?
                """,
                (when, artifact_type, artifact_key),
            )
            conn.execute(
                "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    payload["task_id"],
                    when,
                    "learning_artifact_reused",
                    profile,
                    event_payload if cur.rowcount > 0 else json_dumps({"artifact_type": artifact_type, "artifact_key": artifact_key, "matched": False}),
                ),
            )


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def closeout_run_id(task_id: str) -> str:
    return f"closeout:{task_id}"


def terminal_state_for_payload(payload: dict) -> str:
    status = str(payload.get("status") or "").lower()
    outcome = str(payload.get("outcome") or "").lower()
    if status == "cancelled":
        return "cancelled"
    if status == "failed" or outcome == "fail":
        return "failed"
    return "completed"


def upsert_normalized_closeout(conn, payload: dict, notes: dict) -> None:
    if not table_exists(conn, "execution_runs"):
        return

    task_id = payload["task_id"]
    run_id = closeout_run_id(task_id)
    metadata_json = json_dumps({
        "summary": payload["user_goal_summary"],
        "verification_strength": payload["verification_strength"],
        "assisting_profiles": payload["assisting_profiles"],
        "surface": payload.get("surface"),
    })
    conn.execute(
        """
        INSERT INTO execution_runs(
            task_id, run_id, profile, status, outcome,
            started_at, ended_at, summary, error, metadata_json, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closeout')
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
            task_id,
            run_id,
            payload["owner_profile"],
            payload["status"],
            payload["outcome"],
            payload["opened_at"],
            payload["closed_at"],
            payload["user_goal_summary"],
            None if payload["outcome"] == "success" else payload["user_goal_summary"],
            metadata_json,
        ),
    )

    state_details = json_dumps({"source": "closeout", "status": payload["status"], "outcome": payload["outcome"]})
    conn.execute(
        "DELETE FROM run_state_events WHERE task_id = ? AND run_id = ? AND source = 'closeout'",
        (task_id, run_id),
    )
    conn.execute(
        "INSERT INTO run_state_events(task_id, run_id, occurred_at, state, profile, details_json, source) VALUES (?, ?, ?, 'started', ?, ?, 'closeout')",
        (task_id, run_id, payload["opened_at"], payload["owner_profile"], state_details),
    )
    conn.execute(
        "INSERT INTO run_state_events(task_id, run_id, occurred_at, state, profile, details_json, source) VALUES (?, ?, ?, ?, ?, ?, 'closeout')",
        (task_id, run_id, payload["closed_at"], terminal_state_for_payload(payload), payload["owner_profile"], state_details),
    )

    if table_exists(conn, "routing_decisions"):
        initial_owner = notes.get("initial_owner") or payload["owner_profile"]
        current_owner = notes.get("current_owner") or payload["owner_profile"]
        correct = notes.get("correct_owner")
        user_corrected = notes.get("user_corrected")
        if isinstance(correct, bool):
            correctness = 1 if correct else 0
            evidence_source = "explicit"
        elif user_corrected is True:
            correctness = 0
            evidence_source = "user_corrected"
        elif user_corrected is False:
            correctness = 1
            evidence_source = "user_corrected"
        elif initial_owner != current_owner:
            correctness = 0
            evidence_source = "reroute"
        else:
            correctness = None
            evidence_source = None
        existing = conn.execute(
            "SELECT sequence_index FROM routing_decisions WHERE task_id = ? AND source = 'closeout' ORDER BY sequence_index DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if existing:
            sequence_index = existing[0]
            conn.execute(
                """
                UPDATE routing_decisions
                SET occurred_at = ?, initial_owner = ?, decided_owner = ?, final_owner = ?,
                    reason = ?, ambiguity_class = ?, was_initial_owner_correct = ?, evidence_source = ?, source_event_id = NULL
                WHERE task_id = ? AND sequence_index = ? AND source = 'closeout'
                """,
                (
                    payload["closed_at"],
                    initial_owner,
                    current_owner,
                    current_owner if payload["outcome"] == "success" else None,
                    'closeout',
                    payload.get("surface"),
                    correctness,
                    evidence_source,
                    task_id,
                    sequence_index,
                ),
            )
        else:
            current_max = conn.execute(
                "SELECT COALESCE(MAX(sequence_index), -1) FROM routing_decisions WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            sequence_index = current_max + 1
            conn.execute(
                """
                INSERT INTO routing_decisions(
                    task_id, occurred_at, sequence_index, initial_owner, decided_owner, final_owner,
                    reason, ambiguity_class, was_initial_owner_correct, evidence_source, source_event_id, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'closeout')
                """,
                (
                    task_id,
                    payload["closed_at"],
                    sequence_index,
                    initial_owner,
                    current_owner,
                    current_owner if payload["outcome"] == "success" else None,
                    'closeout',
                    payload.get("surface"),
                    correctness,
                    evidence_source,
                ),
            )

    if table_exists(conn, "task_participants"):
        participants = []
        owner = payload["owner_profile"]
        participants.append((owner, "owner", payload["opened_at"], payload["closed_at"]))
        participants.append((owner, "final_owner", payload["closed_at"], payload["closed_at"]))
        for profile in payload.get("assisting_profiles", []):
            if profile and profile != owner:
                participants.append((profile, "assisting", payload["opened_at"], payload["closed_at"]))
        for profile, role, first_seen_at, last_seen_at in participants:
            conn.execute(
                """
                INSERT INTO task_participants(task_id, profile, role, first_seen_at, last_seen_at, source)
                VALUES (?, ?, ?, ?, ?, 'closeout')
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
                (task_id, profile, role, first_seen_at, last_seen_at),
            )

    if table_exists(conn, "tasks"):
        gaps = []
        # validate() already coerces missing absence-state fields to "unknown",
        # which is itself a real declaration ("operator did not check"). Treat
        # only literally-missing notes as a completeness gap so the default
        # kanban closeout (which always carries correction/learning state
        # after validation) closes the gap without manual flags.
        if notes.get("correction_state") is None:
            gaps.append("correction_state")
        if notes.get("learning_artifact_state") is None:
            gaps.append("learning_artifact_state")
        if not notes.get("initial_owner") or not notes.get("current_owner"):
            gaps.append("routing_context")
        # Provenance / substantiality count as a gap when missing OR when the
        # caller explicitly recorded "unknown" — those tasks can't enter the
        # workflow-metrics eligible set (which requires real + substantial),
        # so telemetry_complete must reflect that they need follow-up labelling.
        if str(payload.get("provenance") or "").lower() in {"", "unknown"}:
            gaps.append("provenance")
        if str(payload.get("substantiality") or "").lower() in {"", "unknown"}:
            gaps.append("substantiality")
        telemetry_complete = 1 if not gaps else 0
        conn.execute(
            """
            UPDATE tasks
            SET first_action_at = COALESCE(first_action_at, ?),
                last_activity_at = ?,
                latest_run_id = ?,
                closeout_source = 'closeout',
                telemetry_complete = ?,
                telemetry_gaps_json = ?,
                review_required = COALESCE(review_required, 0)
            WHERE task_id = ?
            """,
            (
                payload["opened_at"],
                payload["closed_at"],
                run_id,
                telemetry_complete,
                json_dumps(gaps),
                task_id,
            ),
        )


def upsert_task(payload: dict, telemetry_root: Path) -> None:
    notes = dict(payload.get("notes", {}))
    for key in [
        "turn_count",
        "tool_calls",
        "tokens_in",
        "tokens_out",
        "memory_saved_count",
        "fact_saved_count",
        "skill_saved_or_patched_count",
        "correct_owner",
        "user_corrected",
        "correction_state",
        "learning_artifact_state",
        "provenance",
        "substantiality",
    ]:
        if payload.get(key) is not None:
            notes[key] = payload[key]

    with events_connection(telemetry_root) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM tasks WHERE task_id = ?", (payload["task_id"],)).fetchone()[0]
        if existing:
            conn.execute(
                """
                UPDATE tasks
                SET closed_at = ?,
                    status = ?,
                    surface = ?,
                    session_id = COALESCE(?, session_id),
                    kanban_task_id = COALESCE(?, kanban_task_id),
                    title = ?,
                    user_goal_summary = ?,
                    owner_profile = ?,
                    assisting_profiles = ?,
                    task_type = COALESCE(?, task_type),
                    workdir = COALESCE(?, workdir),
                    repo_hint = COALESCE(?, repo_hint),
                    verification_strength = ?,
                    outcome = ?,
                    reopened = ?,
                    final_confidence = COALESCE(?, final_confidence),
                    provenance = ?,
                    substantiality = ?,
                    notes_json = ?
                WHERE task_id = ?
                """,
                (
                    payload["closed_at"],
                    payload["status"],
                    payload["surface"],
                    payload.get("session_id"),
                    payload.get("kanban_task_id"),
                    payload["title"],
                    payload["user_goal_summary"],
                    payload["owner_profile"],
                    json_dumps(payload["assisting_profiles"]),
                    payload.get("task_type"),
                    payload.get("workdir"),
                    payload.get("repo_hint"),
                    payload["verification_strength"],
                    payload["outcome"],
                    int(payload["reopened"]),
                    payload.get("final_confidence"),
                    payload.get("provenance"),
                    payload.get("substantiality"),
                    json_dumps(notes),
                    payload["task_id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, opened_at, closed_at, status, surface, session_id, kanban_task_id,
                    title, user_goal_summary, owner_profile, assisting_profiles, task_type,
                    workdir, repo_hint, verification_strength, outcome, reopened,
                    final_confidence, provenance, substantiality, notes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["task_id"],
                    payload["opened_at"],
                    payload["closed_at"],
                    payload["status"],
                    payload["surface"],
                    payload.get("session_id"),
                    payload.get("kanban_task_id"),
                    payload["title"],
                    payload["user_goal_summary"],
                    payload["owner_profile"],
                    json_dumps(payload["assisting_profiles"]),
                    payload.get("task_type"),
                    payload.get("workdir"),
                    payload.get("repo_hint"),
                    payload["verification_strength"],
                    payload["outcome"],
                    int(payload["reopened"]),
                    payload.get("final_confidence"),
                    payload.get("provenance"),
                    payload.get("substantiality"),
                    json_dumps(notes),
                ),
            )
            conn.execute(
                "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
                (payload["task_id"], payload["opened_at"], "task_opened", payload["owner_profile"], json_dumps({"surface": payload["surface"]})),
            )

        terminal_event_type = "task_completed" if payload["outcome"] == "success" else "task_closed"
        conn.execute(
            """
            DELETE FROM task_events
            WHERE task_id = ?
              AND event_type IN ('task_completed', 'task_closed')
              AND COALESCE(json_extract(payload_json, '$.source'), 'closeout') = 'closeout'
            """,
            (payload["task_id"],),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                payload["task_id"],
                payload["closed_at"],
                terminal_event_type,
                payload["owner_profile"],
                json_dumps({
                    "source": "closeout",
                    "outcome": payload["outcome"],
                    "status": payload["status"],
                    "verification_strength": payload["verification_strength"],
                    "notes": notes,
                }),
            ),
        )
        if payload["reopened"]:
            reopened_exists = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND event_type = 'task_reopened' AND occurred_at = ?",
                (payload["task_id"], payload["closed_at"]),
            ).fetchone()
            if not reopened_exists:
                conn.execute(
                    "INSERT INTO task_events(task_id, occurred_at, event_type, profile, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (payload["task_id"], payload["closed_at"], "task_reopened", payload["owner_profile"], json_dumps({"reopened": True})),
                )

        write_routing_from_closeout(conn, payload, notes)
        write_handoff_events_from_closeout(conn, payload, notes)
        apply_reuse_signals(conn, payload, notes)
        write_absence_events(conn, payload)
        upsert_normalized_closeout(conn, payload, notes)

    append_jsonl(
        telemetry_root,
        {
            "record_type": "task_closeout",
            "logged_at": utc_now(),
            **payload,
            "notes": notes,
        },
    )


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    payload = validate(build_payload(args))
    upsert_task(payload, telemetry_root)
    print(f"Logged task closeout: {payload['task_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
