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
