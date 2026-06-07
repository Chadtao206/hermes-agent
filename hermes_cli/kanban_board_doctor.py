"""Deterministic Kanban board doctor checks.

Read-only by default. Intended for operators, cron watchdogs, and dashboard
health surfaces that need machine-readable stall/corruption signals without
mutating the hot Kanban board (SQLite or Postgres).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health

Issue = dict[str, Any]

_TERMINAL = {"done", "archived"}
_EXPLICIT_DECISION_GATE_STATUSES = {"blocked", "scheduled"}


def _alive(pid: Any) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _issue(severity: str, kind: str, message: str, **extra: Any) -> Issue:
    data: Issue = {"severity": severity, "kind": kind, "message": message}
    data.update({k: v for k, v in extra.items() if v is not None})
    return data


def _jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_title(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _format_status_path(path: list[str], tasks: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    for task_id in path:
        task = tasks.get(task_id) or {}
        parts.append(f"{task_id}:{task.get('status') or 'unknown'}")
    return " <- ".join(parts)


def _nearest_blocked_paths(
    start_id: str,
    dependency_parents: dict[str, list[str]],
    tasks: dict[str, dict[str, Any]],
    *,
    max_depth: int = 8,
) -> list[list[str]]:
    queue: list[list[str]] = [[start_id]]
    seen: set[tuple[str, ...]] = set()
    blocked_paths: list[list[str]] = []
    while queue:
        path = queue.pop(0)
        key = tuple(path)
        if key in seen:
            continue
        seen.add(key)
        current_id = path[-1]
        current = tasks.get(current_id) or {}
        status = str(current.get("status") or "")
        if status == "blocked":
            blocked_paths.append(path)
            continue
        if len(path) >= max_depth or status in _TERMINAL:
            continue
        for parent_id in dependency_parents.get(current_id, []):
            if parent_id in path:
                continue
            queue.append(path + [parent_id])
    return blocked_paths


def _append_graph_visibility_issues(
    issues: list[Issue],
    *,
    tasks: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    dependency_parents: dict[str, list[str]] = {}
    dependency_children: dict[str, list[str]] = {}
    supersedes_pairs: list[tuple[str, str]] = []
    for raw_link in links:
        parent_id = str(raw_link.get("parent_id") or "")
        child_id = str(raw_link.get("child_id") or "")
        relation = str(raw_link.get("relation_type") or "dependency")
        if relation == "dependency":
            dependency_parents.setdefault(child_id, []).append(parent_id)
            dependency_children.setdefault(parent_id, []).append(child_id)
        elif relation == "supersedes":
            supersedes_pairs.append((parent_id, child_id))

    for task_id, task in tasks.items():
        if str(task.get("status") or "") != "todo":
            continue
        direct_parents = dependency_parents.get(task_id) or []
        if not direct_parents:
            continue
        completed_parents = [
            parent_id
            for parent_id in direct_parents
            if str((tasks.get(parent_id) or {}).get("status") or "") in _TERMINAL
        ]
        if not completed_parents:
            continue
        blocked_paths: list[str] = []
        blocked_ancestors: set[str] = set()
        nonterminal_direct_parents: list[str] = []
        for parent_id in direct_parents:
            parent_status = str((tasks.get(parent_id) or {}).get("status") or "")
            if parent_status in _TERMINAL:
                continue
            nonterminal_direct_parents.append(f"{parent_id}:{parent_status or 'unknown'}")
            for path in _nearest_blocked_paths(parent_id, dependency_parents, tasks):
                blocked_paths.append(_format_status_path(path, tasks))
                if path:
                    blocked_ancestors.add(path[-1])
        if blocked_paths:
            issues.append(_issue(
                "warning",
                "todo_with_completed_parents_blocked_by_ancestor",
                "todo task already has completed dependency parents, but another dependency chain is blocked so promotion is intentionally suppressed",
                task_id=task_id,
                assignee=task.get("assignee"),
                completed_parents=", ".join(
                    f"{parent_id}:{(tasks.get(parent_id) or {}).get('status') or 'unknown'}"
                    for parent_id in completed_parents
                ),
                pending_parents=", ".join(nonterminal_direct_parents) or None,
                blocked_ancestors=sorted(blocked_ancestors) or None,
                blocked_paths=blocked_paths,
                action="inspect the blocked ancestor / human-decision gate rather than dispatcher health; downstream promotion resumes only after that chain is resolved",
            ))

    latest_runs: dict[str, dict[str, Any]] = {}
    for run in runs:
        task_id = str(run.get("task_id") or "")
        if not task_id:
            continue
        ts = int(run.get("ended_at") or run.get("started_at") or 0)
        prev = latest_runs.get(task_id)
        prev_ts = int(prev.get("ended_at") or prev.get("started_at") or 0) if prev else -1
        if prev is None or ts >= prev_ts:
            latest_runs[task_id] = run

    for task_id, run in latest_runs.items():
        task = tasks.get(task_id) or {}
        if str(task.get("status") or "") != "done":
            continue
        metadata = _jsonish(run.get("metadata"))
        if metadata.get("chad_decision_required") is not True:
            continue
        child_states = [
            f"{child_id}:{(tasks.get(child_id) or {}).get('status') or 'unknown'}"
            for child_id in dependency_children.get(task_id, [])
        ]
        explicit_gate_children = [
            child_id
            for child_id in dependency_children.get(task_id, [])
            if str((tasks.get(child_id) or {}).get("status") or "") in _EXPLICIT_DECISION_GATE_STATUSES
        ]
        if explicit_gate_children:
            continue
        issues.append(_issue(
            "warning",
            "completed_closeout_decision_flag_without_gate",
            "completed task metadata requested Chad decision, but there is no explicit blocked/scheduled downstream decision gate",
            task_id=task_id,
            assignee=task.get("assignee"),
            direct_child_states=", ".join(child_states) or None,
            action="create or link an explicit human-decision / Jensen checkpoint instead of relying on chad_decision_required metadata alone",
        ))

    for canonical_id, duplicate_id in supersedes_pairs:
        canonical = tasks.get(canonical_id) or {}
        duplicate = tasks.get(duplicate_id) or {}
        duplicate_status = str(duplicate.get("status") or "")
        if not canonical or not duplicate or duplicate_status in _TERMINAL:
            continue
        if _normalize_title(canonical.get("title")) != _normalize_title(duplicate.get("title")):
            continue
        if str(canonical.get("assignee") or "") != str(duplicate.get("assignee") or ""):
            continue
        issues.append(_issue(
            "warning",
            "superseded_duplicate_task",
            "superseded duplicate task is still active and can mislead dependency inspection",
            task_id=duplicate_id,
            assignee=duplicate.get("assignee"),
            superseded_by=canonical_id,
            duplicate_status=duplicate.get("status"),
            canonical_status=canonical.get("status"),
            action="archive or close the superseded duplicate and keep only the canonical replacement active in the dependency graph",
        ))


def _finalize_doctor_result(
    *,
    board: str,
    db_path: str,
    issues: list[Issue],
    ready_age_seconds: int,
    as_of: int,
) -> dict[str, Any]:
    reconcile_summary = _reconcile_summary(board=board, ready_age_seconds=ready_age_seconds)
    suppressed_blocked_tasks = {
        str(packet.get("task_id"))
        for packet in reconcile_summary.get("suppressed_decision_packets") or []
        if "blocked_with_completed_parents_decision" in (packet.get("kinds") or [])
    }
    if suppressed_blocked_tasks:
        before = len(issues)
        issues = [
            issue for issue in issues
            if not (
                issue.get("kind") == "blocked_with_completed_parents"
                and str(issue.get("task_id")) in suppressed_blocked_tasks
            )
        ]
        suppressed_count = before - len(issues)
        if suppressed_count:
            reconcile_summary["suppressed_doctor_issue_count"] = suppressed_count
    return {
        "ok": not issues,
        "board": board,
        "db_path": db_path,
        "issues": issues,
        "reconcile_summary": reconcile_summary,
        "as_of": as_of,
    }


def _quick_check(path: Path) -> Issue | None:
    repair_action = (
        "quiesced repair only: stop gateway/dashboard/cron writers, then run "
        "`hermes kanban repair-db --candidate <verified.db> --install --confirm-quiesced --confirm-freshness-checked` "
        "(add --allow-data-loss only when a human has accepted regression). "
        "Never cp/mv over kanban.db while services hold handles."
    )
    try:
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as handle:
                header = handle.read(16)
            if header != b"SQLite format 3\000":
                return _issue(
                    "critical",
                    "db_invalid_header",
                    "Kanban DB does not have a valid SQLite header",
                    first_16=header.hex(" "),
                    action=repair_action,
                )

        bundle = kanban_health.run_readonly_health_bundle(path)
        phase_map = {
            str(entry.get("phase")): entry
            for entry in bundle.get("phases", [])
            if entry.get("phase")
        }
        python_connect_ok = phase_map.get(kanban_health.PHASE_PYTHON_RO_CONNECT, {}).get("status") == "ok"
        python_select_ok = phase_map.get(kanban_health.PHASE_PYTHON_RO_SELECT_1, {}).get("status") == "ok"
        python_quick_ok = (
            phase_map.get(kanban_health.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK, {}).get("status") == "ok"
        )

        # Preserve prior doctor semantics: if Python read-only quick_check path is healthy,
        # do not fail the board based solely on sqlite3 CLI shape/environment noise.
        if python_connect_ok and python_select_ok and python_quick_ok:
            return None

        failed = [phase for phase in bundle.get("phases", []) if phase.get("status") == "failed"]
        if not failed:
            return None

        first_failure = failed[0]
        failed_phase = str(first_failure.get("phase") or "unknown")

        if failed_phase == kanban_health.PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK:
            quick_rows = [str(row) for row in (first_failure.get("quick_check_rows") or [])]
            first = quick_rows[0] if quick_rows else "no row"
            joined = " | ".join(quick_rows[:8])
            if any("kanban_notifier_heartbeats" in row for row in quick_rows):
                return _issue(
                    "warning",
                    "notifier_heartbeat_integrity",
                    f"Non-critical notifier heartbeat telemetry failed quick_check: {first}",
                    failed_phase=failed_phase,
                    quick_check_rows=quick_rows[:8],
                    health_phases=bundle.get("phases"),
                    action="reset ephemeral notifier telemetry only: DELETE FROM kanban_notifier_heartbeats; drop/recreate idx_notifier_heartbeats_*; do not recover or replace the main board DB unless other tables also fail",
                )
            if quick_rows:
                return _issue(
                    "critical",
                    "db_quick_check_failed",
                    f"PRAGMA quick_check returned {joined}",
                    failed_phase=failed_phase,
                    quick_check_rows=quick_rows[:8],
                    health_phases=bundle.get("phases"),
                    action=repair_action,
                )

        exc_class = first_failure.get("exception_class")
        exc_msg = first_failure.get("exception_message")
        detail = f"{exc_class}: {exc_msg}" if exc_class else str(first_failure.get("message") or "unknown failure")
        return _issue(
            "critical",
            "db_unreadable",
            f"Kanban DB is unreadable at phase {failed_phase}: {detail}",
            failed_phase=failed_phase,
            health_phases=bundle.get("phases"),
            action=repair_action,
        )
    except Exception as exc:
        return _issue(
            "critical",
            "db_unreadable",
            f"Kanban DB is unreadable: {type(exc).__name__}: {exc}",
            action=repair_action,
        )
    return None


def _redacted_pg_dsn() -> str:
    try:
        from hermes_cli.kanban import pg_pool
        import psycopg.conninfo as _ci
        d = _ci.conninfo_to_dict(pg_pool.resolve_dsn())
        return f"postgres://{d.get('host')}:{d.get('port')}/{d.get('dbname')}"
    except Exception:
        logger.debug("kanban doctor: could not resolve/redact PG DSN", exc_info=True)
        return "postgres://<unknown>"


def _run_board_doctor_pg(*, board: str | None, ready_age_seconds: int, pool=None) -> dict[str, Any]:
    from hermes_cli.kanban import pg_pool
    from psycopg.rows import dict_row
    slug = board or kb.get_current_board()
    now = int(time.time())
    issues: list[Issue] = []
    db_path = _redacted_pg_dsn()
    # 1. connectivity in place of sqlite file-integrity (bounded so an
    #    unreachable/misconfigured backend fails fast as a critical issue
    #    rather than hanging or crashing the doctor). Pool acquisition is inside
    #    the try so an unresolvable DSN (resolve_dsn RuntimeError) degrades too.
    try:
        pool = pool or pg_pool.get_pool()
        with pool.connection(timeout=5) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        issues.append(_issue(
            "critical", "pg_unreachable",
            f"Postgres kanban backend is unreachable: {type(exc).__name__}: {exc}",
            action="check the Supabase pooler DSN/credentials/network before relying on the board"))
        return {"ok": False, "board": slug, "db_path": db_path, "issues": issues,
                "reconcile_summary": {"ok": False, "backend": "postgres", "note": "unreachable"},
                "as_of": now}
    tasks_by_id: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    # 2. logical invariant checks (board-scoped PG SQL; same issue kinds as sqlite)
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # orphan dependency/rollup links
        cur.execute(
            "SELECT l.parent_id, l.child_id, l.relation_type, "
            "       p.id AS parent_exists, c.id AS child_exists "
            "FROM task_links l "
            "LEFT JOIN tasks p ON p.board=l.board AND p.id=l.parent_id "
            "LEFT JOIN tasks c ON c.board=l.board AND c.id=l.child_id "
            "WHERE l.board=%s AND (p.id IS NULL OR c.id IS NULL) "
            "ORDER BY l.parent_id, l.child_id", (slug,))
        for row in cur.fetchall():
            missing = []
            if row["parent_exists"] is None: missing.append("parent")
            if row["child_exists"] is None: missing.append("child")
            issues.append(_issue(
                "error", "orphan_task_link",
                f"task_links references missing {'/'.join(missing)} row",
                parent_id=row["parent_id"], child_id=row["child_id"],
                relation_type=row["relation_type"],
                action="remove/recreate the orphan link before relying on dependency promotion"))
        # profile event subscriptions pointing at missing tasks
        cur.execute(
            "SELECT s.task_id, s.profile, s.name FROM kanban_profile_event_subs s "
            "LEFT JOIN tasks t ON t.board=s.board AND t.id=s.task_id "
            "WHERE s.board=%s AND t.id IS NULL ORDER BY s.task_id, s.profile, s.name", (slug,))
        for row in cur.fetchall():
            issues.append(_issue(
                "error", "orphan_profile_event_subscription",
                "profile wake subscription references a missing task",
                task_id=row["task_id"], profile=row["profile"], name=row["name"],
                action="remove the subscription or recreate the task before enabling notifier wakes"))
        # running tasks with expired claim / dead worker / stale heartbeat
        cur.execute(
            "SELECT id, title, assignee, worker_pid, claim_expires, last_heartbeat_at, current_run_id "
            "FROM tasks WHERE board=%s AND status='running' ORDER BY started_at, created_at", (slug,))
        for row in cur.fetchall():
            pid_alive = _alive(row["worker_pid"])
            expired = bool(row["claim_expires"] and int(row["claim_expires"]) < now)
            stale_hb = bool(row["last_heartbeat_at"] and now - int(row["last_heartbeat_at"]) > 15 * 60)
            if not pid_alive or expired or stale_hb:
                issues.append(_issue(
                    "critical" if expired or not pid_alive else "warning",
                    "stale_running_task",
                    "running task has dead/missing worker, expired claim, or stale heartbeat",
                    task_id=row["id"], assignee=row["assignee"], worker_pid=row["worker_pid"],
                    pid_alive=pid_alive, claim_expired=expired,
                    heartbeat_age_seconds=(now - int(row["last_heartbeat_at"])) if row["last_heartbeat_at"] else None,
                    action="reclaim or inspect worker logs before retrying"))
        # stale run rows left marked running
        cur.execute(
            "SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at, "
            "       t.status AS task_status, t.current_run_id "
            "FROM task_runs r JOIN tasks t ON t.board=r.board AND t.id=r.task_id "
            "WHERE r.board=%s AND r.status='running' "
            "  AND (t.status != 'running' OR t.current_run_id IS NULL OR t.current_run_id != r.id) "
            "ORDER BY r.started_at DESC", (slug,))
        for row in cur.fetchall():
            issues.append(_issue(
                "warning", "stale_running_run",
                "task_run is still marked running but is not the task current running run",
                task_id=row["task_id"], run_id=row["run_id"], profile=row["profile"],
                task_status=row["task_status"], worker_pid=row["worker_pid"],
                pid_alive=_alive(row["worker_pid"]),
                action="mark/reconcile stale run metadata; do not treat it as an active worker"))
        # blocked tasks whose dependency parents are all terminal
        cur.execute(
            "SELECT c.id, c.title, c.assignee, COUNT(l.parent_id) AS parents, "
            "  SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) AS terminal_parents, "
            "  string_agg(p.id || ':' || p.status, ', ') AS parent_state "
            "FROM tasks c "
            "JOIN task_links l ON l.board=c.board AND l.child_id=c.id "
            "JOIN tasks p ON p.board=l.board AND p.id=l.parent_id "
            "WHERE c.board=%s AND c.status='blocked' "
            "  AND COALESCE(l.relation_type,'dependency')='dependency' "
            "GROUP BY c.id, c.title, c.assignee, c.created_at "
            "HAVING COUNT(l.parent_id) > 0 "
            "   AND COUNT(l.parent_id) = SUM(CASE WHEN p.status IN ('done','archived') THEN 1 ELSE 0 END) "
            "ORDER BY c.created_at", (slug,))
        for row in cur.fetchall():
            issues.append(_issue(
                "warning", "blocked_with_completed_parents",
                "blocked task has all dependency parents completed; likely needs an explicit unblock/re-review decision",
                task_id=row["id"], assignee=row["assignee"], parents=row["parent_state"],
                action="if remediation evidence is sufficient, run `hermes kanban unblock <task>`; otherwise park with a fresh blocker comment"))
        # ready tasks older than threshold
        cur.execute(
            "SELECT id, title, assignee, created_at FROM tasks "
            "WHERE board=%s AND status='ready' ORDER BY created_at", (slug,))
        for row in cur.fetchall():
            age = now - int(row["created_at"])
            if age >= ready_age_seconds:
                issues.append(_issue(
                    "warning", "old_ready_task",
                    "ready task has not been claimed within the threshold",
                    task_id=row["id"], assignee=row["assignee"], age_seconds=age,
                    action="check gateway dispatcher health and whether assignee profile exists"))
        cur.execute("SELECT id, title, assignee, status FROM tasks WHERE board=%s", (slug,))
        tasks_by_id = {str(row["id"]): dict(row) for row in cur.fetchall()}
        cur.execute(
            "SELECT parent_id, child_id, COALESCE(relation_type,'dependency') AS relation_type "
            "FROM task_links WHERE board=%s",
            (slug,),
        )
        links = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT id, task_id, started_at, ended_at, status, outcome, metadata "
            "FROM task_runs WHERE board=%s",
            (slug,),
        )
        runs = [dict(row) for row in cur.fetchall()]
    _append_graph_visibility_issues(issues, tasks=tasks_by_id, links=links, runs=runs)
    return _finalize_doctor_result(
        board=slug,
        db_path=db_path,
        issues=issues,
        ready_age_seconds=ready_age_seconds,
        as_of=now,
    )


def run_board_doctor(*, board: str | None = None, ready_age_seconds: int = 15 * 60) -> dict[str, Any]:
    try:
        from hermes_cli.kanban.store import resolve_backend
        _backend = resolve_backend()
    except Exception:
        _backend = "sqlite"
    if _backend == "postgres":
        return _run_board_doctor_pg(board=board, ready_age_seconds=ready_age_seconds)
    path = kb.kanban_db_path(board=board)
    now = int(time.time())
    issues: list[Issue] = []
    db_issue = _quick_check(path)
    if db_issue:
        issues.append(db_issue)
        if db_issue.get("severity") == "critical":
            return {"ok": False, "board": board or kb.get_current_board(), "db_path": str(path), "issues": issues, "as_of": now}

    tasks_by_id: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    with kb.snapshot_connect(board=board) as conn:
        # Orphan dependency/rollup links.
        for row in conn.execute(
            """
            SELECT l.parent_id, l.child_id, l.relation_type,
                   p.id AS parent_exists, c.id AS child_exists
              FROM task_links l
              LEFT JOIN tasks p ON p.id = l.parent_id
              LEFT JOIN tasks c ON c.id = l.child_id
             WHERE p.id IS NULL OR c.id IS NULL
             ORDER BY l.parent_id, l.child_id
            """
        ):
            missing = []
            if row["parent_exists"] is None:
                missing.append("parent")
            if row["child_exists"] is None:
                missing.append("child")
            issues.append(_issue(
                "error", "orphan_task_link",
                f"task_links references missing {'/'.join(missing)} row",
                parent_id=row["parent_id"], child_id=row["child_id"], relation_type=row["relation_type"],
                action="remove/recreate the orphan link before relying on dependency promotion",
            ))

        # Profile event subscriptions pointing at missing tasks.
        for row in conn.execute(
            """
            SELECT s.task_id, s.profile, s.name
              FROM kanban_profile_event_subs s
              LEFT JOIN tasks t ON t.id = s.task_id
             WHERE t.id IS NULL
             ORDER BY s.task_id, s.profile, s.name
            """
        ):
            issues.append(_issue(
                "error", "orphan_profile_event_subscription",
                "profile wake subscription references a missing task",
                task_id=row["task_id"], profile=row["profile"], name=row["name"],
                action="remove the subscription or recreate the task before enabling notifier wakes",
            ))

        # Running tasks with expired claim/dead worker.
        for row in conn.execute(
            """
            SELECT id, title, assignee, worker_pid, claim_expires, last_heartbeat_at, current_run_id
              FROM tasks
             WHERE status = 'running'
             ORDER BY started_at, created_at
            """
        ):
            pid_alive = _alive(row["worker_pid"])
            expired = bool(row["claim_expires"] and int(row["claim_expires"]) < now)
            stale_hb = bool(row["last_heartbeat_at"] and now - int(row["last_heartbeat_at"]) > 15 * 60)
            if not pid_alive or expired or stale_hb:
                issues.append(_issue(
                    "critical" if expired or not pid_alive else "warning",
                    "stale_running_task",
                    "running task has dead/missing worker, expired claim, or stale heartbeat",
                    task_id=row["id"], assignee=row["assignee"], worker_pid=row["worker_pid"],
                    pid_alive=pid_alive, claim_expired=expired,
                    heartbeat_age_seconds=(now - int(row["last_heartbeat_at"])) if row["last_heartbeat_at"] else None,
                    action="reclaim or inspect worker logs before retrying",
                ))

        # Stale run rows left marked running after task already moved on.
        for row in conn.execute(
            """
            SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, r.started_at,
                   t.status AS task_status, t.current_run_id
              FROM task_runs r
              JOIN tasks t ON t.id = r.task_id
             WHERE r.status = 'running'
               AND (t.status != 'running' OR t.current_run_id IS NULL OR t.current_run_id != r.id)
             ORDER BY r.started_at DESC
            """
        ):
            issues.append(_issue(
                "warning", "stale_running_run",
                "task_run is still marked running but is not the task current running run",
                task_id=row["task_id"], run_id=row["run_id"], profile=row["profile"], task_status=row["task_status"],
                worker_pid=row["worker_pid"], pid_alive=_alive(row["worker_pid"]),
                action="mark/reconcile stale run metadata; do not treat it as an active worker",
            ))

        # Blocked tasks whose dependency parents are all terminal: likely needs explicit requeue/unblock.
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
             ORDER BY c.created_at
            """
        ):
            issues.append(_issue(
                "warning", "blocked_with_completed_parents",
                "blocked task has all dependency parents completed; likely needs an explicit unblock/re-review decision",
                task_id=row["id"], assignee=row["assignee"], parents=row["parent_state"],
                action="if remediation evidence is sufficient, run `hermes kanban unblock <task>`; otherwise park with a fresh blocker comment",
            ))

        # Ready tasks old enough that dispatcher may not be picking them up.
        for row in conn.execute(
            """
            SELECT id, title, assignee, created_at
              FROM tasks
             WHERE status = 'ready'
             ORDER BY created_at
            """
        ):
            age = now - int(row["created_at"])
            if age >= ready_age_seconds:
                issues.append(_issue(
                    "warning", "old_ready_task",
                    "ready task has not been claimed within the threshold",
                    task_id=row["id"], assignee=row["assignee"], age_seconds=age,
                    action="check gateway dispatcher health and whether assignee profile exists",
                ))
        tasks_by_id = {
            str(row["id"]): dict(row)
            for row in conn.execute("SELECT id, title, assignee, status FROM tasks")
        }
        links = [
            dict(row)
            for row in conn.execute(
                "SELECT parent_id, child_id, COALESCE(relation_type, 'dependency') AS relation_type FROM task_links"
            )
        ]
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT id, task_id, started_at, ended_at, status, outcome, metadata FROM task_runs"
            )
        ]

    _append_graph_visibility_issues(issues, tasks=tasks_by_id, links=links, runs=runs)
    return _finalize_doctor_result(
        board=board or kb.get_current_board(),
        db_path=str(path),
        issues=issues,
        ready_age_seconds=ready_age_seconds,
        as_of=now,
    )


def _reconcile_summary(*, board: str | None, ready_age_seconds: int) -> dict[str, Any]:
    """Embed a compact reconciler summary without changing doctor ok semantics."""
    try:
        from hermes_cli import kanban_reconciler as rec

        result = rec.run_reconciler(
            board=board,
            ready_age_seconds=max(1, int(ready_age_seconds or 900)),
        )
        triage = result.get("wake_triage") or {}
        actions = result.get("actions") or []
        kinds: dict[str, int] = {}
        for action in actions:
            if isinstance(action, dict):
                kind = str(action.get("kind") or "unknown")
                kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "ok": bool(result.get("ok")),
            "action_count": len(actions),
            "wake_mode": triage.get("mode"),
            "wake_agent": bool(triage.get("wake_agent")),
            "suppressed_decision_packet_count": int(triage.get("suppressed_decision_packet_count") or 0),
            "suppressed_decision_packets": list(triage.get("suppressed_decision_packets") or []),
            "kinds": kinds,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def format_doctor_text(result: dict[str, Any]) -> str:
    issues = result.get("issues") or []
    if not issues:
        return f"Kanban board doctor: ok ({result.get('board')})"
    lines = [f"Kanban board doctor: {len(issues)} issue(s) on {result.get('board')} ({result.get('db_path')})"]
    for item in issues:
        loc = item.get("task_id") or item.get("parent_id") or item.get("run_id") or "board"
        lines.append(f"- {item['severity'].upper()} {item['kind']} [{loc}]: {item['message']}")
        if item.get("action"):
            lines.append(f"  action: {item['action']}")
        details = {k: v for k, v in item.items() if k not in {"severity", "kind", "message", "action"}}
        if details:
            lines.append("  details: " + json.dumps(details, sort_keys=True, default=str))
    return "\n".join(lines)
