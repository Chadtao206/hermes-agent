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

    # --- links -----------------------------------------------------------

    def link_tasks(self, parent_id: str, child_id: str, *,
                   relation_type: str = "dependency") -> None:
        # Phase-2: cycle detection deferred (phase-2-tail)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                for tid in (parent_id, child_id):
                    cur.execute(
                        "SELECT id FROM tasks WHERE board=%s AND id=%s",
                        (self.board, tid))
                    if cur.fetchone() is None:
                        raise ValueError(f"task {tid} not found")
                cur.execute(
                    "INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (board,parent_id,child_id) DO NOTHING",
                    (self.board, parent_id, child_id, relation_type))
                if relation_type == "dependency":
                    cur.execute(
                        "SELECT status FROM tasks WHERE board=%s AND id=%s",
                        (self.board, parent_id))
                    parent_row = cur.fetchone()
                    if parent_row and parent_row["status"] != "done":
                        cur.execute(
                            "UPDATE tasks SET status='todo' "
                            "WHERE board=%s AND id=%s AND status='ready'",
                            (self.board, child_id))
                self._emit(cur, child_id, "linked",
                           {"parent": parent_id, "child": child_id,
                            "relation_type": relation_type})

    def unlink_tasks(self, parent_id: str, child_id: str, *,
                     relation_type: str = "dependency") -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "DELETE FROM task_links "
                    "WHERE board=%s AND parent_id=%s AND child_id=%s AND relation_type=%s",
                    (self.board, parent_id, child_id, relation_type))
                deleted = cur.rowcount > 0
                if deleted:
                    self._emit(cur, child_id, "unlinked",
                               {"parent": parent_id, "child": child_id,
                                "relation_type": relation_type})
        if deleted and relation_type == "dependency":
            self.recompute_ready()
        return deleted

    def parent_ids(self, task_id: str, *,
                   relation_type: Optional[str] = "dependency") -> list:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if relation_type is None:
                cur.execute(
                    "SELECT parent_id FROM task_links "
                    "WHERE board=%s AND child_id=%s ORDER BY parent_id",
                    (self.board, task_id))
            else:
                cur.execute(
                    "SELECT parent_id FROM task_links "
                    "WHERE board=%s AND child_id=%s AND relation_type=%s ORDER BY parent_id",
                    (self.board, task_id, relation_type))
            return [r["parent_id"] for r in cur.fetchall()]

    def child_ids(self, task_id: str, *,
                  relation_type: Optional[str] = "dependency") -> list:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if relation_type is None:
                cur.execute(
                    "SELECT child_id FROM task_links "
                    "WHERE board=%s AND parent_id=%s ORDER BY child_id",
                    (self.board, task_id))
            else:
                cur.execute(
                    "SELECT child_id FROM task_links "
                    "WHERE board=%s AND parent_id=%s AND relation_type=%s ORDER BY child_id",
                    (self.board, task_id, relation_type))
            return [r["child_id"] for r in cur.fetchall()]

    # --- comments --------------------------------------------------------

    def add_comment(self, task_id: str, *, author: str, body: str) -> int:
        if not author:
            raise ValueError("author cannot be empty")
        if not body:
            raise ValueError("body cannot be empty")
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                if cur.fetchone() is None:
                    raise ValueError(f"task {task_id} not found")
                now = int(time.time())
                cur.execute(
                    "INSERT INTO task_comments (board, task_id, author, body, created_at) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (self.board, task_id, author, body, now))
                comment_id = cur.fetchone()["id"]
                self._emit(cur, task_id, "commented",
                           {"author": author, "len": len(body)})
        return comment_id

    def list_comments(self, task_id: str) -> list:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM task_comments "
                "WHERE board=%s AND task_id=%s ORDER BY created_at ASC",
                (self.board, task_id))
            return [Comment(**{k: r[k] for k in Comment.__dataclass_fields__})
                    for r in cur.fetchall()]

    # --- events ----------------------------------------------------------

    def list_events(self, task_id: str, **kwargs) -> list:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM task_events "
                "WHERE board=%s AND task_id=%s ORDER BY created_at ASC, id ASC",
                (self.board, task_id))
            return [Event(**{k: r[k] for k in Event.__dataclass_fields__})
                    for r in cur.fetchall()]

    def gc_events(self, *, older_than_seconds: int = 30 * 24 * 3600,
                  **kwargs) -> int:
        cutoff = int(time.time()) - older_than_seconds
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "DELETE FROM task_events "
                "WHERE board=%s AND created_at < %s "
                "AND task_id IN ("
                "  SELECT id FROM tasks WHERE board=%s AND status IN ('done','archived')"
                ")",
                (self.board, cutoff, self.board))
            return cur.rowcount

    # --- workspace -------------------------------------------------------

    def set_workspace_path(self, task_id: str, path) -> None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE tasks SET workspace_path=%s WHERE board=%s AND id=%s",
                (str(path), self.board, task_id))

    # --- notify subscriptions --------------------------------------------

    def add_notify_sub(self, *, task_id, platform, chat_id, thread_id=None,
                       user_id=None, notifier_profile=None, event_kinds=None,
                       include_children=None) -> None:
        thread_id = thread_id or ''
        now = int(time.time())
        pk = (self.board, task_id, platform, chat_id, thread_id)
        ek_insert = json.dumps(event_kinds) if isinstance(event_kinds, list) else None
        ic_insert = 1 if include_children else 0
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "INSERT INTO kanban_notify_subs "
                    "(board,task_id,platform,chat_id,thread_id,user_id,notifier_profile,"
                    "created_at,last_event_id,event_kinds,include_children) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s) "
                    "ON CONFLICT (board,task_id,platform,chat_id,thread_id) DO NOTHING",
                    (*pk, user_id, notifier_profile, now, ek_insert, ic_insert),
                )
                # Selective backfill UPDATEs so they apply whether row was just inserted or pre-existed
                if notifier_profile is not None:
                    cur.execute(
                        "UPDATE kanban_notify_subs SET notifier_profile=%s "
                        "WHERE board=%s AND task_id=%s AND platform=%s AND chat_id=%s "
                        "AND thread_id=%s AND notifier_profile IS NULL",
                        (notifier_profile, *pk),
                    )
                if isinstance(event_kinds, list):
                    cur.execute(
                        "UPDATE kanban_notify_subs SET event_kinds=%s "
                        "WHERE board=%s AND task_id=%s AND platform=%s AND chat_id=%s "
                        "AND thread_id=%s",
                        (json.dumps(event_kinds), *pk),
                    )
                if include_children is not None:
                    cur.execute(
                        "UPDATE kanban_notify_subs SET include_children=%s "
                        "WHERE board=%s AND task_id=%s AND platform=%s AND chat_id=%s "
                        "AND thread_id=%s",
                        (1 if include_children else 0, *pk),
                    )

    def remove_notify_sub(self, *, task_id, platform, chat_id, thread_id=None) -> bool:
        thread_id = thread_id or ''
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "DELETE FROM kanban_notify_subs "
                "WHERE board=%s AND task_id=%s AND platform=%s AND chat_id=%s "
                "AND thread_id=%s",
                (self.board, task_id, platform, chat_id, thread_id),
            )
            return cur.rowcount > 0

    def list_notify_subs(self, task_id=None) -> list:
        clauses = ["board=%s"]
        params: list = [self.board]
        if task_id is not None:
            clauses.append("task_id=%s")
            params.append(task_id)
        sql = "SELECT * FROM kanban_notify_subs WHERE " + " AND ".join(clauses)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    # --- profile event subscriptions -------------------------------------

    def add_profile_event_sub(self, *, task_id, profile, name="",
                              event_kinds=_UNSET, include_children=None,
                              wake_agent=None, wake_prompt=_UNSET,
                              enabled=None) -> None:
        now = int(time.time())
        pk = (self.board, task_id, profile, name)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "INSERT INTO kanban_profile_event_subs "
                    "(board,task_id,profile,name,created_at,last_event_id) "
                    "VALUES (%s,%s,%s,%s,%s,0) "
                    "ON CONFLICT (board,task_id,profile,name) DO NOTHING",
                    (*pk, now),
                )
                if event_kinds is not _UNSET:
                    ek_val = json.dumps(event_kinds) if isinstance(event_kinds, list) else None
                    cur.execute(
                        "UPDATE kanban_profile_event_subs SET event_kinds=%s "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (ek_val, *pk),
                    )
                if include_children is not None:
                    cur.execute(
                        "UPDATE kanban_profile_event_subs SET include_children=%s "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (1 if include_children else 0, *pk),
                    )
                if wake_agent is not None:
                    cur.execute(
                        "UPDATE kanban_profile_event_subs SET wake_agent=%s "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (1 if wake_agent else 0, *pk),
                    )
                if wake_prompt is not _UNSET:
                    cur.execute(
                        "UPDATE kanban_profile_event_subs SET wake_prompt=%s "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (wake_prompt, *pk),
                    )
                if enabled is not None:
                    cur.execute(
                        "UPDATE kanban_profile_event_subs SET enabled=%s "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (1 if enabled else 0, *pk),
                    )

    def remove_profile_event_sub(self, *, task_id, profile, name="") -> bool:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "DELETE FROM kanban_profile_event_subs "
                "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                (self.board, task_id, profile, name),
            )
            return cur.rowcount > 0

    def list_profile_event_subs(self, *, task_id=None, profile=None,
                                enabled_only=True) -> list:
        clauses = ["board=%s"]
        params: list = [self.board]
        if task_id is not None:
            clauses.append("task_id=%s")
            params.append(task_id)
        if profile is not None:
            clauses.append("profile=%s")
            params.append(profile)
        if enabled_only:
            clauses.append("enabled=1")
        sql = ("SELECT * FROM kanban_profile_event_subs WHERE "
               + " AND ".join(clauses)
               + " ORDER BY created_at ASC")
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    # --- complete_task (basic) -------------------------------------------

    def complete_task(self, task_id: str, *, result=None, summary=None,
                      metadata=None, created_cards=None,
                      expected_run_id=None) -> bool:
        if created_cards:
            raise NotImplementedError(
                "phase-2-tail: complete_task created_cards gating")
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                current_run_id = row["current_run_id"] if row else None
                cur.execute(
                    "UPDATE tasks SET status='done', completed_at=%s, result=%s, "
                    "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                    "WHERE board=%s AND id=%s "
                    "AND status IN ('running','ready','blocked','scheduled')",
                    (now, result, self.board, task_id))
                if cur.rowcount != 1:
                    return False
                if current_run_id is not None:
                    run_summary = summary if summary is not None else result
                    cur.execute(
                        "UPDATE task_runs SET status='done', outcome='completed', "
                        "summary=%s, metadata=%s, ended_at=%s, "
                        "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                        "WHERE board=%s AND id=%s AND ended_at IS NULL",
                        (run_summary,
                         Jsonb(metadata) if metadata is not None else None,
                         now, self.board, current_run_id))
                    cur.execute(
                        "UPDATE tasks SET current_run_id=NULL "
                        "WHERE board=%s AND id=%s",
                        (self.board, task_id))
                self._emit(cur, task_id, "completed", {})
        self.recompute_ready()
        return True

    # --- runs -------------------------------------------------------------

    def _row_to_run(self, row: dict) -> Run:
        d = dict(row)
        d.pop("board", None)
        # metadata is JSONB — already a dict on read, no json.loads needed
        return Run(**{k: d.get(k) for k in Run.__dataclass_fields__})

    def list_runs(self, task_id: str, *, include_active=True,
                  state_type=None, state_name=None) -> list:
        if state_type is not None or state_name is not None:
            raise NotImplementedError(
                "phase-2-tail: list_runs state filters")
        sql = ("SELECT * FROM task_runs WHERE board=%s AND task_id=%s"
               + ("" if include_active else " AND ended_at IS NOT NULL")
               + " ORDER BY started_at ASC, id ASC")
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (self.board, task_id))
            return [self._row_to_run(r) for r in cur.fetchall()]

    def get_run(self, run_id: int) -> Optional[Run]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM task_runs WHERE board=%s AND id=%s",
                (self.board, run_id))
            row = cur.fetchone()
            return self._row_to_run(row) if row else None

    def latest_run(self, task_id: str) -> Optional[Run]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM task_runs WHERE board=%s AND task_id=%s "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (self.board, task_id))
            row = cur.fetchone()
            return self._row_to_run(row) if row else None

    def latest_summary(self, task_id: str) -> Optional[str]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT summary FROM task_runs "
                "WHERE board=%s AND task_id=%s AND summary IS NOT NULL AND summary<>'' "
                "ORDER BY COALESCE(ended_at,started_at) DESC, id DESC LIMIT 1",
                (self.board, task_id))
            row = cur.fetchone()
            return row["summary"] if row else None

    def latest_summaries(self, task_ids) -> dict:
        if not task_ids:
            return {}
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT task_id, summary FROM ("
                "  SELECT task_id, summary, "
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY task_id "
                "           ORDER BY COALESCE(ended_at,started_at) DESC, id DESC"
                "         ) rn "
                "  FROM task_runs "
                "  WHERE board=%s AND task_id = ANY(%s) "
                "    AND summary IS NOT NULL AND summary<>''"
                ") t WHERE rn=1",
                (self.board, list(task_ids)))
            return {r["task_id"]: r["summary"] for r in cur.fetchall()}

    # --- event claiming (notify subs) ------------------------------------

    def claim_unseen_events_for_sub(self, *, task_id, platform, chat_id,
                                    thread_id=None, kinds=None,
                                    include_children=False) -> tuple:
        thread_id = thread_id or ''
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT last_event_id, event_kinds, include_children "
                    "FROM kanban_notify_subs "
                    "WHERE board=%s AND task_id=%s AND platform=%s "
                    "AND chat_id=%s AND thread_id=%s FOR UPDATE",
                    (self.board, task_id, platform, chat_id, thread_id))
                sub_row = cur.fetchone()
                if sub_row is None:
                    return (0, 0, [])
                old_cursor = int(sub_row["last_event_id"])
                row_include_children = bool(sub_row["include_children"])
                # Determine task scope
                scope = [task_id]
                if include_children or row_include_children:
                    cur.execute(
                        "SELECT child_id FROM task_links "
                        "WHERE board=%s AND parent_id=%s",
                        (self.board, task_id))
                    for r in cur.fetchall():
                        scope.append(r["child_id"])
                # Determine effective event kinds
                ek_raw = sub_row["event_kinds"]
                if kinds is not None:
                    effective_kinds = list(kinds)
                elif ek_raw:
                    try:
                        effective_kinds = json.loads(ek_raw)
                    except Exception:
                        effective_kinds = None
                else:
                    effective_kinds = None
                # Query events
                if effective_kinds is not None:
                    cur.execute(
                        "SELECT * FROM task_events "
                        "WHERE board=%s AND task_id = ANY(%s) "
                        "AND id > %s AND kind = ANY(%s) ORDER BY id ASC",
                        (self.board, scope, old_cursor, effective_kinds))
                else:
                    cur.execute(
                        "SELECT * FROM task_events "
                        "WHERE board=%s AND task_id = ANY(%s) "
                        "AND id > %s ORDER BY id ASC",
                        (self.board, scope, old_cursor))
                event_rows = cur.fetchall()
                if not event_rows:
                    return (old_cursor, old_cursor, [])
                new_cursor = max(r["id"] for r in event_rows)
                # CAS advance
                cur.execute(
                    "UPDATE kanban_notify_subs SET last_event_id=%s "
                    "WHERE board=%s AND task_id=%s AND platform=%s "
                    "AND chat_id=%s AND thread_id=%s AND last_event_id=%s",
                    (new_cursor, self.board, task_id, platform,
                     chat_id, thread_id, old_cursor))
                events = [
                    Event(**{k: r[k] for k in Event.__dataclass_fields__})
                    for r in event_rows
                ]
                return (old_cursor, new_cursor, events)

    # --- event claiming (profile subs) -----------------------------------

    def claim_unseen_events_for_profile_sub(self, *, task_id, profile,
                                            name="") -> tuple:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT last_event_id, event_kinds, include_children "
                    "FROM kanban_profile_event_subs "
                    "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s "
                    "FOR UPDATE",
                    (self.board, task_id, profile, name))
                sub_row = cur.fetchone()
                if sub_row is None:
                    return (0, 0, [])
                old_cursor = int(sub_row["last_event_id"])
                # Determine task scope
                scope = [task_id]
                if sub_row["include_children"]:
                    cur.execute(
                        "SELECT child_id FROM task_links "
                        "WHERE board=%s AND parent_id=%s",
                        (self.board, task_id))
                    for r in cur.fetchall():
                        scope.append(r["child_id"])
                # Determine effective event kinds
                ek_raw = sub_row["event_kinds"]
                if ek_raw:
                    try:
                        effective_kinds = json.loads(ek_raw)
                    except Exception:
                        effective_kinds = list(_DEFAULT_NOTIFY_TERMINAL_KINDS)
                else:
                    effective_kinds = list(_DEFAULT_NOTIFY_TERMINAL_KINDS)
                # Scan events
                cur.execute(
                    "SELECT * FROM task_events "
                    "WHERE board=%s AND task_id = ANY(%s) "
                    "AND id > %s AND kind = ANY(%s) ORDER BY id ASC",
                    (self.board, scope, old_cursor, effective_kinds))
                event_rows = cur.fetchall()
                if not event_rows:
                    return (old_cursor, old_cursor, [])
                new_cursor = max(r["id"] for r in event_rows)
                claimed_events = []
                now = int(time.time())
                for r in event_rows:
                    cur.execute(
                        "INSERT INTO kanban_profile_event_claims "
                        "(board, event_id, profile, name, root_task_id, claimed_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (board, event_id, profile, name) DO NOTHING",
                        (self.board, r["id"], profile, name, task_id, now))
                    if cur.rowcount == 1:
                        claimed_events.append(
                            Event(**{k: r[k] for k in Event.__dataclass_fields__}))
                # CAS advance over ALL scanned events
                cur.execute(
                    "UPDATE kanban_profile_event_subs SET last_event_id=%s "
                    "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s "
                    "AND last_event_id=%s",
                    (new_cursor, self.board, task_id, profile, name, old_cursor))
                return (old_cursor, new_cursor, claimed_events)

    # --- board stats and assignees ---------------------------------------

    def board_stats(self) -> dict:
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "WHERE board=%s AND status != 'archived' GROUP BY status",
                (self.board,))
            by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(
                "SELECT assignee, status, COUNT(*) AS n FROM tasks "
                "WHERE board=%s AND status != 'archived' AND assignee IS NOT NULL "
                "GROUP BY assignee, status",
                (self.board,))
            by_assignee: dict = {}
            for r in cur.fetchall():
                by_assignee.setdefault(r["assignee"], {})[r["status"]] = int(r["n"])
            cur.execute(
                "SELECT MIN(created_at) AS ts FROM tasks "
                "WHERE board=%s AND status='ready'",
                (self.board,))
            oldest_row = cur.fetchone()
            oldest_ready_age = (
                (now - int(oldest_row["ts"]))
                if oldest_row and oldest_row["ts"] is not None else None
            )
        return {
            "by_status": by_status,
            "by_assignee": by_assignee,
            "oldest_ready_age_seconds": oldest_ready_age,
            "now": now,
        }

    def known_assignees(self) -> list:
        # Phase-2: on-disk profile detection deferred (phase-2-tail)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT assignee, status, COUNT(*) AS n FROM tasks "
                "WHERE board=%s AND status != 'archived' AND assignee IS NOT NULL "
                "GROUP BY assignee, status",
                (self.board,))
            counts: dict = {}
            for r in cur.fetchall():
                counts.setdefault(r["assignee"], {})[r["status"]] = int(r["n"])
        return [
            {"name": name, "on_disk": False, "counts": cnt}
            for name, cnt in sorted(counts.items())
        ]

    # --- deferred sidecar methods ----------------------------------------

    def list_profile_wake_events(self, **kwargs) -> list:
        raise NotImplementedError("phase-2-tail: list_profile_wake_events")

    def record_notifier_heartbeat(self, **kwargs) -> None:
        raise NotImplementedError("phase-2-tail: record_notifier_heartbeat")

    def list_notifier_heartbeats(self, **kwargs) -> list:
        raise NotImplementedError("phase-2-tail: list_notifier_heartbeats")

    def heartbeat_worker(self, **kwargs) -> bool:
        raise NotImplementedError("phase-2-tail: heartbeat_worker")
