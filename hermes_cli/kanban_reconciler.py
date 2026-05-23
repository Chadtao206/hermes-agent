"""Read-only Kanban stalled-transition reconciler.

Phase 1A intentionally does not mutate the board.  It classifies the same
families of orchestration stalls that the board doctor can observe, but returns
explicit action records suitable for Jensen/operator decision queues.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb


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
    "old_review_nonspawnable",
    "old_review_spawnable",
    "pre_spawn_validation_decision",
    "repeated_failure_signature_decision",
    "review_skill_provenance_missing",
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
        packet["safe_to_apply"] = bool(packet["safe_to_apply"] and action.get("safe_to_apply"))
        packet["severity"] = _highest_severity([
            str(packet.get("severity") or "warning"),
            str(action.get("severity") or "warning"),
        ])

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
    canonical = json.dumps(
        {k: _jsonable(v) for k, v in sorted(material.items())},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{task_id or 'board'}:{digest}"


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


def _has_sdlc_review_skill() -> bool:
    """Best-effort local inventory check for the legacy review skill.

    Phase 1A never blocks on this.  It is used only to produce a diagnostic
    action when review work exists and the hard-coded review skill cannot be
    found in the usual root/profile skill stores.
    """
    roots: list[Path] = []
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home
        default_root = get_default_hermes_root()
        roots.append(default_root / "skills")
        roots.append(get_hermes_home() / "skills")
        profiles_root = default_root / "profiles"
        if profiles_root.is_dir():
            roots.extend(p / "skills" for p in profiles_root.iterdir() if p.is_dir())
    except Exception:
        roots.append(Path.home() / ".hermes" / "skills")

    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved in seen or not root.is_dir():
            continue
        seen.add(resolved)
        direct = root / "sdlc-review" / "SKILL.md"
        if direct.is_file():
            return True
        try:
            for candidate in root.rglob("sdlc-review/SKILL.md"):
                if candidate.is_file():
                    return True
        except OSError:
            continue
    return False


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
    except Exception as exc:
        errors.append(f"workspace validation failed: {exc}")

    if profile_ok:
        skills = list(task.skills or [])
        if task.status == "review" and "sdlc-review" not in skills:
            # Review dispatch force-loads sdlc-review immediately before
            # spawn. Validate that implicit skill too, otherwise the worker
            # can fail at CLI startup before doing any review work.
            skills.append("sdlc-review")
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
            actions.append(_action(
                "expired_claim_candidate",
                row["id"],
                "warning",
                "running task claim has expired",
                safe_to_apply=False,
                details={**base, "seconds_expired": as_of - int(claim_expires)},
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
        actions.append(_action(
            "stale_run_metadata",
            row["task_id"],
            "warning",
            "task_run is marked running but is not the task current active run",
            safe_to_apply=False,
            details={
                "run_id": row["run_id"],
                "profile": row["profile"],
                "worker_pid": row["worker_pid"],
                "pid_alive": _pid_alive(row["worker_pid"]),
                "task_status": row["task_status"],
                "current_run_id": row["current_run_id"],
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
         WHERE status IN ('ready','review')
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

    for status in ("ready", "review"):
        for row in conn.execute(
            """
            SELECT id, title, assignee, created_at
              FROM tasks
             WHERE status = ? AND claim_lock IS NULL
             ORDER BY created_at, id
            """,
            (status,),
        ):
            age = as_of - int(row["created_at"])
            if age < ready_age_seconds:
                continue
            spawnable = _profile_spawnable(row["assignee"])
            kind = f"old_{status}_{'spawnable' if spawnable else 'nonspawnable'}"
            actions.append(_action(
                kind,
                row["id"],
                "warning" if spawnable else "info",
                f"{status} task has not been claimed within threshold",
                safe_to_apply=False,
                details={
                    "assignee": row["assignee"],
                    "age_seconds": age,
                    "created_at": row["created_at"],
                    "spawnable_profile": spawnable,
                },
            ))

    review_count = int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'review'"
    ).fetchone()[0])
    if review_count and not _has_sdlc_review_skill():
        actions.append(_action(
            "review_skill_provenance_missing",
            None,
            "warning",
            "review dispatch injects sdlc-review, but the skill was not found locally; diagnostic only",
            safe_to_apply=False,
            details={"review_task_count": review_count, "skill": "sdlc-review"},
        ))

    return _sort_actions(actions)


def actions_to_dicts(actions: list[ReconcileAction]) -> list[dict[str, Any]]:
    return [asdict(action) for action in actions]


def run_reconciler(
    *,
    board: Optional[str] = None,
    ready_age_seconds: int = 15 * 60,
    now: Optional[int] = None,
) -> dict[str, Any]:
    path = kb.kanban_db_path(board=board)
    as_of = int(now if now is not None else time.time())
    with _snapshot_connect(path) as conn:
        actions = collect_reconcile_actions(
            conn,
            ready_age_seconds=ready_age_seconds,
            now=as_of,
        )
    action_dicts = actions_to_dicts(actions)
    return {
        "ok": not actions,
        "board": board or kb.get_current_board(),
        "db_path": str(path),
        "actions": action_dicts,
        "wake_triage": classify_wake_triage(action_dicts),
        "as_of": as_of,
        "mutation_applied": False,
    }


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
        "review_task_count",
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
