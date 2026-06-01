"""Read-only Kanban stalled-transition reconciler.

Phase 1A intentionally does not mutate the board.  It classifies the same
families of orchestration stalls that the board doctor can observe, but returns
explicit action records suitable for Jensen/operator decision queues.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)


_DEFERRED_PG_RECONCILE_KINDS = [
    "review_parent_pr_head_evidence_missing",
    "repeated_failure_signature_decision",
]
_pg_partial_logged = False  # log the deferral once per process

_SYSTEMIC_FAILURE_CLASSES = {
    kb.FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE,
    kb.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT,
    "pre_spawn_validation",
}


@dataclass(frozen=True)
class ReconcileAction:
    kind: str
    task_id: Optional[str]
    severity: str
    reason: str
    safe_to_apply: bool
    signature: str
    details: dict[str, Any]


_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}

WAKE_BUCKET_AUTO_SILENT = "auto_silent"
WAKE_BUCKET_COMPACT_NOTIFY = "compact_notify"
WAKE_BUCKET_JENSEN_DECISION_REQUIRED = "jensen_decision_required"

_JENSEN_DECISION_KINDS = {
    "blocked_with_completed_parents_decision",
    "scheduled_with_completed_parents_decision",
    "review_parent_pr_head_evidence_missing",
}

_COMPACT_NOTIFY_KINDS = {
    "dead_running_candidate",
    "expired_claim_candidate",
    "old_ready_nonspawnable",
    "old_ready_spawnable",
    "orphan_claim_lock_observed",
    "pre_spawn_validation_decision",
    "repeated_failure_signature_decision",
    "stale_heartbeat_observed",
    "stale_run_metadata",
}


def _action_summary_for_wake_triage(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": action.get("kind"),
        "task_id": action.get("task_id"),
        "severity": action.get("severity"),
        "signature": action.get("signature"),
        "safe_to_apply": bool(action.get("safe_to_apply")),
        "reason": action.get("reason"),
    }


def _highest_severity(severities: list[str]) -> str:
    if not severities:
        return "warning"
    return min(
        (str(severity or "warning") for severity in severities),
        key=lambda severity: _SEVERITY_RANK.get(severity, 99),
    )


_DECISION_HINTS_BY_KIND: dict[str, dict[str, Any]] = {
    "review_parent_pr_head_evidence_missing": {
        "category": "review_evidence_gap",
        "suggested_options": [
            "remediate_parent_closeout",
            "keep_parked",
            "manual_review_with_stale_pr_risk",
        ],
        "next_step": (
            "remediate parent closeout PR-head evidence before final review, "
            "or explicitly accept stale-PR-gate risk"
        ),
    },
    "scheduled_with_completed_parents_decision": {
        "category": "parked_completed_dependencies",
        "suggested_options": ["keep_parked", "unblock", "close"],
        "next_step": "choose keep-parked, unblock, or close now that dependency parents are complete",
    },
    "blocked_with_completed_parents_decision": {
        "category": "blocked_completed_dependencies",
        "suggested_options": ["unblock", "keep_blocked", "close"],
        "next_step": "choose unblock, keep-blocked, or close now that dependency parents are complete",
    },
}


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _decision_hint_for_kinds(kinds: list[str]) -> dict[str, Any]:
    categories: list[str] = []
    options: list[str] = []
    next_steps: list[str] = []
    for kind in kinds:
        hint = _DECISION_HINTS_BY_KIND.get(str(kind))
        if not hint:
            categories.append("unclassified_decision")
            options.extend(["inspect", "keep_parked"])
            next_steps.append("inspect full reconcile payload and classify the operator decision")
            continue
        categories.append(str(hint["category"]))
        options.extend(str(option) for option in hint.get("suggested_options", []))
        next_steps.append(str(hint["next_step"]))

    categories = sorted(set(categories))
    options = _unique_preserving_order(options)
    next_steps = _unique_preserving_order(next_steps)
    primary_category = (
        "review_evidence_gap"
        if "review_evidence_gap" in categories
        else (categories[0] if categories else "unclassified_decision")
    )
    return {
        "primary_category": primary_category,
        "decision_categories": categories,
        "suggested_options": options,
        "recommended_next_step": "; ".join(next_steps),
    }


def _kanban_cli_command(*args: Any) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in ("hermes", "kanban", *args))


def _plan_step(command: str, purpose: str, *, mutates: bool) -> dict[str, Any]:
    return {"command": command, "purpose": purpose, "mutates": mutates}


def _packet_comment_text(packet: dict[str, Any], option: str) -> str:
    category = packet.get("primary_category") or "unclassified_decision"
    return (
        f"Jensen reconcile decision dry-run option selected: {option}; "
        f"category={category}; packet={packet.get('packet_id')}"
    )


def _metadata_arg(**values: Any) -> str:
    clean = {key: value for key, value in values.items() if value is not None}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _parent_ids_for_action(action: dict[str, Any]) -> list[str]:
    details = action.get("details") or {}
    if not isinstance(details, dict):
        return []
    parent_ids: list[str] = []
    closeouts = details.get("parent_closeouts")
    if isinstance(closeouts, list):
        for closeout in closeouts:
            if isinstance(closeout, dict) and closeout.get("parent_task_id"):
                parent_ids.append(str(closeout["parent_task_id"]))
    parent_state = details.get("parents")
    if isinstance(parent_state, str):
        for item in parent_state.split(","):
            parent_id = item.strip().split(":", 1)[0].strip()
            if parent_id:
                parent_ids.append(parent_id)
    return _unique_preserving_order(parent_ids)


def _operator_plan_for_option(packet: dict[str, Any], option: str) -> dict[str, Any]:
    task_id = packet.get("task_id")
    parent_ids = list(packet.get("affected_parent_task_ids") or [])
    plan: dict[str, Any] = {
        "option": option,
        "dry_run": True,
        "requires_confirmation": True,
        "mutates_when_applied": option not in {"inspect"},
        "preflight": [],
        "commands": [],
        "postcheck": [_plan_step(
            _kanban_cli_command("reconcile", "--json"),
            "verify reconcile signal after any manual action",
            mutates=False,
        )],
        "requires_input": [],
        "notes": [],
    }
    if task_id:
        plan["preflight"].append(_plan_step(
            _kanban_cli_command("show", task_id),
            "inspect current task state before applying an operator decision",
            mutates=False,
        ))
    plan["preflight"].append(_plan_step(
        _kanban_cli_command("reconcile", "--json"),
        "confirm the decision packet is still current",
        mutates=False,
    ))

    if not task_id:
        plan["mutates_when_applied"] = False
        plan["notes"].append("board-level packet has no task-specific mutation plan")
        return plan

    comment = _packet_comment_text(packet, option)
    if option in {"keep_parked", "keep_blocked"}:
        plan["commands"].append(_plan_step(
            _kanban_cli_command("comment", task_id, comment),
            "record explicit operator decision without changing task status",
            mutates=True,
        ))
    elif option == "unblock":
        plan["commands"].extend([
            _plan_step(
                _kanban_cli_command("comment", task_id, comment),
                "record why the parked/blocked task is being released",
                mutates=True,
            ),
            _plan_step(
                _kanban_cli_command("unblock", task_id),
                "return task to ready for dispatcher/profile processing",
                mutates=True,
            ),
        ])
    elif option == "close":
        result = "Closed by explicit Jensen reconcile decision after completed dependencies."
        metadata = _metadata_arg(
            reconcile_decision="close",
            source="kanban_reconcile_dry_run",
            packet_id=packet.get("packet_id"),
        )
        plan["commands"].append(_plan_step(
            _kanban_cli_command(
                "complete",
                task_id,
                "--result",
                result,
                "--summary",
                result,
                "--metadata",
                metadata,
            ),
            "close task with structured reconcile-decision metadata",
            mutates=True,
        ))
    elif option == "remediate_parent_closeout":
        if not parent_ids:
            plan["requires_input"].append("parent task id(s) needing PR-head evidence")
        plan["requires_input"].append("verified current PR head SHA for each remediated parent")
        for parent_id in parent_ids or ["<parent_task_id>"]:
            metadata = _metadata_arg(
                pr_head_sha="<verified_pr_head_sha>",
                reconcile_remediation_for=task_id,
                source="kanban_reconcile_dry_run",
            )
            plan["commands"].extend([
                _plan_step(
                    _kanban_cli_command("show", parent_id),
                    "inspect parent closeout before backfilling PR-head evidence",
                    mutates=False,
                ),
                _plan_step(
                    _kanban_cli_command(
                        "edit",
                        parent_id,
                        "--result",
                        "<existing result plus verified current PR head evidence>",
                        "--metadata",
                        metadata,
                    ),
                    "backfill structured PR-head evidence on completed parent closeout",
                    mutates=True,
                ),
            ])
    elif option == "manual_review_with_stale_pr_risk":
        risk_comment = comment + "; accepted_risk=stale_pr_head_gate_unenforceable"
        plan["commands"].extend([
            _plan_step(
                _kanban_cli_command("comment", task_id, risk_comment),
                "record explicit acceptance of stale-PR-gate risk",
                mutates=True,
            ),
            _plan_step(
                _kanban_cli_command("unblock", task_id),
                "release review despite missing current-PR-head evidence",
                mutates=True,
            ),
        ])
    else:
        plan["mutates_when_applied"] = False
        plan["notes"].append("unclassified option; inspect packet before choosing a mutation")
    return plan


def _operator_plans_for_packet(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        str(option): _operator_plan_for_option(packet, str(option))
        for option in packet.get("suggested_options") or []
    }


def _decision_packet_signature(packet: dict[str, Any]) -> str:
    # Keep this stable across volatile age/timestamp drift while still changing
    # when the actionable packet shape changes (kind/category/affected parents).
    return _signature(
        "decision_packet",
        str(packet.get("task_id") or "board"),
        {
            "affected_parent_task_ids": list(packet.get("affected_parent_task_ids") or []),
            "kinds": list(packet.get("kinds") or []),
            "primary_category": packet.get("primary_category"),
            "suggested_options": list(packet.get("suggested_options") or []),
        },
    )


def _find_decision_packet(
    packets: list[dict[str, Any]],
    task_id: str,
) -> Optional[dict[str, Any]]:
    for packet in packets:
        if str(packet.get("task_id") or "") == str(task_id):
            return packet
    return None


def group_decision_actions_by_task(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group Jensen decision-required actions into operator decision packets.

    The underlying reconcile actions remain separate and read-only for auditability,
    signatures, and compatibility. This projection gives cron/watch output one
    packet per task (or one board-level packet) so duplicate signals such as
    "scheduled with completed parents" plus "missing PR-head evidence" do not
    wake Jensen as separate decisions for the same card.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for action in actions:
        if _wake_bucket_for_action(action) != WAKE_BUCKET_JENSEN_DECISION_REQUIRED:
            continue
        task_id = action.get("task_id")
        packet_key = str(task_id or "board")
        packet = grouped.setdefault(
            packet_key,
            {
                "packet_id": f"task:{task_id}" if task_id else "board",
                "task_id": task_id,
                "severity": "warning",
                "action_count": 0,
                "kinds": [],
                "signatures": [],
                "reasons": [],
                "affected_parent_task_ids": [],
                "safe_to_apply": True,
                "actions": [],
            },
        )
        summary = _action_summary_for_wake_triage(action)
        packet["actions"].append(summary)
        packet["action_count"] = int(packet["action_count"]) + 1
        packet["kinds"] = sorted({*packet["kinds"], str(action.get("kind") or "")})
        signature = action.get("signature")
        if signature:
            packet["signatures"].append(signature)
        reason = action.get("reason")
        if reason and reason not in packet["reasons"]:
            packet["reasons"].append(reason)
        packet["affected_parent_task_ids"] = _unique_preserving_order([
            *packet.get("affected_parent_task_ids", []),
            *_parent_ids_for_action(action),
        ])
        packet["safe_to_apply"] = bool(packet["safe_to_apply"] and action.get("safe_to_apply"))
        packet["severity"] = _highest_severity([
            str(packet.get("severity") or "warning"),
            str(action.get("severity") or "warning"),
        ])

    for packet in grouped.values():
        packet.update(_decision_hint_for_kinds(packet.get("kinds") or []))
        packet["packet_signature"] = _decision_packet_signature(packet)
        packet["operator_plans"] = _operator_plans_for_packet(packet)

    return sorted(
        grouped.values(),
        key=lambda packet: (
            _SEVERITY_RANK.get(str(packet.get("severity") or "warning"), 99),
            str(packet.get("task_id") or ""),
            str(packet.get("packet_id") or ""),
        ),
    )


def _wake_bucket_for_action(action: dict[str, Any]) -> str:
    kind = str(action.get("kind") or "")
    severity = str(action.get("severity") or "warning")
    if kind in _JENSEN_DECISION_KINDS:
        return WAKE_BUCKET_JENSEN_DECISION_REQUIRED
    if kind in _COMPACT_NOTIFY_KINDS:
        return WAKE_BUCKET_COMPACT_NOTIFY
    # Unknown high-severity findings should not be hidden or reduced to a
    # compact script notice; wake Jensen to classify the new action family.
    if severity in {"critical", "error"}:
        return WAKE_BUCKET_JENSEN_DECISION_REQUIRED
    # Unknown warnings are still actionable but deterministic enough for a
    # compact operator notice until the classifier grows an explicit rule.
    return WAKE_BUCKET_COMPACT_NOTIFY


def classify_wake_triage(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify reconcile actions for script-first wake decisions.

    The contract is intentionally conservative: no reconcile actions means a
    watcher can stay silent; known deterministic defects can be reported with a
    compact notification; ambiguous workflow/handoff decisions require Jensen.
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        WAKE_BUCKET_AUTO_SILENT: [],
        WAKE_BUCKET_COMPACT_NOTIFY: [],
        WAKE_BUCKET_JENSEN_DECISION_REQUIRED: [],
    }
    if not actions:
        return {
            "mode": WAKE_BUCKET_AUTO_SILENT,
            "wake_agent": False,
            "reason": "no reconcile actions",
            "total_actions": 0,
            "decision_packet_count": 0,
            "decision_packets": [],
            "summary": {
                WAKE_BUCKET_AUTO_SILENT: 0,
                WAKE_BUCKET_COMPACT_NOTIFY: 0,
                WAKE_BUCKET_JENSEN_DECISION_REQUIRED: 0,
            },
            "buckets": buckets,
        }

    for action in actions:
        bucket = _wake_bucket_for_action(action)
        buckets[bucket].append(_action_summary_for_wake_triage(action))

    decision_packets = group_decision_actions_by_task(actions)

    if buckets[WAKE_BUCKET_JENSEN_DECISION_REQUIRED]:
        mode = WAKE_BUCKET_JENSEN_DECISION_REQUIRED
        reason = "ambiguous workflow or handoff decision requires Jensen"
    elif buckets[WAKE_BUCKET_COMPACT_NOTIFY]:
        mode = WAKE_BUCKET_COMPACT_NOTIFY
        reason = "deterministic reconcile signal can be delivered compactly without waking Jensen"
    else:
        mode = WAKE_BUCKET_AUTO_SILENT
        reason = "no operator-visible reconcile signal"

    return {
        "mode": mode,
        "wake_agent": mode == WAKE_BUCKET_JENSEN_DECISION_REQUIRED,
        "reason": reason,
        "total_actions": len(actions),
        "decision_packet_count": len(decision_packets),
        "decision_packets": decision_packets,
        "summary": {bucket: len(items) for bucket, items in buckets.items()},
        "buckets": buckets,
    }


def _pid_alive(pid: Any) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _signature(kind: str, task_id: Optional[str], material: dict[str, Any]) -> str:
    material = _stable_signature_material(material)
    canonical = json.dumps(
        {k: _jsonable(v) for k, v in sorted(material.items())},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{task_id or 'board'}:{digest}"


def _stable_signature_material(material: dict[str, Any]) -> dict[str, Any]:
    """Drop volatile observation-only fields from reconcile signatures.

    Reconcile apply is a gated two-step flow: operators inspect a dry-run packet
    and then pass the packet signature back into ``--apply-option``.  Fields like
    ``age_seconds`` change on every run, so including them made otherwise-current
    decision packets stale seconds after they were printed.  Keep the signature
    tied to structural state (status, parents, run ids, claim ids, timestamps,
    evidence) and leave live age in ``details`` for display only.
    """
    return {key: value for key, value in material.items() if key != "age_seconds"}


def _action(
    kind: str,
    task_id: Optional[str],
    severity: str,
    reason: str,
    *,
    safe_to_apply: bool = False,
    details: Optional[dict[str, Any]] = None,
) -> ReconcileAction:
    clean_details = dict(details or {})
    return ReconcileAction(
        kind=kind,
        task_id=task_id,
        severity=severity,
        reason=reason,
        safe_to_apply=bool(safe_to_apply),
        signature=_signature(kind, task_id, clean_details),
        details=clean_details,
    )


def _sort_actions(actions: list[ReconcileAction]) -> list[ReconcileAction]:
    return sorted(
        actions,
        key=lambda a: (
            _SEVERITY_RANK.get(a.severity, 99),
            a.kind,
            a.task_id or "",
            a.signature,
        ),
    )


def _profile_spawnable(profile: Optional[str]) -> bool:
    if not profile:
        return False
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        # Preserve dispatcher health semantics: if we cannot introspect
        # profiles, do not classify assigned work as nonspawnable.
        return True
    try:
        return bool(profile_exists(profile))
    except Exception:
        return True


def _host_prefix() -> str:
    try:
        return f"{kb._claimer_id().split(':', 1)[0]}:"
    except Exception:
        return ""


def _is_review_lane(assignee: Optional[str]) -> bool:
    try:
        return kb._lane_type_for_assignee(assignee) == "review"
    except Exception:
        return str(assignee or "").strip().casefold() in {"reviewer", "boris"}


def _parent_pr_head_evidence_details(
    conn: sqlite3.Connection,
    task_id: str,
) -> list[dict[str, Any]]:
    """Summarize parent closeout PR-head evidence without mutating the board."""
    details: list[dict[str, Any]] = []
    for parent_id in kb.parent_ids(conn, task_id, relation_type=kb.LINK_RELATION_DEPENDENCY):
        task_row = conn.execute(
            "SELECT status, result FROM tasks WHERE id = ?",
            (parent_id,),
        ).fetchone()
        status = task_row["status"] if task_row else None
        rows = conn.execute(
            """
            SELECT id, metadata
              FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
            """,
            (parent_id,),
        ).fetchall()
        latest_run_id = int(rows[0]["id"]) if rows else None
        latest_metadata_keys: list[str] = []
        evidence_run_id: Optional[int] = None
        has_pr_head = False
        for idx, run in enumerate(rows):
            try:
                metadata = json.loads(run["metadata"]) if run["metadata"] else None
            except Exception:
                metadata = None
            if idx == 0 and isinstance(metadata, dict):
                latest_metadata_keys = sorted(str(k) for k in metadata.keys())
            try:
                sha = kb._extract_pr_head_sha(metadata)
            except Exception:
                sha = None
            if sha:
                has_pr_head = True
                evidence_run_id = int(run["id"])
                break
        result_fallback_present = False
        if not has_pr_head and task_row:
            try:
                result_fallback_present = bool(kb._extract_pr_head_sha(task_row["result"]))
            except Exception:
                result_fallback_present = False
            has_pr_head = result_fallback_present
        details.append({
            "parent_task_id": parent_id,
            "parent_status": status,
            "latest_completed_run_id": latest_run_id,
            "evidence_run_id": evidence_run_id,
            "pr_head_sha_present": has_pr_head,
            "result_fallback_present": result_fallback_present,
            "latest_metadata_keys": latest_metadata_keys,
        })
    return details


def _pre_spawn_validation_errors_for_reconcile(task: kb.Task) -> list[str]:
    """Return deterministic spawn prerequisite failures without DB writes."""
    errors: list[str] = []
    profile_ok = True
    if not task.assignee:
        return errors
    if not _profile_spawnable(task.assignee):
        profile_ok = False
        errors.append(f"profile not found: {task.assignee}")

    try:
        errors.extend(kb._workspace_pre_spawn_errors(task))
        if (task.workspace_kind or "scratch") == "dir" and not task.workspace_path:
            errors.append("workspace_kind=dir requires workspace_path")
    except Exception as exc:
        errors.append(f"workspace validation failed: {exc}")

    if profile_ok:
        skills = list(task.skills or [])
        if skills:
            home = kb._profile_home_for_spawn(task)
            missing = [
                str(skill) for skill in skills
                if skill and not kb._skill_available_for_home(str(skill), home)
            ]
            if missing:
                errors.append("missing forced skill(s): " + ", ".join(missing))
    return errors


def _latest_failure_payloads(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return recent structured failure payloads for a task."""
    rows = conn.execute(
        """
        SELECT kind, payload, created_at
          FROM task_events
         WHERE task_id = ?
           AND kind IN (
               'gave_up', 'spawn_failed', 'crashed', 'timed_out',
               'pre_spawn_validation_failed', 'systemic_failure_signature',
               'protocol_violation'
           )
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (task_id, max(1, int(limit))),
    ).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        payload.setdefault("event_kind", row["kind"])
        payload.setdefault("created_at", row["created_at"])
        payloads.append(payload)
    return payloads


def _failure_signature_from_payloads(
    payloads: list[dict[str, Any]],
    fallback_error: Optional[str],
) -> Optional[str]:
    for payload in payloads:
        sig = str(payload.get("failure_signature") or "").strip()
        if sig:
            return sig
    error = str(fallback_error or "").strip()
    if not error:
        return None
    return kb._error_fingerprint(error)


def _count_values(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _snapshot_sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


@contextmanager
def _snapshot_connect(path: Path):
    """Query a filesystem snapshot so live reconcile creates no DB sidecars.

    Opening a WAL-mode SQLite database via ``mode=ro`` can still create
    transient ``-wal``/``-shm`` files next to the live board.  Phase 1A's
    contract is stricter: classify only and leave the live DB path untouched.
    Copying the main DB and any existing sidecars into a temp dir confines any
    SQLite bookkeeping to the snapshot.
    """
    with tempfile.TemporaryDirectory(prefix="hermes-kanban-reconcile-") as tmp:
        snap = Path(tmp) / path.name
        shutil.copy2(path, snap)
        for suffix in ("-wal", "-shm"):
            src = _snapshot_sidecar(path, suffix)
            if src.exists():
                shutil.copy2(src, _snapshot_sidecar(snap, suffix))
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def collect_reconcile_actions(
    conn: sqlite3.Connection,
    *,
    ready_age_seconds: int = 15 * 60,
    now: Optional[int] = None,
) -> list[ReconcileAction]:
    """Classify actionable stalled-transition signals without writing."""
    as_of = int(now if now is not None else time.time())
    ready_age_seconds = max(1, int(ready_age_seconds or 900))
    actions: list[ReconcileAction] = []
    host_prefix = _host_prefix()

    # Running tasks: split dead/expired/stale-heartbeat observations so a
    # heartbeat warning alone never becomes reclaim authority.
    for row in conn.execute(
        """
        SELECT id, title, assignee, worker_pid, claim_lock, claim_expires,
               last_heartbeat_at, current_run_id, started_at
          FROM tasks
         WHERE status = 'running'
         ORDER BY started_at, created_at, id
        """
    ):
        pid_alive = _pid_alive(row["worker_pid"])
        claim_expires = row["claim_expires"]
        expired = bool(claim_expires and int(claim_expires) < as_of)
        hb = row["last_heartbeat_at"]
        heartbeat_age = (as_of - int(hb)) if hb is not None else None
        stale_hb = bool(heartbeat_age is not None and heartbeat_age > 15 * 60)
        base = {
            "assignee": row["assignee"],
            "worker_pid": row["worker_pid"],
            "pid_alive": pid_alive,
            "claim_lock": row["claim_lock"],
            "claim_expires": claim_expires,
            "last_heartbeat_at": hb,
            "current_run_id": row["current_run_id"],
        }
        if not pid_alive:
            # Safe only when this is clearly a host-local worker claim or no
            # pid was ever recorded.  The dry-run record remains advisory.
            lock = row["claim_lock"] or ""
            host_local = bool(host_prefix and lock.startswith(host_prefix))
            safe = host_local or row["worker_pid"] is None
            actions.append(_action(
                "dead_running_candidate",
                row["id"],
                "critical",
                "running task has no live worker PID",
                safe_to_apply=safe,
                details={**base, "host_local": host_local},
            ))
        if expired:
            lock = row["claim_lock"] or ""
            host_local = bool(host_prefix and lock.startswith(host_prefix))
            safe = (host_local and not pid_alive) or row["worker_pid"] is None
            actions.append(_action(
                "expired_claim_candidate",
                row["id"],
                "warning",
                "running task claim has expired",
                safe_to_apply=safe,
                details={
                    **base,
                    "seconds_expired": as_of - int(claim_expires),
                    "host_local": host_local,
                },
            ))
        if stale_hb:
            actions.append(_action(
                "stale_heartbeat_observed",
                row["id"],
                "warning",
                "running task heartbeat is older than the advisory threshold",
                safe_to_apply=False,
                details={**base, "heartbeat_age_seconds": heartbeat_age},
            ))

    # Stale run metadata: run row still marked running but no longer the
    # current task run.  Phase 1A reports only; Phase 1B may clean this up.
    for row in conn.execute(
        """
        SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at,
               t.status AS task_status, t.current_run_id
          FROM task_runs r
          JOIN tasks t ON t.id = r.task_id
         WHERE r.status = 'running'
           AND (t.status != 'running' OR t.current_run_id IS NULL OR t.current_run_id != r.id)
         ORDER BY r.started_at DESC, r.id DESC
        """
    ):
        pid_alive = _pid_alive(row["worker_pid"])
        actions.append(_action(
            "stale_run_metadata",
            row["task_id"],
            "warning",
            "task_run is marked running but is not the task current active run",
            safe_to_apply=not pid_alive,
            details={
                "run_id": row["run_id"],
                "profile": row["profile"],
                "worker_pid": row["worker_pid"],
                "pid_alive": pid_alive,
                "task_status": row["task_status"],
                "current_run_id": row["current_run_id"],
            },
        ))

    # Orphan claim locks: claim_lock is set but the task is no longer running.
    # All claim/release/timeout/complete paths clear claim_lock atomically when
    # they leave 'running', so a non-terminal, non-running row carrying a lock
    # is an invariant violation that blocks future claim_task CAS attempts
    # (which require claim_lock IS NULL).  Terminal states (done/archived) can
    # never be reclaimed, so a residual lock there is cosmetic and excluded to
    # keep the watchdog signal actionable.
    for row in conn.execute(
        """
        SELECT id, status, assignee, claim_lock, claim_expires, worker_pid,
               current_run_id, last_heartbeat_at, started_at, created_at
          FROM tasks
         WHERE claim_lock IS NOT NULL
           AND status NOT IN ('running', 'done', 'archived')
         ORDER BY id
        """
    ):
        claim_expires = row["claim_expires"]
        actions.append(_action(
            "orphan_claim_lock_observed",
            row["id"],
            "error",
            "task is not running but still carries a claim_lock; "
            "future claim_task CAS will fail until the lock is cleared",
            safe_to_apply=False,
            details={
                "status": row["status"],
                "assignee": row["assignee"],
                "claim_lock": row["claim_lock"],
                "claim_expires": claim_expires,
                "claim_expired": bool(
                    claim_expires is not None and int(claim_expires) < as_of
                ),
                "worker_pid": row["worker_pid"],
                "pid_alive": _pid_alive(row["worker_pid"]),
                "current_run_id": row["current_run_id"],
                "last_heartbeat_at": row["last_heartbeat_at"],
                "started_at": row["started_at"],
                "age_seconds": as_of - int(row["created_at"]),
            },
        ))

    # Blocked tasks whose dependency parents are all terminal need an explicit
    # Jensen/reviewer decision, not automatic unblocking.
    for row in conn.execute(
        """
        SELECT c.id, c.title, c.assignee, COUNT(l.parent_id) AS parents,
               SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) AS terminal_parents,
               GROUP_CONCAT(p.id || ':' || p.status, ', ') AS parent_state
          FROM tasks c
          JOIN task_links l ON l.child_id = c.id
          JOIN tasks p ON p.id = l.parent_id
         WHERE c.status = 'blocked'
           AND COALESCE(l.relation_type, 'dependency') = 'dependency'
         GROUP BY c.id
        HAVING parents > 0 AND parents = terminal_parents
         ORDER BY c.created_at, c.id
        """
    ):
        actions.append(_action(
            "blocked_with_completed_parents_decision",
            row["id"],
            "warning",
            "blocked task has all dependency parents completed; explicit unblock/re-review decision needed",
            safe_to_apply=False,
            details={
                "assignee": row["assignee"],
                "parents": row["parent_state"],
                "parent_count": row["parents"],
            },
        ))

    # Scheduled tasks are intentionally non-dispatchable, so do not auto-unblock
    # them.  But when all dependency parents are terminal and the card has no
    # active claim/run, an old scheduled task is often a parked decision rather
    # than healthy idle work.  Surface it as decision-only so Jensen/operator
    # wakes can inspect and choose unblock, keep parked, or close out.
    for row in conn.execute(
        """
        SELECT c.id, c.title, c.assignee, c.created_at, c.started_at,
               c.current_run_id, c.last_heartbeat_at,
               COUNT(l.parent_id) AS parents,
               SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) AS terminal_parents,
               GROUP_CONCAT(p.id || ':' || p.status, ', ') AS parent_state
          FROM tasks c
          JOIN task_links l ON l.child_id = c.id
          JOIN tasks p ON p.id = l.parent_id
         WHERE c.status = 'scheduled'
           AND c.claim_lock IS NULL
           AND c.current_run_id IS NULL
           AND c.worker_pid IS NULL
           AND COALESCE(l.relation_type, 'dependency') = 'dependency'
         GROUP BY c.id
        HAVING parents > 0 AND parents = terminal_parents
         ORDER BY c.created_at, c.id
        """
    ):
        age = as_of - int(row["created_at"])
        if age < ready_age_seconds:
            continue
        actions.append(_action(
            "scheduled_with_completed_parents_decision",
            row["id"],
            "warning",
            "scheduled task has all dependency parents completed and needs an explicit keep-parked/unblock/close decision",
            safe_to_apply=False,
            details={
                "assignee": row["assignee"],
                "parents": row["parent_state"],
                "parent_count": row["parents"],
                "age_seconds": age,
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "current_run_id": row["current_run_id"],
                "last_heartbeat_at": row["last_heartbeat_at"],
            },
        ))

    # Final-review cards can only enforce the current-PR-head gate when at
    # least one completed dependency parent exposes PR head evidence in its
    # closeout metadata (or the legacy explicit-SHA result fallback).  If no
    # such evidence exists, reviewer completion can still proceed, but stale-PR
    # safety is unenforceable.  Surface this before a reviewer is spawned or a
    # parked reviewer card is treated as healthy idle work.
    for row in conn.execute(
        """
        SELECT c.id, c.title, c.assignee, c.status, c.created_at,
               COUNT(l.parent_id) AS parents,
               SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) AS terminal_parents,
               GROUP_CONCAT(p.id || ':' || p.status, ', ') AS parent_state
          FROM tasks c
          JOIN task_links l ON l.child_id = c.id
          JOIN tasks p ON p.id = l.parent_id
         WHERE c.status NOT IN ('done','archived')
           AND COALESCE(l.relation_type, 'dependency') = 'dependency'
         GROUP BY c.id
        HAVING parents > 0 AND parents = terminal_parents
         ORDER BY c.created_at, c.id
        """
    ):
        if not _is_review_lane(row["assignee"]):
            continue
        if kb._expected_parent_pr_head_sha(conn, row["id"]) is not None:
            continue
        age = as_of - int(row["created_at"])
        parent_details = _parent_pr_head_evidence_details(conn, row["id"])
        actions.append(_action(
            "review_parent_pr_head_evidence_missing",
            row["id"],
            "warning",
            "review task's completed dependency parents lack PR-head evidence; final-review stale-PR gate cannot enforce current-head coverage",
            safe_to_apply=False,
            details={
                "assignee": row["assignee"],
                "status": row["status"],
                "parents": row["parent_state"],
                "parent_count": row["parents"],
                "age_seconds": age,
                "parent_closeouts": parent_details,
            },
        ))

    # Spawn prerequisite validation mirrors the dispatcher's deterministic
    # pre-spawn checks without claiming tasks or writing failure rows.  This
    # gives Jensen/operator wakes an actionable decision queue for profile,
    # skill, and workspace defects before the dispatcher burns retries.
    for row in conn.execute(
        """
        SELECT id, status, title, assignee, created_at
          FROM tasks
         WHERE status = 'ready'
           AND claim_lock IS NULL
         ORDER BY priority DESC, created_at, id
        """
    ):
        task = kb.get_task(conn, row["id"])
        if task is None:
            continue
        errors = _pre_spawn_validation_errors_for_reconcile(task)
        if not errors:
            continue
        actions.append(_action(
            "pre_spawn_validation_decision",
            row["id"],
            "warning",
            "task is eligible to spawn but deterministic profile/skill/workspace prerequisites are not satisfied",
            safe_to_apply=False,
            details={
                "assignee": row["assignee"],
                "status": row["status"],
                "age_seconds": as_of - int(row["created_at"]),
                "workspace_kind": task.workspace_kind,
                "workspace_path": task.workspace_path,
                "skills": task.skills or [],
                "validation_errors": errors,
            },
        ))

    # Repeated same-signature failures across tasks are often platform/profile
    # defects rather than independent task failures.  The dispatcher already
    # has a same-tick systemic breaker; this read-only view catches durable
    # residue across ticks/sessions and preserves structured failure-class
    # metadata when it exists.
    failure_groups: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        """
        SELECT id, status, assignee, consecutive_failures, last_failure_error
          FROM tasks
         WHERE status NOT IN ('done','archived')
           AND consecutive_failures > 0
           AND last_failure_error IS NOT NULL
         ORDER BY id
        """
    ):
        payloads = _latest_failure_payloads(conn, row["id"])
        failure_signature = _failure_signature_from_payloads(
            payloads, row["last_failure_error"]
        )
        if not failure_signature:
            continue
        failure_classes = sorted({
            str(payload.get("failure_class"))
            for payload in payloads
            if payload.get("failure_class")
        })
        triggers = sorted({
            str(payload.get("trigger_outcome"))
            for payload in payloads
            if payload.get("trigger_outcome")
        })
        failure_groups.setdefault(failure_signature, []).append({
            "task_id": row["id"],
            "status": row["status"],
            "assignee": row["assignee"],
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "last_failure_error": str(row["last_failure_error"] or "")[:240],
            "failure_classes": failure_classes,
            "trigger_outcomes": triggers,
        })
    for failure_signature, tasks in failure_groups.items():
        failure_classes = sorted({
            failure_class
            for task in tasks
            for failure_class in task.get("failure_classes", [])
        })
        systemic_metadata = bool(
            set(failure_classes).intersection(_SYSTEMIC_FAILURE_CLASSES)
        )
        threshold = kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD
        if len(tasks) < threshold and not systemic_metadata:
            continue
        status_counts = _count_values([str(task["status"]) for task in tasks])
        assignee_counts = _count_values([
            str(task["assignee"] or "unassigned") for task in tasks
        ])
        actions.append(_action(
            "repeated_failure_signature_decision",
            None,
            "error" if systemic_metadata else "warning",
            "multiple non-terminal tasks share a failure signature or systemic failure metadata; treat as possible platform/profile defect before retrying",
            safe_to_apply=False,
            details={
                "failure_signature": failure_signature,
                "task_count": len(tasks),
                "signature_threshold": threshold,
                "systemic_metadata_present": systemic_metadata,
                "failure_classes": failure_classes,
                "status_counts": status_counts,
                "assignee_counts": assignee_counts,
                "total_consecutive_failures": sum(
                    int(task["consecutive_failures"]) for task in tasks
                ),
                "tasks": tasks[:10],
            },
        ))

    for row in conn.execute(
        """
        SELECT id, title, assignee, created_at
          FROM tasks
         WHERE status = 'ready' AND claim_lock IS NULL
         ORDER BY created_at, id
        """,
    ):
        age = as_of - int(row["created_at"])
        if age < ready_age_seconds:
            continue
        spawnable = _profile_spawnable(row["assignee"])
        kind = f"old_ready_{'spawnable' if spawnable else 'nonspawnable'}"
        actions.append(_action(
            kind,
            row["id"],
            "warning" if spawnable else "info",
            "ready task has not been claimed within threshold",
            safe_to_apply=False,
            details={
                "assignee": row["assignee"],
                "age_seconds": age,
                "created_at": row["created_at"],
                "spawnable_profile": spawnable,
            },
        ))

    return _sort_actions(actions)


def _collect_reconcile_actions_pg(
    conn,
    slug: str,
    store,
    *,
    ready_age_seconds: int = 15 * 60,
    now: Optional[int] = None,
) -> list[ReconcileAction]:
    """Board-scoped Postgres port of ``collect_reconcile_actions``.

    Read-only: emits the same nine CORE action kinds (same kind/severity/reason/
    safe_to_apply/details as the sqlite collector) but never writes/mutates the
    board. ``conn`` is an open psycopg connection (caller-owned); ``store`` is a
    ``PostgresKanbanStore`` bound to ``slug`` (used only for ``store.get_task``
    in the pre-spawn check). The two niche DEFERRED kinds
    (``review_parent_pr_head_evidence_missing`` and
    ``repeated_failure_signature_decision``) are intentionally not emitted.
    """
    from psycopg.rows import dict_row

    as_of = int(now if now is not None else time.time())
    ready_age_seconds = max(1, int(ready_age_seconds or 900))
    actions: list[ReconcileAction] = []
    host_prefix = _host_prefix()

    with conn.cursor(row_factory=dict_row) as cur:
        # Running tasks: split dead/expired/stale-heartbeat observations so a
        # heartbeat warning alone never becomes reclaim authority.
        cur.execute(
            "SELECT id, title, assignee, worker_pid, claim_lock, claim_expires, "
            "       last_heartbeat_at, current_run_id, started_at "
            "  FROM tasks "
            " WHERE board=%s AND status='running' "
            " ORDER BY started_at, created_at, id",
            (slug,),
        )
        for row in cur.fetchall():
            pid_alive = _pid_alive(row["worker_pid"])
            claim_expires = row["claim_expires"]
            expired = bool(claim_expires and int(claim_expires) < as_of)
            hb = row["last_heartbeat_at"]
            heartbeat_age = (as_of - int(hb)) if hb is not None else None
            stale_hb = bool(heartbeat_age is not None and heartbeat_age > 15 * 60)
            base = {
                "assignee": row["assignee"],
                "worker_pid": row["worker_pid"],
                "pid_alive": pid_alive,
                "claim_lock": row["claim_lock"],
                "claim_expires": claim_expires,
                "last_heartbeat_at": hb,
                "current_run_id": row["current_run_id"],
            }
            if not pid_alive:
                # Safe only when this is clearly a host-local worker claim or no
                # pid was ever recorded.  The dry-run record remains advisory.
                lock = row["claim_lock"] or ""
                host_local = bool(host_prefix and lock.startswith(host_prefix))
                safe = host_local or row["worker_pid"] is None
                actions.append(_action(
                    "dead_running_candidate",
                    row["id"],
                    "critical",
                    "running task has no live worker PID",
                    safe_to_apply=safe,
                    details={**base, "host_local": host_local},
                ))
            if expired:
                lock = row["claim_lock"] or ""
                host_local = bool(host_prefix and lock.startswith(host_prefix))
                safe = (host_local and not pid_alive) or row["worker_pid"] is None
                actions.append(_action(
                    "expired_claim_candidate",
                    row["id"],
                    "warning",
                    "running task claim has expired",
                    safe_to_apply=safe,
                    details={
                        **base,
                        "seconds_expired": as_of - int(claim_expires),
                        "host_local": host_local,
                    },
                ))
            if stale_hb:
                actions.append(_action(
                    "stale_heartbeat_observed",
                    row["id"],
                    "warning",
                    "running task heartbeat is older than the advisory threshold",
                    safe_to_apply=False,
                    details={**base, "heartbeat_age_seconds": heartbeat_age},
                ))

        # Stale run metadata: run row still marked running but no longer the
        # current task run.  Phase 1A reports only; Phase 1B may clean this up.
        cur.execute(
            "SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at, "
            "       t.status AS task_status, t.current_run_id "
            "  FROM task_runs r "
            "  JOIN tasks t ON t.board=r.board AND t.id=r.task_id "
            " WHERE r.board=%s AND r.status='running' "
            "   AND (t.status != 'running' OR t.current_run_id IS NULL "
            "        OR t.current_run_id != r.id) "
            " ORDER BY r.started_at DESC, r.id DESC",
            (slug,),
        )
        for row in cur.fetchall():
            pid_alive = _pid_alive(row["worker_pid"])
            actions.append(_action(
                "stale_run_metadata",
                row["task_id"],
                "warning",
                "task_run is marked running but is not the task current active run",
                safe_to_apply=not pid_alive,
                details={
                    "run_id": row["run_id"],
                    "profile": row["profile"],
                    "worker_pid": row["worker_pid"],
                    "pid_alive": pid_alive,
                    "task_status": row["task_status"],
                    "current_run_id": row["current_run_id"],
                },
            ))

        # Orphan claim locks: claim_lock is set but the task is no longer
        # running.  All claim/release/timeout/complete paths clear claim_lock
        # atomically when they leave 'running', so a non-terminal, non-running
        # row carrying a lock is an invariant violation that blocks future
        # claim_task CAS attempts (which require claim_lock IS NULL).  Terminal
        # states (done/archived) can never be reclaimed, so a residual lock
        # there is cosmetic and excluded to keep the watchdog signal actionable.
        cur.execute(
            "SELECT id, status, assignee, claim_lock, claim_expires, worker_pid, "
            "       current_run_id, last_heartbeat_at, started_at, created_at "
            "  FROM tasks "
            " WHERE board=%s AND claim_lock IS NOT NULL "
            "   AND status NOT IN ('running', 'done', 'archived') "
            " ORDER BY id",
            (slug,),
        )
        for row in cur.fetchall():
            claim_expires = row["claim_expires"]
            actions.append(_action(
                "orphan_claim_lock_observed",
                row["id"],
                "error",
                "task is not running but still carries a claim_lock; "
                "future claim_task CAS will fail until the lock is cleared",
                safe_to_apply=False,
                details={
                    "status": row["status"],
                    "assignee": row["assignee"],
                    "claim_lock": row["claim_lock"],
                    "claim_expires": claim_expires,
                    "claim_expired": bool(
                        claim_expires is not None and int(claim_expires) < as_of
                    ),
                    "worker_pid": row["worker_pid"],
                    "pid_alive": _pid_alive(row["worker_pid"]),
                    "current_run_id": row["current_run_id"],
                    "last_heartbeat_at": row["last_heartbeat_at"],
                    "started_at": row["started_at"],
                    "age_seconds": as_of - int(row["created_at"]),
                },
            ))

        # Blocked tasks whose dependency parents are all terminal need an
        # explicit Jensen/reviewer decision, not automatic unblocking.
        cur.execute(
            "SELECT c.id, c.title, c.assignee, COUNT(l.parent_id) AS parents, "
            "  SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) "
            "      AS terminal_parents, "
            "  string_agg(p.id || ':' || p.status, ', ' ORDER BY p.id) AS parent_state "
            "  FROM tasks c "
            "  JOIN task_links l ON l.board=c.board AND l.child_id=c.id "
            "  JOIN tasks p ON p.board=l.board AND p.id=l.parent_id "
            " WHERE c.board=%s AND c.status='blocked' "
            "   AND COALESCE(l.relation_type,'dependency')='dependency' "
            " GROUP BY c.id, c.title, c.assignee, c.created_at "
            "HAVING COUNT(l.parent_id) > 0 "
            "   AND COUNT(l.parent_id) = "
            "       SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) "
            " ORDER BY c.created_at, c.id",
            (slug,),
        )
        for row in cur.fetchall():
            actions.append(_action(
                "blocked_with_completed_parents_decision",
                row["id"],
                "warning",
                "blocked task has all dependency parents completed; explicit unblock/re-review decision needed",
                safe_to_apply=False,
                details={
                    "assignee": row["assignee"],
                    "parents": row["parent_state"],
                    "parent_count": row["parents"],
                },
            ))

        # Scheduled tasks are intentionally non-dispatchable, so do not
        # auto-unblock them.  But when all dependency parents are terminal and
        # the card has no active claim/run, an old scheduled task is often a
        # parked decision rather than healthy idle work.  Surface it as
        # decision-only so Jensen/operator wakes can inspect and choose unblock,
        # keep parked, or close out.
        cur.execute(
            "SELECT c.id, c.title, c.assignee, c.created_at, c.started_at, "
            "       c.current_run_id, c.last_heartbeat_at, "
            "       COUNT(l.parent_id) AS parents, "
            "  SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) "
            "      AS terminal_parents, "
            "  string_agg(p.id || ':' || p.status, ', ' ORDER BY p.id) AS parent_state "
            "  FROM tasks c "
            "  JOIN task_links l ON l.board=c.board AND l.child_id=c.id "
            "  JOIN tasks p ON p.board=l.board AND p.id=l.parent_id "
            " WHERE c.board=%s AND c.status='scheduled' "
            "   AND c.claim_lock IS NULL "
            "   AND c.current_run_id IS NULL "
            "   AND c.worker_pid IS NULL "
            "   AND COALESCE(l.relation_type,'dependency')='dependency' "
            " GROUP BY c.id, c.title, c.assignee, c.created_at, c.started_at, "
            "          c.current_run_id, c.last_heartbeat_at "
            "HAVING COUNT(l.parent_id) > 0 "
            "   AND COUNT(l.parent_id) = "
            "       SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) "
            " ORDER BY c.created_at, c.id",
            (slug,),
        )
        for row in cur.fetchall():
            age = as_of - int(row["created_at"])
            if age < ready_age_seconds:
                continue
            actions.append(_action(
                "scheduled_with_completed_parents_decision",
                row["id"],
                "warning",
                "scheduled task has all dependency parents completed and needs an explicit keep-parked/unblock/close decision",
                safe_to_apply=False,
                details={
                    "assignee": row["assignee"],
                    "parents": row["parent_state"],
                    "parent_count": row["parents"],
                    "age_seconds": age,
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "current_run_id": row["current_run_id"],
                    "last_heartbeat_at": row["last_heartbeat_at"],
                },
            ))

        # NOTE: review_parent_pr_head_evidence_missing is DEFERRED (not emitted).

        # Spawn prerequisite validation mirrors the dispatcher's deterministic
        # pre-spawn checks without claiming tasks or writing failure rows.  This
        # gives Jensen/operator wakes an actionable decision queue for profile,
        # skill, and workspace defects before the dispatcher burns retries.
        cur.execute(
            "SELECT id, status, title, assignee, created_at "
            "  FROM tasks "
            " WHERE board=%s AND status='ready' "
            "   AND claim_lock IS NULL "
            " ORDER BY priority DESC, created_at, id",
            (slug,),
        )
        ready_unclaimed_rows = cur.fetchall()
        for row in ready_unclaimed_rows:
            task = store.get_task(row["id"])
            if task is None:
                continue
            errors = _pre_spawn_validation_errors_for_reconcile(task)
            if not errors:
                continue
            actions.append(_action(
                "pre_spawn_validation_decision",
                row["id"],
                "warning",
                "task is eligible to spawn but deterministic profile/skill/workspace prerequisites are not satisfied",
                safe_to_apply=False,
                details={
                    "assignee": row["assignee"],
                    "status": row["status"],
                    "age_seconds": as_of - int(row["created_at"]),
                    "workspace_kind": task.workspace_kind,
                    "workspace_path": task.workspace_path,
                    "skills": task.skills or [],
                    "validation_errors": errors,
                },
            ))

        # NOTE: repeated_failure_signature_decision is DEFERRED (not emitted).

        cur.execute(
            "SELECT id, title, assignee, created_at "
            "  FROM tasks "
            " WHERE board=%s AND status='ready' AND claim_lock IS NULL "
            " ORDER BY created_at, id",
            (slug,),
        )
        for row in cur.fetchall():
            age = as_of - int(row["created_at"])
            if age < ready_age_seconds:
                continue
            spawnable = _profile_spawnable(row["assignee"])
            kind = f"old_ready_{'spawnable' if spawnable else 'nonspawnable'}"
            actions.append(_action(
                kind,
                row["id"],
                "warning" if spawnable else "info",
                "ready task has not been claimed within threshold",
                safe_to_apply=False,
                details={
                    "assignee": row["assignee"],
                    "age_seconds": age,
                    "created_at": row["created_at"],
                    "spawnable_profile": spawnable,
                },
            ))

    return _sort_actions(actions)


def actions_to_dicts(actions: list[ReconcileAction]) -> list[dict[str, Any]]:
    return [asdict(action) for action in actions]


def _filter_acknowledged_decision_packets(
    conn: sqlite3.Connection,
    action_dicts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Suppress decision packets that Jensen explicitly chose to keep parked.

    ``keep_parked`` / ``keep_blocked`` are real operator decisions.  Without a
    durable acknowledgement, deterministic wake triage keeps re-waking Jensen for
    the same intentionally parked card, which turns a no-op decision into token
    burn.  Treat the idempotency comment written by ``apply_reconcile_decision``
    as an acknowledgement for the current packet signature.  If any structural
    fact changes, the packet signature changes and the decision wakes again.
    """
    triage = classify_wake_triage(action_dicts)
    suppressed_action_signatures: set[str] = set()
    suppressed_packets: list[dict[str, Any]] = []
    for packet in triage.get("decision_packets") or []:
        task_id = str(packet.get("task_id") or "").strip()
        packet_signature = str(packet.get("packet_signature") or "").strip()
        if not task_id or not packet_signature:
            continue
        applied_option: Optional[str] = None
        for option in ("keep_parked", "keep_blocked"):
            if _find_existing_reconcile_decision_comment(
                conn,
                task_id,
                option=option,
                packet_signature=packet_signature,
            ) is not None:
                applied_option = option
                break
        if not applied_option:
            continue
        action_signatures = [
            str(action.get("signature") or "")
            for action in packet.get("actions") or []
            if action.get("signature")
        ]
        suppressed_action_signatures.update(action_signatures)
        suppressed_packets.append({
            "task_id": task_id,
            "packet_signature": packet_signature,
            "option": applied_option,
            "action_count": int(packet.get("action_count") or len(action_signatures)),
            "kinds": list(packet.get("kinds") or []),
        })
    if not suppressed_action_signatures:
        return action_dicts, []
    filtered = [
        action for action in action_dicts
        if str(action.get("signature") or "") not in suppressed_action_signatures
    ]
    return filtered, suppressed_packets


def run_reconciler(
    *,
    board: Optional[str] = None,
    ready_age_seconds: int = 15 * 60,
    now: Optional[int] = None,
) -> dict[str, Any]:
    try:
        from hermes_cli.kanban.store import resolve_backend
        if resolve_backend() == "postgres":
            return _run_reconciler_pg(board=board,
                                      ready_age_seconds=ready_age_seconds, now=now)
    except Exception:
        pass  # defensive: fall through to sqlite (default deployments unaffected)
    # ---- existing sqlite body, verbatim ----
    path = kb.kanban_db_path(board=board)
    as_of = int(now if now is not None else time.time())
    with _snapshot_connect(path) as conn:
        actions = collect_reconcile_actions(
            conn,
            ready_age_seconds=ready_age_seconds,
            now=as_of,
        )
        action_dicts = actions_to_dicts(actions)
        action_dicts, suppressed_packets = _filter_acknowledged_decision_packets(
            conn,
            action_dicts,
        )
    wake_triage = classify_wake_triage(action_dicts)
    if suppressed_packets:
        wake_triage["suppressed_decision_packet_count"] = len(suppressed_packets)
        wake_triage["suppressed_decision_packets"] = suppressed_packets
    else:
        wake_triage["suppressed_decision_packet_count"] = 0
    return {
        "ok": not action_dicts,
        "board": board or kb.get_current_board(),
        "db_path": str(path),
        "actions": action_dicts,
        "wake_triage": wake_triage,
        "as_of": as_of,
        "mutation_applied": False,
    }


def _run_reconciler_pg(*, board, ready_age_seconds, now=None, pool=None):
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    from hermes_cli.kanban_board_doctor import _redacted_pg_dsn
    global _pg_partial_logged
    slug = board or kb.get_current_board()      # resolve once
    as_of = int(now if now is not None else time.time())
    db_path = _redacted_pg_dsn()
    partial = {"deferred_kinds": list(_DEFERRED_PG_RECONCILE_KINDS),
               "note": "PG reconcile omits PR-head-evidence + systemic-failure-"
                       "signature checks (Phase 6 B1); run sqlite reconcile for "
                       "full coverage."}
    if not _pg_partial_logged:
        logger.info("kanban reconcile (postgres): %d action kinds deferred: %s",
                    len(_DEFERRED_PG_RECONCILE_KINDS),
                    ", ".join(_DEFERRED_PG_RECONCILE_KINDS))
        _pg_partial_logged = True
    try:
        pool = pool or pg_pool.get_pool()
        with pool.connection(timeout=5) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        logger.warning("kanban reconcile (postgres): backend unreachable: %s",
                       type(exc).__name__)
        return {"ok": False, "board": slug, "db_path": db_path, "actions": [],
                "wake_triage": {"mode": "auto_silent", "wake_agent": False,
                                "suppressed_decision_packet_count": 0},
                "as_of": as_of, "mutation_applied": False, "partial": partial}
    store = PostgresKanbanStore(board=slug, pool=pool)
    with pool.connection() as conn:
        actions = _collect_reconcile_actions_pg(
            conn, slug, store, ready_age_seconds=ready_age_seconds, now=as_of)
    action_dicts = actions_to_dicts(actions)
    action_dicts, suppressed_packets = _filter_acknowledged_decision_packets_pg(
        store, action_dicts)
    wake_triage = classify_wake_triage(action_dicts)
    wake_triage["suppressed_decision_packet_count"] = len(suppressed_packets)
    if suppressed_packets:
        wake_triage["suppressed_decision_packets"] = suppressed_packets
    return {"ok": not action_dicts, "board": slug, "db_path": db_path,
            "actions": action_dicts, "wake_triage": wake_triage, "as_of": as_of,
            "mutation_applied": False, "partial": partial}


def _filter_acknowledged_decision_packets_pg(store, action_dicts):
    """Mirror ``_filter_acknowledged_decision_packets`` using PG store.list_comments."""
    triage = classify_wake_triage(action_dicts)
    suppressed_action_signatures: set[str] = set()
    suppressed_packets: list[dict[str, Any]] = []
    for packet in triage.get("decision_packets") or []:
        task_id = str(packet.get("task_id") or "").strip()
        packet_signature = str(packet.get("packet_signature") or "").strip()
        if not task_id or not packet_signature:
            continue
        comments = store.list_comments(task_id)
        applied_option: Optional[str] = None
        for option in ("keep_parked", "keep_blocked"):
            if any(_reconcile_decision_comment_matches(
                    c, option=option, packet_signature=packet_signature)
                   for c in comments):
                applied_option = option
                break
        if not applied_option:
            continue
        sigs = [str(a.get("signature") or "")
                for a in packet.get("actions") or [] if a.get("signature")]
        suppressed_action_signatures.update(sigs)
        suppressed_packets.append({
            "task_id": task_id, "packet_signature": packet_signature,
            "option": applied_option,
            "action_count": int(packet.get("action_count") or len(sigs)),
            "kinds": list(packet.get("kinds") or [])})
    if not suppressed_action_signatures:
        return action_dicts, []
    filtered = [a for a in action_dicts
                if str(a.get("signature") or "") not in suppressed_action_signatures]
    return filtered, suppressed_packets


def _apply_error(
    message: str,
    *,
    board: Optional[str],
    task_id: Optional[str],
    option: Optional[str],
    packet: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "board": board or kb.get_current_board(),
        "task_id": task_id,
        "option": option,
        "error": message,
        "packet": packet,
        "mutation_applied": False,
    }


def _reconcile_decision_applied_comment(
    *,
    option: str,
    packet_signature: str,
    category: Any,
    mutation: str,
) -> str:
    return (
        f"Jensen reconcile decision applied: option={option}; "
        f"packet_signature={packet_signature}; category={category}; "
        f"mutation={mutation}"
    )


def _reconcile_decision_comment_matches(
    comment: kb.Comment,
    *,
    option: str,
    packet_signature: str,
) -> bool:
    """Pure per-comment predicate for an applied-decision audit comment.

    Extracted so backend-specific packet filters (e.g. the PG path) can reuse
    the same marker test without an open sqlite connection. Behavior is
    identical to the inlined sqlite check.
    """
    return (
        "Jensen reconcile decision applied:" in comment.body
        and f"option={option};" in comment.body
        and f"packet_signature={packet_signature};" in comment.body
    )


def _find_existing_reconcile_decision_comment(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    option: str,
    packet_signature: str,
) -> Optional[kb.Comment]:
    """Return the first existing audit comment for an applied decision.

    Reconcile apply gates are safe to retry: a scheduler/operator may submit the
    same still-current packet more than once.  Treat an existing task comment
    with the same option and packet signature as the durable idempotency record
    rather than appending another comment.
    """
    for comment in kb.list_comments(conn, task_id):
        if _reconcile_decision_comment_matches(
            comment, option=option, packet_signature=packet_signature
        ):
            return comment
    return None


_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _validate_reconcile_pr_head_sha(value: str) -> str:
    sha = str(value or "").strip()
    if not _GIT_SHA_RE.match(sha):
        raise ValueError("pr_head_sha must be a 7-64 character hex git SHA")
    return sha.lower()


def _latest_completed_run_metadata(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    rows = kb.list_runs(conn, task_id)
    completed = [run for run in rows if run.outcome == "completed"]
    if not completed:
        return {}
    metadata = completed[0].metadata if isinstance(completed[0].metadata, dict) else {}
    return dict(metadata)


def _remediate_parent_closeout_pr_head(
    conn: sqlite3.Connection,
    *,
    parent_task_id: str,
    child_task_id: str,
    packet_signature: str,
    pr_head_sha: str,
) -> bool:
    parent = kb.get_task(conn, parent_task_id)
    if parent is None or parent.status not in {"done", "archived"}:
        return False
    result = parent.result or ""
    remediation_line = (
        f"[Reconcile remediation] Verified current PR head SHA for {child_task_id}: "
        f"{pr_head_sha}"
    )
    if pr_head_sha not in result:
        result = (result.rstrip() + "\n\n" + remediation_line).strip()
    metadata = _latest_completed_run_metadata(conn, parent_task_id)
    metadata.update({
        "pr_head_sha": pr_head_sha,
        "reconcile_remediation_for": child_task_id,
        "source": "kanban_reconcile_apply",
        "packet_signature": packet_signature,
    })
    return kb.edit_completed_task_result(
        conn,
        parent_task_id,
        result=result,
        summary=result,
        metadata=metadata,
    )


def _find_reconcile_action(
    actions: list[dict[str, Any]],
    *,
    task_id: str,
    kind: str,
    signature: str,
) -> Optional[dict[str, Any]]:
    for action in actions:
        if (
            action.get("kind") == kind
            and action.get("task_id") == task_id
            and action.get("signature") == signature
        ):
            return action
    return None


def _apply_clear_orphan_claim_lock(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    action_signature: str,
    action: dict[str, Any],
    board: Optional[str],
    path: Path,
    author: str,
    as_of: int,
) -> dict[str, Any]:
    comment = _reconcile_decision_applied_comment(
        option="clear_orphan_claim_lock",
        packet_signature=action_signature,
        category="orphan_claim_lock",
        mutation="clear_claim_lock",
    )
    with kb.write_txn(conn):
        row = conn.execute(
            """
            SELECT id, status, claim_lock, claim_expires
              FROM tasks
             WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["claim_lock"] is None
            or row["status"] in {"running", "done", "archived"}
        ):
            return _apply_error(
                "orphan claim lock is no longer present",
                board=board,
                task_id=task_id,
                option="clear_orphan_claim_lock",
                packet=action,
            )
        cur = conn.execute(
            """
            UPDATE tasks
               SET claim_lock = NULL,
                   claim_expires = NULL
             WHERE id = ?
               AND claim_lock IS NOT NULL
               AND status NOT IN ('running', 'done', 'archived')
            """,
            (task_id,),
        )
        if cur.rowcount != 1:
            return _apply_error(
                "orphan claim lock could not be cleared in its current state",
                board=board,
                task_id=task_id,
                option="clear_orphan_claim_lock",
                packet=action,
            )
        now = int(time.time())
        comment_cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, (author or "jensen").strip(), comment, now),
        )
        kb._append_event(
            conn,
            task_id,
            "reconcile_orphan_claim_lock_cleared",
            {
                "previous_claim_lock": row["claim_lock"],
                "previous_claim_expires": row["claim_expires"],
                "action_signature": action_signature,
                "source": "kanban_reconcile_apply",
            },
        )
    return {
        "ok": True,
        "board": board or kb.get_current_board(),
        "db_path": str(path),
        "task_id": task_id,
        "option": "clear_orphan_claim_lock",
        "packet_signature": action_signature,
        "packet": action,
        "plan": None,
        "comment_id": int(comment_cur.lastrowid or 0),
        "comment": comment,
        "mutation_applied": True,
        "mutation": "clear_claim_lock",
        "as_of": as_of,
    }


def _apply_reclaim_running_claim(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    option: str,
    action_kind: str,
    action_signature: str,
    action: dict[str, Any],
    board: Optional[str],
    path: Path,
    author: str,
    as_of: int,
) -> dict[str, Any]:
    if not bool(action.get("safe_to_apply")):
        return _apply_error(
            f"current {action_kind} action is advisory, not safe to apply",
            board=board,
            task_id=task_id,
            option=option,
            packet=action,
        )
    comment = _reconcile_decision_applied_comment(
        option=option,
        packet_signature=action_signature,
        category=action_kind,
        mutation="reclaim_running_claim",
    )
    if not kb.reclaim_task(
        conn,
        task_id,
        reason=f"kanban_reconcile_apply:{action_kind}:{action_signature}",
    ):
        return _apply_error(
            "running claim could not be reclaimed in its current state",
            board=board,
            task_id=task_id,
            option=option,
            packet=action,
        )
    comment_id = kb.add_comment(conn, task_id, (author or "jensen").strip(), comment)
    run_id = None
    try:
        events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "reclaimed"
        ]
        if events:
            run_id = events[0].run_id
    except Exception:
        run_id = None
    kb._append_event(
        conn,
        task_id,
        "reconcile_running_claim_reclaimed",
        {
            "option": option,
            "action_kind": action_kind,
            "action_signature": action_signature,
            "source": "kanban_reconcile_apply",
        },
        run_id=run_id,
    )
    return {
        "ok": True,
        "board": board or kb.get_current_board(),
        "db_path": str(path),
        "task_id": task_id,
        "option": option,
        "packet_signature": action_signature,
        "packet": action,
        "plan": None,
        "comment_id": comment_id,
        "comment": comment,
        "mutation_applied": True,
        "mutation": "reclaim_running_claim",
        "as_of": as_of,
    }


def _apply_close_stale_run_metadata(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    action_signature: str,
    action: dict[str, Any],
    board: Optional[str],
    path: Path,
    author: str,
    as_of: int,
) -> dict[str, Any]:
    option = "close_stale_run_metadata"
    if not bool(action.get("safe_to_apply")):
        return _apply_error(
            "current stale_run_metadata action is advisory, not safe to apply",
            board=board,
            task_id=task_id,
            option=option,
            packet=action,
        )
    details = action.get("details") or {}
    if not isinstance(details, dict) or details.get("run_id") is None:
        return _apply_error(
            "stale_run_metadata action is missing a valid run_id",
            board=board,
            task_id=task_id,
            option=option,
            packet=action,
        )
    try:
        run_id_int = int(details["run_id"])
    except (TypeError, ValueError):
        return _apply_error(
            "stale_run_metadata action is missing a valid run_id",
            board=board,
            task_id=task_id,
            option=option,
            packet=action,
        )
    comment = _reconcile_decision_applied_comment(
        option=option,
        packet_signature=action_signature,
        category="stale_run_metadata",
        mutation="close_stale_run_metadata",
    )
    with kb.write_txn(conn):
        row = conn.execute(
            """
            SELECT r.status, r.worker_pid, t.current_run_id
              FROM task_runs r
              JOIN tasks t ON t.id = r.task_id
             WHERE r.id = ? AND r.task_id = ?
            """,
            (run_id_int, task_id),
        ).fetchone()
        if row is None or row["status"] != "running":
            return _apply_error(
                "stale run metadata is no longer present",
                board=board,
                task_id=task_id,
                option=option,
                packet=action,
            )
        if row["current_run_id"] == run_id_int:
            return _apply_error(
                "run is current for task and cannot be closed as stale metadata",
                board=board,
                task_id=task_id,
                option=option,
                packet=action,
            )
        if _pid_alive(row["worker_pid"]):
            return _apply_error(
                "stale run worker PID is still alive; manual inspection required",
                board=board,
                task_id=task_id,
                option=option,
                packet=action,
            )
        now = int(time.time())
        cur = conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, 'stale run metadata closed by reconcile apply'),
                   error = COALESCE(error, ?), ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND task_id = ? AND status = 'running'
            """,
            (f"stale_run_metadata:{action_signature}", now, run_id_int, task_id),
        )
        if cur.rowcount != 1:
            return _apply_error(
                "stale run metadata could not be closed in its current state",
                board=board,
                task_id=task_id,
                option=option,
                packet=action,
            )
        comment_cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, (author or "jensen").strip(), comment, now),
        )
        kb._append_event(
            conn,
            task_id,
            "reconcile_stale_run_metadata_closed",
            {
                "run_id": run_id_int,
                "action_signature": action_signature,
                "source": "kanban_reconcile_apply",
            },
            run_id=run_id_int,
        )
    return {
        "ok": True,
        "board": board or kb.get_current_board(),
        "db_path": str(path),
        "task_id": task_id,
        "option": option,
        "packet_signature": action_signature,
        "packet": action,
        "plan": None,
        "comment_id": int(comment_cur.lastrowid or 0),
        "comment": comment,
        "mutation_applied": True,
        "mutation": "close_stale_run_metadata",
        "as_of": as_of,
    }


def apply_reconcile_decision(
    *,
    task_id: str,
    option: str,
    packet_signature: str,
    confirm_dry_run: bool,
    board: Optional[str] = None,
    ready_age_seconds: int = 15 * 60,
    author: str = "jensen",
    pr_head_sha: str = "",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Apply one explicitly gated reconcile operator decision.

    Every mutation path validates the current decision packet/action signature
    against a fresh reconcile pass before writing, so stale wake output cannot
    be applied blindly.
    """
    option = str(option or "").strip()
    task_id = str(task_id or "").strip()
    packet_signature = str(packet_signature or "").strip()
    if option not in {
        "keep_parked",
        "keep_blocked",
        "unblock",
        "close",
        "manual_review_with_stale_pr_risk",
        "remediate_parent_closeout",
        "clear_orphan_claim_lock",
        "reclaim_dead_running",
        "reclaim_expired_claim",
        "close_stale_run_metadata",
    }:
        return _apply_error(
            "unsupported reconcile apply option for gated apply",
            board=board,
            task_id=task_id or None,
            option=option or None,
        )
    if not task_id:
        return _apply_error(
            "task_id is required",
            board=board,
            task_id=None,
            option=option,
        )
    if not packet_signature:
        return _apply_error(
            "packet_signature is required",
            board=board,
            task_id=task_id,
            option=option,
        )
    if not confirm_dry_run:
        return _apply_error(
            "confirm_dry_run is required before applying a reconcile plan",
            board=board,
            task_id=task_id,
            option=option,
        )
    remediated_pr_head_sha = ""
    if option == "remediate_parent_closeout":
        try:
            remediated_pr_head_sha = _validate_reconcile_pr_head_sha(pr_head_sha)
        except ValueError as exc:
            return _apply_error(
                str(exc),
                board=board,
                task_id=task_id,
                option=option,
            )

    path = kb.kanban_db_path(board=board)
    as_of = int(now if now is not None else time.time())
    with kb.connect(path) as conn:
        actions = collect_reconcile_actions(
            conn,
            ready_age_seconds=ready_age_seconds,
            now=as_of,
        )
        action_dicts = actions_to_dicts(actions)
        if option == "clear_orphan_claim_lock":
            action = _find_reconcile_action(
                action_dicts,
                task_id=task_id,
                kind="orphan_claim_lock_observed",
                signature=packet_signature,
            )
            if action is None:
                return _apply_error(
                    "no current orphan claim-lock action matches task/signature",
                    board=board,
                    task_id=task_id,
                    option=option,
                )
            return _apply_clear_orphan_claim_lock(
                conn,
                task_id=task_id,
                action_signature=packet_signature,
                action=action,
                board=board,
                path=path,
                author=author,
                as_of=as_of,
            )

        if option in {"reclaim_dead_running", "reclaim_expired_claim"}:
            action_kind = (
                "dead_running_candidate"
                if option == "reclaim_dead_running"
                else "expired_claim_candidate"
            )
            action = _find_reconcile_action(
                action_dicts,
                task_id=task_id,
                kind=action_kind,
                signature=packet_signature,
            )
            if action is None:
                return _apply_error(
                    f"no current {action_kind} action matches task/signature",
                    board=board,
                    task_id=task_id,
                    option=option,
                )
            return _apply_reclaim_running_claim(
                conn,
                task_id=task_id,
                option=option,
                action_kind=action_kind,
                action_signature=packet_signature,
                action=action,
                board=board,
                path=path,
                author=author,
                as_of=as_of,
            )

        if option == "close_stale_run_metadata":
            action = _find_reconcile_action(
                action_dicts,
                task_id=task_id,
                kind="stale_run_metadata",
                signature=packet_signature,
            )
            if action is None:
                return _apply_error(
                    "no current stale_run_metadata action matches task/signature",
                    board=board,
                    task_id=task_id,
                    option=option,
                )
            return _apply_close_stale_run_metadata(
                conn,
                task_id=task_id,
                action_signature=packet_signature,
                action=action,
                board=board,
                path=path,
                author=author,
                as_of=as_of,
            )

        triage = classify_wake_triage(action_dicts)
        packet = _find_decision_packet(triage.get("decision_packets") or [], task_id)
        if packet is None:
            return _apply_error(
                "no current decision packet for task",
                board=board,
                task_id=task_id,
                option=option,
            )
        if packet.get("packet_signature") != packet_signature:
            return _apply_error(
                "packet_signature does not match current decision packet",
                board=board,
                task_id=task_id,
                option=option,
                packet=packet,
            )
        plan = (packet.get("operator_plans") or {}).get(option)
        if not isinstance(plan, dict):
            return _apply_error(
                "selected option is not available for current decision packet",
                board=board,
                task_id=task_id,
                option=option,
                packet=packet,
            )

        mutation = (
            "comment_only"
            if option in {"keep_parked", "keep_blocked"}
            else (
                "comment_and_unblock"
                if option in {"unblock", "manual_review_with_stale_pr_risk"}
                else ("parent_closeout_remediation" if option == "remediate_parent_closeout" else "comment_and_close")
            )
        )
        comment = _reconcile_decision_applied_comment(
            option=option,
            packet_signature=packet_signature,
            category=packet.get("primary_category"),
            mutation=mutation,
        )
        existing_comment = _find_existing_reconcile_decision_comment(
            conn,
            task_id,
            option=option,
            packet_signature=packet_signature,
        )
        if existing_comment is not None and option in {"keep_parked", "keep_blocked"}:
            return {
                "ok": True,
                "board": board or kb.get_current_board(),
                "db_path": str(path),
                "task_id": task_id,
                "option": option,
                "packet_signature": packet_signature,
                "packet": packet,
                "plan": plan,
                "comment_id": existing_comment.id,
                "comment": existing_comment.body,
                "mutation_applied": False,
                "mutation": mutation,
                "idempotent": True,
                "as_of": as_of,
            }

        comment_id = 0
        if option in {"keep_parked", "keep_blocked"}:
            comment_id = kb.add_comment(conn, task_id, author or "jensen", comment)
        elif option in {"unblock", "manual_review_with_stale_pr_risk"}:
            if not kb.unblock_task(conn, task_id):
                return _apply_error(
                    "selected option could not unblock task in its current state",
                    board=board,
                    task_id=task_id,
                    option=option,
                    packet=packet,
                )
            comment_id = kb.add_comment(conn, task_id, author or "jensen", comment)
        elif option == "close":
            result_text = "Closed by explicit Jensen reconcile decision after completed dependencies."
            metadata = {
                "reconcile_decision": "close",
                "source": "kanban_reconcile_apply",
                "packet_id": packet.get("packet_id"),
                "packet_signature": packet_signature,
            }
            try:
                closed = kb.complete_task(
                    conn,
                    task_id,
                    result=result_text,
                    summary=result_text,
                    metadata=metadata,
                )
            except kb.PRHeadGateError as exc:
                return _apply_error(
                    f"selected option could not close task: {exc}",
                    board=board,
                    task_id=task_id,
                    option=option,
                    packet=packet,
                )
            if not closed:
                return _apply_error(
                    "selected option could not close task in its current state",
                    board=board,
                    task_id=task_id,
                    option=option,
                    packet=packet,
                )
            comment_id = kb.add_comment(conn, task_id, author or "jensen", comment)
        elif option == "remediate_parent_closeout":
            parent_ids = list(packet.get("affected_parent_task_ids") or [])
            if not parent_ids:
                return _apply_error(
                    "current decision packet has no parent closeouts to remediate",
                    board=board,
                    task_id=task_id,
                    option=option,
                    packet=packet,
                )
            failed_parents: list[str] = []
            for parent_id in parent_ids:
                ok = _remediate_parent_closeout_pr_head(
                    conn,
                    parent_task_id=str(parent_id),
                    child_task_id=task_id,
                    packet_signature=packet_signature,
                    pr_head_sha=remediated_pr_head_sha,
                )
                if not ok:
                    failed_parents.append(str(parent_id))
            if failed_parents:
                return _apply_error(
                    "selected option could not remediate all parent closeouts",
                    board=board,
                    task_id=task_id,
                    option=option,
                    packet={**packet, "failed_parent_task_ids": failed_parents},
                )
            comment_id = kb.add_comment(conn, task_id, author or "jensen", comment)
        return {
            "ok": True,
            "board": board or kb.get_current_board(),
            "db_path": str(path),
            "task_id": task_id,
            "option": option,
            "packet_signature": packet_signature,
            "packet": packet,
            "plan": plan,
            "comment_id": comment_id,
            "comment": comment,
            "mutation_applied": True,
            "mutation": mutation,
            "as_of": as_of,
        }


def format_reconcile_apply_text(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        lines = [
            "Kanban reconcile apply rejected",
            f"- task_id={result.get('task_id')}",
            f"- option={result.get('option')}",
            f"- error={result.get('error')}",
        ]
        packet = result.get("packet") or {}
        if packet:
            lines.append(f"- current_packet_signature={packet.get('packet_signature')}")
        return "\n".join(lines)
    return "\n".join([
        "Kanban reconcile apply complete",
        f"- task_id={result.get('task_id')}",
        f"- option={result.get('option')}",
        f"- mutation={result.get('mutation')}",
        f"- mutation_applied={result.get('mutation_applied')}",
        f"- idempotent={bool(result.get('idempotent'))}",
        f"- comment_id={result.get('comment_id')}",
        "- postcheck: hermes kanban reconcile --json",
    ])


def _truncate_for_reconcile_text(value: Any, *, limit: int = 160) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _format_count_map_for_reconcile_text(value: Any, *, limit: int = 4) -> Optional[str]:
    if not isinstance(value, dict) or not value:
        return None
    items = sorted(value.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
    shown = [f"{key}={count}" for key, count in items[:limit]]
    omitted = len(items) - len(shown)
    if omitted > 0:
        shown.append(f"+{omitted} more")
    return ", ".join(shown)


def _detail_highlights_for_reconcile_text(action: dict[str, Any]) -> str:
    """Return bounded, human-scannable action details for Slack/terminal output."""
    details = action.get("details") or {}
    if not isinstance(details, dict) or not details:
        return ""

    fragments: list[str] = []
    for key in (
        "assignee",
        "status",
        "age_seconds",
        "parent_count",
        "parents",
        "workspace_kind",
        "workspace_path",
        "failure_signature",
        "task_count",
        "signature_threshold",
        "total_consecutive_failures",
        "skill",
        "run_id",
    ):
        if key in details and details.get(key) not in (None, "", [], {}):
            fragments.append(
                f"{key}={_truncate_for_reconcile_text(details.get(key), limit=120)}"
            )

    for key in ("validation_errors", "failure_classes", "trigger_outcomes"):
        value = details.get(key)
        if isinstance(value, list) and value:
            shown = [
                _truncate_for_reconcile_text(item, limit=90)
                for item in value[:3]
            ]
            if len(value) > len(shown):
                shown.append(f"+{len(value) - len(shown)} more")
            fragments.append(f"{key}=[" + "; ".join(shown) + "]")

    for key in ("status_counts", "assignee_counts"):
        formatted = _format_count_map_for_reconcile_text(details.get(key))
        if formatted:
            fragments.append(f"{key}={formatted}")

    task_examples = details.get("tasks")
    if isinstance(task_examples, list) and task_examples:
        shown_tasks: list[str] = []
        for task in task_examples[:3]:
            if isinstance(task, dict):
                task_id = task.get("task_id") or task.get("id") or "?"
                status = task.get("status") or "?"
                assignee = task.get("assignee") or "unassigned"
                shown_tasks.append(f"{task_id}/{status}/{assignee}")
            else:
                shown_tasks.append(_truncate_for_reconcile_text(task, limit=80))
        if len(task_examples) > len(shown_tasks):
            shown_tasks.append(f"+{len(task_examples) - len(shown_tasks)} more")
        fragments.append("tasks=[" + "; ".join(shown_tasks) + "]")

    if not fragments:
        # Preserve observability without dumping arbitrarily large nested JSON.
        keys = sorted(str(key) for key in details.keys())[:8]
        if keys:
            suffix = "" if len(details) <= len(keys) else f", +{len(details) - len(keys)} more"
            fragments.append("detail_keys=" + ",".join(keys) + suffix)

    return "; ".join(fragments[:8])


def _decision_packet_lines_for_reconcile_text(
    packets: list[dict[str, Any]],
    *,
    max_examples: int,
) -> list[str]:
    bounded_packets = packets[: max(0, int(max_examples or 0))]
    omitted = max(0, len(packets) - len(bounded_packets))
    lines = [
        f"Decision packets (first {len(bounded_packets)}; grouped by task; full payload: rerun with --json):"
    ]
    for packet in bounded_packets:
        loc = packet.get("task_id") or "board"
        safe = "safe" if packet.get("safe_to_apply") else "decision-only"
        kinds = ", ".join(str(kind) for kind in packet.get("kinds") or [])
        lines.append(
            f"- {str(packet.get('severity')).upper()} packet [{loc}] "
            f"({safe}; {int(packet.get('action_count') or 0)} action(s)): {kinds}"
        )
        if packet.get("primary_category"):
            lines.append(f"  category: {packet.get('primary_category')}")
        if packet.get("packet_signature"):
            lines.append(f"  packet_signature: {packet.get('packet_signature')}")
        options = packet.get("suggested_options") or []
        if isinstance(options, list) and options:
            lines.append("  options: " + ", ".join(str(option) for option in options[:5]))
        operator_plans = packet.get("operator_plans") or {}
        if isinstance(operator_plans, dict) and operator_plans:
            previews = []
            for option, plan in list(operator_plans.items())[:4]:
                if isinstance(plan, dict):
                    command_count = len(plan.get("commands") or [])
                    previews.append(f"{option}:{command_count} cmd(s)")
            if previews:
                lines.append("  dry-run plans: " + ", ".join(previews))
        next_step = packet.get("recommended_next_step")
        if next_step:
            lines.append(
                "  next: " + _truncate_for_reconcile_text(next_step, limit=180)
            )
        reasons = packet.get("reasons") or []
        if isinstance(reasons, list) and reasons:
            shown_reasons = [
                _truncate_for_reconcile_text(reason, limit=150)
                for reason in reasons[:2]
            ]
            if len(reasons) > len(shown_reasons):
                shown_reasons.append(f"+{len(reasons) - len(shown_reasons)} more")
            lines.append("  reasons: " + " | ".join(shown_reasons))
        signatures = packet.get("signatures") or []
        if isinstance(signatures, list) and signatures:
            shown_signatures = [str(signature) for signature in signatures[:3]]
            if len(signatures) > len(shown_signatures):
                shown_signatures.append(f"+{len(signatures) - len(shown_signatures)} more")
            lines.append("  signatures: " + ", ".join(shown_signatures))
    if omitted:
        lines.append(f"... {omitted} more decision packet(s) omitted from compact output; rerun with --json for full details.")
    return lines


def format_reconcile_text(result: dict[str, Any], *, max_examples: int = 5) -> str:
    actions = result.get("actions") or []
    if not actions:
        triage = result.get("wake_triage") or classify_wake_triage([])
        return (
            f"Kanban reconcile: no stalled-transition actions ({result.get('board')})\n"
            f"Wake triage: {triage.get('mode')} "
            f"(wake_agent={str(bool(triage.get('wake_agent'))).lower()})"
        )

    grouped: dict[tuple[str, str], int] = {}
    for action in actions:
        key = (str(action.get("severity")), str(action.get("kind")))
        grouped[key] = grouped.get(key, 0) + 1

    lines = [
        f"Kanban reconcile: {len(actions)} action(s) on {result.get('board')} ({result.get('db_path')})",
        "Summary:",
    ]
    for (severity, kind), count in sorted(
        grouped.items(),
        key=lambda item: (_SEVERITY_RANK.get(item[0][0], 99), item[0][1]),
    ):
        lines.append(f"- {severity.upper()} {kind}: {count}")

    triage = result.get("wake_triage") or classify_wake_triage(actions)
    triage_summary = triage.get("summary") or {}
    lines.extend([
        "Wake triage:",
        f"- mode={triage.get('mode')} wake_agent={str(bool(triage.get('wake_agent'))).lower()}",
        "- buckets: "
        f"{WAKE_BUCKET_AUTO_SILENT}={int(triage_summary.get(WAKE_BUCKET_AUTO_SILENT) or 0)}, "
        f"{WAKE_BUCKET_COMPACT_NOTIFY}={int(triage_summary.get(WAKE_BUCKET_COMPACT_NOTIFY) or 0)}, "
        f"{WAKE_BUCKET_JENSEN_DECISION_REQUIRED}={int(triage_summary.get(WAKE_BUCKET_JENSEN_DECISION_REQUIRED) or 0)}",
    ])
    packet_count = int(triage.get("decision_packet_count") or 0)
    if packet_count:
        lines.append(f"- decision_packets={packet_count}")
    lines.append(f"- reason: {_truncate_for_reconcile_text(triage.get('reason'), limit=180)}")

    decision_packets = triage.get("decision_packets") or []
    if triage.get("mode") == WAKE_BUCKET_JENSEN_DECISION_REQUIRED and decision_packets:
        lines.extend(_decision_packet_lines_for_reconcile_text(
            decision_packets,
            max_examples=max_examples,
        ))
        return "\n".join(lines)

    bounded_examples = actions[: max(0, int(max_examples or 0))]
    omitted = max(0, len(actions) - len(bounded_examples))
    lines.append(f"Examples (first {len(bounded_examples)}; full payload: rerun with --json):")
    for action in bounded_examples:
        loc = action.get("task_id") or "board"
        safe = "safe" if action.get("safe_to_apply") else "decision-only"
        reason = _truncate_for_reconcile_text(action.get("reason"), limit=180)
        lines.append(
            f"- {str(action.get('severity')).upper()} {action.get('kind')} "
            f"[{loc}] ({safe}): {reason}"
        )
        highlights = _detail_highlights_for_reconcile_text(action)
        if highlights:
            lines.append(f"  highlights: {highlights}")
        lines.append(f"  signature: {action.get('signature')}")
    if omitted:
        lines.append(f"... {omitted} more action(s) omitted from compact output; rerun with --json for full details.")
    return "\n".join(lines)
