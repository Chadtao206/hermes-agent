# hermes_cli/kanban/store_postgres.py
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hermes_cli.kanban_db import Task, Run, Event, Comment  # reuse dataclasses
from hermes_cli.kanban import pg_pool

_VALID_INITIAL_STATUSES = {"running", "blocked", "scheduled"}
_DEFAULT_NOTIFY_TERMINAL_KINDS = ("completed", "blocked", "gave_up",
                                  "crashed", "timed_out", "archived")
_UNSET = object()


def _new_task_id() -> str:
    return "t_" + secrets.token_hex(4)


class PostgresKanbanStore:
    """KanbanStore backed by Postgres (psycopg 3). Fresh implementation of the
    conformance-covered surface; board captured at construction. NOT a delegating
    adapter. Intricate kanban_db semantics are deferred (raise NotImplementedError)."""

    def __init__(self, board: Optional[str] = None, pool=None):
        self.board = board or "default"
        self._pool = pool or pg_pool.get_pool()

    def close(self) -> None:
        return None  # shared pool; owner closes it

    # --- helpers ---------------------------------------------------------
    def _emit(self, cur, task_id: str, kind: str, payload=None, run_id: Optional[int] = None) -> None:
        cur.execute(
            "INSERT INTO task_events (board, task_id, run_id, kind, payload, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (self.board, task_id, run_id, kind,
             Jsonb(payload) if payload is not None else None, int(time.time())),
        )

    def _row_to_task(self, row: dict) -> Task:
        d = dict(row)
        d.pop("board", None)
        for required in ("id", "title", "status", "priority", "created_at",
                         "workspace_kind"):
            if d.get(required) is None:
                raise ValueError(
                    f"_row_to_task: required column '{required}' missing/NULL")
        sk = d.get("skills")
        if isinstance(sk, str):
            try:
                parsed = json.loads(sk)
                d["skills"] = ([str(s) for s in parsed if s]
                               if isinstance(parsed, list) else None)
            except Exception:
                d["skills"] = None
        return Task(**{k: d.get(k) for k in Task.__dataclass_fields__})

    # --- task CRUD -------------------------------------------------------
    def create_task(self, *, title, body=None, assignee=None, created_by=None,
                    workspace_kind="scratch", workspace_path=None, branch_name=None,
                    tenant=None, priority=0, parents=(), triage=False,
                    idempotency_key=None, max_runtime_seconds=None, skills=None,
                    max_retries=None, initial_status="running", session_id=None,
                    **_ignored: Any) -> str:
        if initial_status not in _VALID_INITIAL_STATUSES:
            raise ValueError(
                f"initial_status must be one of {sorted(_VALID_INITIAL_STATUSES)}")
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                if idempotency_key:
                    cur.execute(
                        "SELECT id FROM tasks WHERE board=%s AND idempotency_key=%s "
                        "AND status != 'archived'", (self.board, idempotency_key))
                    hit = cur.fetchone()
                    if hit:
                        return hit["id"]
                if initial_status in ("blocked", "scheduled"):
                    status = initial_status
                elif triage:
                    status = "triage"
                else:
                    status = "ready"
                    if parents:
                        cur.execute(
                            "SELECT 1 FROM tasks WHERE board=%s AND id = ANY(%s) "
                            "AND status != 'done' LIMIT 1",
                            (self.board, list(parents)))
                        if cur.fetchone():
                            status = "todo"
                tid = _new_task_id()
                cur.execute(
                    "INSERT INTO tasks (board, id, title, body, assignee, status, "
                    "priority, created_by, created_at, workspace_kind, workspace_path, "
                    "branch_name, tenant, idempotency_key, max_runtime_seconds, skills, "
                    "max_retries, session_id) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (self.board, tid, title, body, assignee, status, priority,
                     created_by, now, workspace_kind, workspace_path, branch_name,
                     tenant, idempotency_key, max_runtime_seconds,
                     json.dumps(skills) if skills else None, max_retries, session_id))
                for p in parents:
                    cur.execute(
                        "INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                        "VALUES (%s,%s,%s,'dependency') ON CONFLICT DO NOTHING",
                        (self.board, p, tid))
                self._emit(cur, tid, "created", {
                    "assignee": assignee, "status": status,
                    "parents": list(parents), "tenant": tenant,
                    "branch_name": branch_name, "skills": skills})
                if status == "blocked":
                    self._emit(cur, tid, "blocked", {"reason": "initial_status=blocked"})
            return tid

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM tasks WHERE board=%s AND id=%s",
                        (self.board, task_id))
            row = cur.fetchone()
            return self._row_to_task(row) if row else None

    def list_tasks(self, *, assignee=None, status=None, tenant=None, session_id=None,
                   include_archived=False, limit=None, order_by=None,
                   workflow_template_id=None, current_step_key=None,
                   **_ignored: Any) -> list[Task]:
        if order_by:
            raise NotImplementedError(
                "phase-2-tail: list_tasks(order_by=...) not yet supported on postgres")
        clauses = ["board=%s"]
        params: list[Any] = [self.board]
        for col, val in (("assignee", assignee), ("status", status),
                         ("tenant", tenant), ("session_id", session_id),
                         ("workflow_template_id", workflow_template_id),
                         ("current_step_key", current_step_key)):
            if val is not None:
                clauses.append(f"{col}=%s")
                params.append(val)
        if not include_archived and status != "archived":
            clauses.append("status != 'archived'")
        sql = ("SELECT * FROM tasks WHERE " + " AND ".join(clauses) +
               " ORDER BY priority DESC, created_at ASC")
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [self._row_to_task(r) for r in cur.fetchall()]

    # --- status transitions ----------------------------------------------

    def block_task(self, task_id: str, *, reason=None, expected_run_id=None) -> bool:
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                current_run_id = row["current_run_id"] if row else None
                cur.execute(
                    "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL "
                    "WHERE board=%s AND id=%s AND status IN ('running','ready')",
                    (self.board, task_id))
                if cur.rowcount == 1:
                    if current_run_id is not None:
                        cur.execute(
                            "UPDATE task_runs SET status=%s, outcome=%s, ended_at=%s, "
                            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                            "WHERE board=%s AND id=%s AND ended_at IS NULL",
                            ('blocked', 'blocked', now, self.board, current_run_id))
                        cur.execute(
                            "UPDATE tasks SET current_run_id=NULL "
                            "WHERE board=%s AND id=%s",
                            (self.board, task_id))
                    self._emit(cur, task_id, "blocked", {"reason": reason})
                    return True
                return False

    def unblock_task(self, task_id: str) -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT 1 FROM task_links l "
                    "JOIN tasks t ON t.board=l.board AND t.id=l.parent_id "
                    "WHERE l.board=%s AND l.child_id=%s AND l.relation_type='dependency' "
                    "AND t.status NOT IN ('done','archived') LIMIT 1",
                    (self.board, task_id))
                has_incomplete_parent = cur.fetchone() is not None
                target = "todo" if has_incomplete_parent else "ready"
                cur.execute(
                    "UPDATE tasks SET status=%s, current_run_id=NULL, "
                    "consecutive_failures=0, last_failure_error=NULL "
                    "WHERE board=%s AND id=%s AND status IN ('blocked','scheduled')",
                    (target, self.board, task_id))
                if cur.rowcount == 1:
                    payload = {"status": "todo"} if target == "todo" else None
                    self._emit(cur, task_id, "unblocked", payload)
                    return True
                return False

    def schedule_task(self, task_id: str, *, reason=None, expected_run_id=None) -> bool:
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                current_run_id = row["current_run_id"] if row else None
                cur.execute(
                    "UPDATE tasks SET status='scheduled', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL "
                    "WHERE board=%s AND id=%s AND status IN "
                    "('todo','ready','running','blocked')",
                    (self.board, task_id))
                if cur.rowcount == 1:
                    if current_run_id is not None:
                        cur.execute(
                            "UPDATE task_runs SET status=%s, outcome=%s, ended_at=%s, "
                            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                            "WHERE board=%s AND id=%s AND ended_at IS NULL",
                            ('scheduled', 'scheduled', now, self.board, current_run_id))
                        cur.execute(
                            "UPDATE tasks SET current_run_id=NULL "
                            "WHERE board=%s AND id=%s",
                            (self.board, task_id))
                    self._emit(cur, task_id, "scheduled", {"reason": reason})
                    return True
                return False

    def archive_task(self, task_id: str) -> bool:
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                current_run_id = row["current_run_id"] if row else None
                cur.execute(
                    "UPDATE tasks SET status='archived', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL "
                    "WHERE board=%s AND id=%s AND status != 'archived'",
                    (self.board, task_id))
                if cur.rowcount == 1:
                    if current_run_id is not None:
                        cur.execute(
                            "UPDATE task_runs SET status=%s, outcome=%s, ended_at=%s, "
                            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                            "WHERE board=%s AND id=%s AND ended_at IS NULL",
                            ('reclaimed', 'reclaimed', now, self.board, current_run_id))
                        cur.execute(
                            "UPDATE tasks SET current_run_id=NULL "
                            "WHERE board=%s AND id=%s",
                            (self.board, task_id))
                    self._emit(cur, task_id, "archived")
                    result = True
                else:
                    result = False
        if result:
            self.recompute_ready()
        return result

    def assign_task(self, task_id: str, profile) -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT status, claim_lock FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                if row and row["status"] == "running" and row["claim_lock"] is not None:
                    raise RuntimeError("cannot reassign a running, claimed task")
                cur.execute(
                    "UPDATE tasks SET assignee=%s, consecutive_failures=0, "
                    "last_failure_error=NULL WHERE board=%s AND id=%s",
                    (profile, self.board, task_id))
                if cur.rowcount == 1:
                    self._emit(cur, task_id, "assigned", {"assignee": profile})
                    return True
                return False

    def reassign_task(self, task_id: str, profile, *, reclaim_first=False, reason=None) -> bool:
        if reclaim_first:
            self.reclaim_task(task_id, reason=reason)
        else:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT status FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                if row and row["status"] == "running":
                    return False
        return self.assign_task(task_id, profile)

    def reclaim_task(self, task_id: str, *, reason=None) -> bool:
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                current_run_id = row["current_run_id"] if row else None
                cur.execute(
                    "UPDATE tasks SET status='ready', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, consecutive_failures=0 "
                    "WHERE board=%s AND id=%s AND "
                    "(status='running' OR claim_lock IS NOT NULL)",
                    (self.board, task_id))
                if cur.rowcount == 1:
                    if current_run_id is not None:
                        cur.execute(
                            "UPDATE task_runs SET status=%s, outcome=%s, ended_at=%s, "
                            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                            "WHERE board=%s AND id=%s AND ended_at IS NULL",
                            ('reclaimed', 'reclaimed', now, self.board, current_run_id))
                        cur.execute(
                            "UPDATE tasks SET current_run_id=NULL "
                            "WHERE board=%s AND id=%s",
                            (self.board, task_id))
                    self._emit(cur, task_id, "reclaimed", {"manual": True, "reason": reason})
                    return True
                return False

    def set_status_direct(self, task_id: str, new_status: str) -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                if new_status == "ready":
                    cur.execute(
                        "SELECT 1 FROM task_links l "
                        "JOIN tasks t ON t.board=l.board AND t.id=l.parent_id "
                        "WHERE l.board=%s AND l.child_id=%s "
                        "AND l.relation_type='dependency' AND t.status NOT IN ('done','archived') LIMIT 1",
                        (self.board, task_id))
                    if cur.fetchone() is not None:
                        return False
                cur.execute(
                    "SELECT status FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                old_row = cur.fetchone()
                old_status = old_row["status"] if old_row else None
                cur.execute(
                    "UPDATE tasks SET status=%s, "
                    "claim_lock=(CASE WHEN %s='running' THEN claim_lock ELSE NULL END), "
                    "claim_expires=(CASE WHEN %s='running' THEN claim_expires ELSE NULL END), "
                    "worker_pid=(CASE WHEN %s='running' THEN worker_pid ELSE NULL END) "
                    "WHERE board=%s AND id=%s",
                    (new_status, new_status, new_status, new_status, self.board, task_id))
                if cur.rowcount == 1:
                    self._emit(cur, task_id, "status", {"status": new_status})
                    if old_status in ("done", "archived") and new_status not in ("done", "archived"):
                        cur.execute(
                            "SELECT child_id FROM task_links WHERE board=%s AND parent_id=%s "
                            "AND relation_type='dependency'", (self.board, task_id))
                        child_ids = [r["child_id"] for r in cur.fetchall()]
                        for cid in child_ids:
                            cur.execute(
                                "UPDATE tasks SET status='todo' WHERE board=%s AND id=%s "
                                "AND status='ready'", (self.board, cid))
                            if cur.rowcount == 1:
                                self._emit(cur, cid, "status", {"status": "todo"})
                    result = True
                else:
                    result = False
        if result and new_status in ("done", "ready"):
            self.recompute_ready()
        return result

    def set_task_priority(self, task_id: str, priority: int) -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE tasks SET priority=%s WHERE board=%s AND id=%s",
                    (priority, self.board, task_id))
                if cur.rowcount == 1:
                    self._emit(cur, task_id, "reprioritized", {"priority": priority})
                    return True
                return False

    def edit_task_fields(self, task_id: str, *, title=None, body=None) -> bool:
        if title is None and body is None:
            return False
        if title is not None and not str(title).strip():
            raise ValueError("title cannot be empty")
        sets = []
        params: list = []
        if title is not None:
            sets.append("title=%s")
            params.append(title)
        if body is not None:
            sets.append("body=%s")
            params.append(body)
        params.extend([self.board, task_id])
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE board=%s AND id=%s",
                    tuple(params))
                if cur.rowcount == 1:
                    self._emit(cur, task_id, "edited")
                    return True
                return False

    def delete_task(self, task_id: str) -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "DELETE FROM task_links WHERE board=%s AND (parent_id=%s OR child_id=%s)",
                    (self.board, task_id, task_id))
                cur.execute(
                    "DELETE FROM task_comments WHERE board=%s AND task_id=%s",
                    (self.board, task_id))
                cur.execute(
                    "DELETE FROM task_events WHERE board=%s AND task_id=%s",
                    (self.board, task_id))
                cur.execute(
                    "DELETE FROM task_runs WHERE board=%s AND task_id=%s",
                    (self.board, task_id))
                cur.execute(
                    "DELETE FROM kanban_notify_subs WHERE board=%s AND task_id=%s",
                    (self.board, task_id))
                cur.execute(
                    "DELETE FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                result = cur.rowcount == 1
        if result:
            self.recompute_ready()
        return result

    def promote_task(self, task_id: str, *, actor, reason=None, force=False,
                     dry_run=False) -> tuple:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT status FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                if row is None:
                    return (False, f"task {task_id} not found")
                status = row["status"]
                if status not in ("todo", "blocked"):
                    return (False,
                            f"task {task_id} is '{status}'; "
                            "promote only applies to 'todo' or 'blocked'")
                if not force:
                    cur.execute(
                        "SELECT 1 FROM task_links l "
                        "JOIN tasks t ON t.board=l.board AND t.id=l.parent_id "
                        "WHERE l.board=%s AND l.child_id=%s "
                        "AND l.relation_type='dependency' "
                        "AND t.status NOT IN ('done','archived') LIMIT 1",
                        (self.board, task_id))
                    if cur.fetchone() is not None:
                        return (False, "blocked by incomplete parent(s)")
                if dry_run:
                    return (True, None)
                cur.execute(
                    "UPDATE tasks SET status='ready' WHERE board=%s AND id=%s "
                    "AND status IN ('todo','blocked')",
                    (self.board, task_id))
                if cur.rowcount == 1:
                    self._emit(cur, task_id, "promoted_manual",
                               {"actor": actor, "reason": reason, "forced": force})
                    return (True, None)
                return (False, f"task {task_id} status changed concurrently; promote had no effect")

    def recompute_ready(self) -> int:
        # Phase-2 simplification: sticky-block re-promotion deferred (phase-2-tail);
        # only 'todo' tasks are recomputed.
        count = 0
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, status FROM tasks WHERE board=%s AND status IN ('todo','blocked')",
                (self.board,))
            candidates = cur.fetchall()
        for row in candidates:
            tid = row["id"]
            status = row["status"]
            if status == "blocked":
                continue
            # status == "todo": promote if all dependency parents are done/archived
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "SELECT count(*) AS n FROM task_links l "
                        "JOIN tasks t ON t.board=l.board AND t.id=l.parent_id "
                        "WHERE l.board=%s AND l.child_id=%s "
                        "AND l.relation_type='dependency' "
                        "AND t.status NOT IN ('done','archived')",
                        (self.board, tid))
                    n = cur.fetchone()["n"]
                    if n == 0:
                        cur.execute(
                            "UPDATE tasks SET status='ready', consecutive_failures=0, "
                            "last_failure_error=NULL "
                            "WHERE board=%s AND id=%s AND status='todo'",
                            (self.board, tid))
                        if cur.rowcount == 1:
                            self._emit(cur, tid, "promoted")
                            count += 1
        return count

    def has_spawnable_ready(self) -> bool:
        # Phase-2 simplification: on-disk profile validation deferred (phase-2-tail);
        # returns True if any ready+assigned+unclaimed task exists.
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE board=%s AND status='ready' "
                "AND assignee IS NOT NULL AND claim_lock IS NULL",
                (self.board,))
            return cur.fetchone()["n"] > 0
