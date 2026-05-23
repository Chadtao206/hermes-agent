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
    return {
        "ok": not actions,
        "board": board or kb.get_current_board(),
        "db_path": str(path),
        "actions": actions_to_dicts(actions),
        "as_of": as_of,
        "mutation_applied": False,
    }


def format_reconcile_text(result: dict[str, Any]) -> str:
    actions = result.get("actions") or []
    if not actions:
        return f"Kanban reconcile: no stalled-transition actions ({result.get('board')})"

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

    lines.append("Actions:")
    for action in actions:
        loc = action.get("task_id") or "board"
        safe = "safe" if action.get("safe_to_apply") else "decision-only"
        lines.append(
            f"- {str(action.get('severity')).upper()} {action.get('kind')} "
            f"[{loc}] ({safe}): {action.get('reason')}"
        )
        lines.append(f"  signature: {action.get('signature')}")
        details = action.get("details") or {}
        if details:
            lines.append("  details: " + json.dumps(details, sort_keys=True, default=str))
    return "\n".join(lines)
