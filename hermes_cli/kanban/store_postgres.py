# hermes_cli/kanban/store_postgres.py
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Callable, Iterable, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hermes_cli import kanban_db
from hermes_cli.kanban_db import (  # reuse dataclasses
    Task, Run, Event, Comment, DEFAULT_HEARTBEAT_EVENT_MIN_INTERVAL_SECONDS,
    DEFAULT_PROFILE_WAKE_FAILURE_EVENT_MIN_INTERVAL_SECONDS,
    DEFAULT_FAILURE_LIMIT,
)
from hermes_cli.kanban_db import (  # reuse PURE helpers + exceptions across backends
    HallucinatedCardsError, PRHeadGateError, ExternalHandoffGateError,
    LINK_RELATION_DEPENDENCY,
    VALID_INITIAL_STATUSES,
    VALID_WORKSPACE_KINDS,
    KNOWN_TOOLSET_NAMES,
    _canonical_assignee,
    _claimer_id,
    _lane_type_for_assignee,
    _closeout_pr_evidence,
    _closeout_external_handoff_evidence,
    _external_handoff_required,
    _extract_reviewed_pr_head_sha,
    _extract_pr_head_sha,
    _TASK_ID_PROSE_RE,
    _EXTERNAL_HANDOFF_METADATA_KEYS,
    _pre_spawn_validation_errors,
    _RESPAWN_BLOCKER_RE,
    _RESPAWN_GUARD_PR_URL_RE,
    _RESPAWN_GUARD_SUCCESS_WINDOW,
    _RESPAWN_GUARD_PR_WINDOW,
    _STALE_HEARTBEAT_GAP_SECONDS,
)
from hermes_cli.kanban_db import DispatchResult
from hermes_cli.kanban import pg_pool

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

    def _find_missing_parents(self, cur, parents: Iterable[str]) -> list[str]:
        """Board-scoped parity with kanban_db._find_missing_parents: return the
        subset of ``parents`` that do not exist as tasks on this board, in input
        order (so the ValueError message matches the sqlite path)."""
        parents = list(parents)
        if not parents:
            return []
        cur.execute(
            "SELECT id FROM tasks WHERE board=%s AND id = ANY(%s)",
            (self.board, parents))
        present = {r["id"] for r in cur.fetchall()}
        return [p for p in parents if p not in present]

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
        # Input-validation parity with kanban_db.create_task (sqlite). Mirror the
        # SAME checks, in the same order, raising the SAME ValueError messages so
        # both backends behave identically. Reuse the upstream constants/helpers
        # (do not hardcode the sets) to stay in lockstep with kanban_db.
        assignee = _canonical_assignee(assignee)
        if not title or not title.strip():
            raise ValueError("title is required")
        if initial_status not in VALID_INITIAL_STATUSES:
            raise ValueError(
                f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}")
        if workspace_kind not in VALID_WORKSPACE_KINDS:
            raise ValueError(
                f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
                f"got {workspace_kind!r}")
        if branch_name is not None:
            branch_name = str(branch_name).strip() or None
        if branch_name and workspace_kind != "worktree":
            raise ValueError("branch_name is only valid for worktree workspaces")
        parents = tuple(p for p in parents if p)

        # Normalise + validate skills: strip whitespace, drop empties, dedupe
        # (preserving order). Refuse commas inside a single name, and collect
        # toolset-name confusions up front so the user sees the whole list at once.
        skills_list: Optional[list[str]] = None
        if skills is not None:
            cleaned: list[str] = []
            seen: set[str] = set()
            toolset_typos: list[str] = []
            for s in skills:
                if not s:
                    continue
                name = str(s).strip()
                if not name:
                    continue
                if "," in name:
                    raise ValueError(
                        f"skill name cannot contain comma: {name!r} "
                        f"(pass a list of separate names instead of a comma-joined string)"
                    )
                if name.casefold() in KNOWN_TOOLSET_NAMES:
                    toolset_typos.append(name)
                    continue
                if name in seen:
                    continue
                seen.add(name)
                cleaned.append(name)
            if toolset_typos:
                quoted = ", ".join(repr(n) for n in toolset_typos)
                noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
                raise ValueError(
                    f"{quoted} {noun}, not skill name(s). "
                    "Put toolsets in the assignee profile's `toolsets:` config "
                    "instead of per-task skills. Skills are named skill bundles "
                    "(e.g. `kanban-worker`, `blogwatcher`); toolsets are runtime "
                    "capabilities (e.g. `web`, `browser`, `terminal`)."
                )
            skills_list = cleaned

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
                # Parent existence — board-scoped, mirrors sqlite's
                # _find_missing_parents (input-order-preserving). Runs whenever
                # parents are supplied (ready/todo, blocked/scheduled, triage)
                # so link rows never dangle. Done inside the transaction, before
                # the INSERT, so the check + insert are atomic.
                if parents:
                    missing = self._find_missing_parents(cur, parents)
                    if missing:
                        raise ValueError(
                            f"unknown parent task(s): {', '.join(missing)}")
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
                    (self.board, tid, title.strip(), body, assignee, status, priority,
                     created_by, now, workspace_kind, workspace_path, branch_name,
                     tenant, idempotency_key, max_runtime_seconds,
                     json.dumps(skills_list) if skills_list is not None else None,
                     max_retries, session_id))
                for p in parents:
                    cur.execute(
                        "INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                        "VALUES (%s,%s,%s,'dependency') ON CONFLICT DO NOTHING",
                        (self.board, p, tid))
                self._emit(cur, tid, "created", {
                    "assignee": assignee, "status": status,
                    "parents": list(parents), "tenant": tenant,
                    "branch_name": branch_name, "skills": skills_list or None})
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

    # --- triage composite writes -----------------------------------------

    def specify_triage_task(self, task_id: str, *, title: Optional[str] = None,
                            body: Optional[str] = None, assignee: Optional[str] = None,
                            author: Optional[str] = None) -> bool:
        """Flesh out a triage task and promote it to ``todo`` (PG mirror of
        kanban_db.specify_triage_task). Single transaction; emits one
        ``specified`` event; optional inline audit comment; recompute_ready()
        outside the txn. Returns False when missing / not in triage."""
        if title is not None and not title.strip():
            raise ValueError("title cannot be blank")
        assignee = _canonical_assignee(assignee)
        promoted = False
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT title, body, assignee FROM tasks "
                    "WHERE board=%s AND id=%s AND status='triage' FOR UPDATE",
                    (self.board, task_id))
                existing = cur.fetchone()
                if existing is None:
                    return False
                sets = ["status='todo'"]
                params: list[Any] = []
                changed_fields: list[str] = []
                if title is not None and title.strip() != (existing["title"] or ""):
                    sets.append("title=%s")
                    params.append(title.strip())
                    changed_fields.append("title")
                if body is not None and (body or "") != (existing["body"] or ""):
                    sets.append("body=%s")
                    params.append(body)
                    changed_fields.append("body")
                if assignee is not None and assignee != (existing["assignee"] or None):
                    sets.append("assignee=%s")
                    params.append(assignee)
                    changed_fields.append("assignee")
                params.extend([self.board, task_id])
                cur.execute(
                    f"UPDATE tasks SET {', '.join(sets)} "
                    f"WHERE board=%s AND id=%s AND status='triage'",
                    tuple(params))
                if cur.rowcount != 1:
                    return False
                if changed_fields and author and author.strip():
                    cur.execute(
                        "INSERT INTO task_comments "
                        "(board, task_id, author, body, created_at) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (self.board, task_id, author.strip(),
                         "Specified — updated " + ", ".join(changed_fields)
                         + " and promoted to todo.",
                         int(time.time())))
                self._emit(cur, task_id, "specified",
                           {"changed_fields": changed_fields} if changed_fields else None)
                promoted = True
        if promoted:
            self.recompute_ready()
        return True

    def decompose_triage_task(self, task_id: str, *, root_assignee, children,
                              author=None, auto_promote=True):
        """Fan a triage task into a child graph and promote the root to ``todo``
        (PG mirror of kanban_db.decompose_triage_task). One transaction; emits
        created/linked/decomposed events; recompute_ready() after iff auto_promote.
        Returns child ids (input order) or None (missing/not-triage/empty/cycle)."""
        if not children:
            return None
        if root_assignee is not None:
            root_assignee = _canonical_assignee(root_assignee)
        # --- pre-validate child shape (verbatim from kanban_db 5738-5753) ---
        for idx, child in enumerate(children):
            if not isinstance(child, dict):
                raise ValueError(f"child[{idx}] is not a dict")
            title = child.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"child[{idx}].title is required")
            parents_idx = child.get("parents") or []
            if not isinstance(parents_idx, list):
                raise ValueError(f"child[{idx}].parents must be a list")
            for p in parents_idx:
                if not isinstance(p, int) or p < 0 or p >= len(children):
                    raise ValueError(
                        f"child[{idx}].parents[{p}] is not a valid index into children")
                if p == idx:
                    raise ValueError(f"child[{idx}] cannot list itself as a parent")
        # --- cycle detection (Kahn, verbatim from kanban_db 5760-5776) ---
        _in_deg = [0] * len(children)
        _adj = [[] for _ in range(len(children))]
        for _i, _c in enumerate(children):
            for _p in (_c.get("parents") or []):
                _adj[_p].append(_i)
                _in_deg[_i] += 1
        _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
        _seen = 0
        while _queue:
            _node = _queue.pop()
            _seen += 1
            for _nb in _adj[_node]:
                _in_deg[_nb] -= 1
                if _in_deg[_nb] == 0:
                    _queue.append(_nb)
        if _seen != len(children):
            raise ValueError("cyclic dependency detected in decomposed children list")

        now = int(time.time())
        child_ids: list[str] = []
        committed = False
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT id, status, tenant FROM tasks "
                    "WHERE board=%s AND id=%s FOR UPDATE",
                    (self.board, task_id))
                root_row = cur.fetchone()
                if root_row is None or root_row["status"] != "triage":
                    return None
                tenant = root_row["tenant"]
                for child in children:
                    new_id = _new_task_id()
                    body = child.get("body")
                    cur.execute(
                        "INSERT INTO tasks (board, id, title, body, assignee, status, "
                        "workspace_kind, tenant, created_at, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,'todo','scratch',%s,%s,%s)",
                        (self.board, new_id, child["title"].strip(),
                         body if isinstance(body, str) else None,
                         _canonical_assignee(child.get("assignee")),
                         tenant, now, (author or "decomposer")))
                    self._emit(cur, new_id, "created",
                               {"by": author or "decomposer",
                                "from_decompose_of": task_id})
                    child_ids.append(new_id)
                for idx, child in enumerate(children):
                    for p_idx in child.get("parents") or []:
                        parent_id = child_ids[p_idx]
                        child_id = child_ids[idx]
                        cur.execute(
                            "INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                            "VALUES (%s,%s,%s,'dependency') ON CONFLICT DO NOTHING",
                            (self.board, parent_id, child_id))
                        self._emit(cur, child_id, "linked",
                                   {"parent": parent_id, "child": child_id})
                for cid in child_ids:
                    cur.execute(
                        "INSERT INTO task_links (board, parent_id, child_id, relation_type) "
                        "VALUES (%s,%s,%s,'dependency') ON CONFLICT DO NOTHING",
                        (self.board, cid, task_id))
                sets = ["status='todo'"]
                params: list[Any] = []
                if root_assignee is not None:
                    sets.append("assignee=%s")
                    params.append(root_assignee)
                params.extend([self.board, task_id])
                cur.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE board=%s AND id=%s",
                    tuple(params))
                if author and author.strip():
                    cur.execute(
                        "INSERT INTO task_comments (board, task_id, author, body, created_at) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (self.board, task_id, author.strip(),
                         "Decomposed into " + ", ".join(child_ids)
                         + ". Root will wake when all children complete.", now))
                self._emit(cur, task_id, "decomposed",
                           {"child_ids": child_ids, "root_assignee": root_assignee})
                committed = True
        if committed and auto_promote:
            self.recompute_ready()
        return child_ids

    # --- status transitions ----------------------------------------------

    def block_task(self, task_id: str, *, reason=None, expected_run_id=None) -> bool:
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s "
                    "FOR UPDATE",
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
        # Phase-2: expected_run_id optimistic-concurrency guard not yet honored (phase-2-tail).
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s "
                    "FOR UPDATE",
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
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s "
                    "FOR UPDATE",
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
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s "
                    "FOR UPDATE",
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
        # Phase-2: SQLite normalizes event_kinds (dedup/str-coerce/strip/drop-empty) via _normalize_event_kinds; PG stores the list as-is (phase-2-tail).
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
                # Phase-2: SQLite normalizes event_kinds (dedup/str-coerce/strip/drop-empty) via _normalize_event_kinds; PG stores the list as-is (phase-2-tail).
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

    # --- complete_task (full parity) -------------------------------------
    #
    # Mirrors kanban_db.complete_task (the sqlite reference) faithfully.
    # Sanctioned PG-only divergences: every WHERE/INSERT is board-scoped
    # (board=%s); run metadata is JSONB (Jsonb() on write, already-a-dict on
    # read, no json.loads); the pure metadata/regex helpers are imported and
    # reused from kanban_db rather than reimplemented. OS-level workspace/tmux
    # cleanup never happens in the store — it is delegated to the caller via
    # the optional on_cleanup hook (Part B wires the rmtree+tmux-kill fn).

    def _pg_terminal_run_already_closed(
        self, cur, task_id: str, expected_run_id: Optional[int],
        *, outcome: str, task_status: str,
    ) -> bool:
        """Duplicate-closeout idempotency check (board-scoped)."""
        if expected_run_id is None:
            return False
        cur.execute(
            "SELECT t.status AS task_status, r.outcome, r.ended_at "
            "FROM task_runs r JOIN tasks t "
            "ON t.board=r.board AND t.id=r.task_id "
            "WHERE r.board=%s AND r.id=%s AND r.task_id=%s",
            (self.board, int(expected_run_id), task_id))
        row = cur.fetchone()
        return bool(
            row
            and row["task_status"] == task_status
            and row["outcome"] == outcome
            and row["ended_at"] is not None
        )

    def _pg_expected_parent_pr_head_sha(
        self, task_id: str,
    ) -> Optional[tuple[str, str, Optional[int]]]:
        """Return ``(sha, parent_task_id, parent_run_id)`` from done parents."""
        parents = self.parent_ids(task_id, relation_type=LINK_RELATION_DEPENDENCY)
        if not parents:
            return None

        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, task_id, metadata FROM task_runs "
                "WHERE board=%s AND task_id = ANY(%s) AND outcome='completed' "
                "ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC",
                (self.board, parents))
            rows = cur.fetchall()
            for row in rows:
                # task_runs.metadata is JSONB — already a dict, no json.loads.
                sha = _extract_pr_head_sha(row["metadata"])
                if sha:
                    return sha, row["task_id"], int(row["id"])

        # Legacy/manual fallback: only honor explicit SHA-looking text.
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            for parent_id in parents:
                cur.execute(
                    "SELECT result FROM tasks "
                    "WHERE board=%s AND id=%s AND status='done'",
                    (self.board, parent_id))
                task_row = cur.fetchone()
                if task_row:
                    sha = _extract_pr_head_sha(task_row["result"])
                    if sha:
                        return sha, parent_id, None
        return None

    def _pg_enforce_review_pr_head_gate(
        self, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    ) -> None:
        task = self.get_task(task_id)
        if not task or _lane_type_for_assignee(task.assignee) != "review":
            return
        expected = self._pg_expected_parent_pr_head_sha(task_id)
        if expected is None:
            return
        expected_sha, parent_task_id, parent_run_id = expected
        reviewed_sha = _extract_reviewed_pr_head_sha(metadata)
        if reviewed_sha == expected_sha:
            return
        payload = {
            "expected_pr_head_sha": expected_sha,
            "reviewed_pr_head_sha": reviewed_sha,
            "parent_task_id": parent_task_id,
            "parent_run_id": parent_run_id,
            "summary_preview": (
                (summary or "").strip().splitlines()[0][:200] if summary else None
            ),
        }
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                self._emit(cur, task_id,
                           "completion_blocked_pr_head_gate", payload)
        raise PRHeadGateError(
            task_id=task_id,
            expected_sha=expected_sha,
            reviewed_sha=reviewed_sha,
            parent_task_id=parent_task_id,
            parent_run_id=parent_run_id,
        )

    def _pg_enforce_external_handoff_gate(
        self, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    ) -> None:
        if not _external_handoff_required(metadata):
            return
        if _closeout_external_handoff_evidence(metadata):
            return
        payload = {
            "required": True,
            "accepted_keys": list(_EXTERNAL_HANDOFF_METADATA_KEYS),
            "summary_preview": (
                (summary or "").strip().splitlines()[0][:200] if summary else None
            ),
        }
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                self._emit(cur, task_id,
                           "completion_blocked_external_handoff_gate", payload)
        raise ExternalHandoffGateError(task_id=task_id)

    def _pg_verify_created_cards(
        self, completing_task_id: str, claimed_ids: Iterable[str],
    ) -> tuple[list[str], list[str]]:
        """Partition claimed_ids into (verified, phantom) — board-scoped."""
        claimed = [str(x).strip() for x in (claimed_ids or []) if str(x).strip()]
        if not claimed:
            return [], []
        seen: set[str] = set()
        ordered: list[str] = []
        for cid in claimed:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT assignee FROM tasks WHERE board=%s AND id=%s",
                (self.board, completing_task_id))
            row = cur.fetchone()
            if row is None:
                return [], ordered
            completing_assignee = row["assignee"]
            cur.execute(
                "SELECT id, created_by FROM tasks "
                "WHERE board=%s AND id = ANY(%s)",
                (self.board, ordered))
            found = {r["id"]: r["created_by"] for r in cur.fetchall()}
        linked_children: set[str] = set(self.child_ids(completing_task_id))
        verified: list[str] = []
        phantom: list[str] = []
        for cid in ordered:
            if cid not in found:
                phantom.append(cid)
                continue
            created_by = found.get(cid)
            if completing_assignee and created_by == completing_assignee:
                verified.append(cid)
            elif created_by == completing_task_id:
                verified.append(cid)
            elif cid in linked_children:
                verified.append(cid)
            else:
                phantom.append(cid)
        return verified, phantom

    def _pg_scan_prose_for_phantom_ids(self, text: str) -> list[str]:
        """Regex-scan free-form text for t_<hex> refs that don't exist."""
        if not text:
            return []
        matches = _TASK_ID_PROSE_RE.findall(text)
        if not matches:
            return []
        seen: set[str] = set()
        unique: list[str] = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM tasks WHERE board=%s AND id = ANY(%s)",
                (self.board, unique))
            existing = {r["id"] for r in cur.fetchall()}
        return [m for m in unique if m not in existing]

    def _pg_closeout_packet(
        self, cur, task_id: str, *, run_id: Optional[int],
        outcome: str, summary: Optional[str], metadata: Optional[dict],
    ) -> dict:
        """Build the deterministic closeout packet (board-scoped read)."""
        cur.execute(
            "SELECT assignee, status, branch_name FROM tasks "
            "WHERE board=%s AND id=%s",
            (self.board, task_id))
        row = cur.fetchone()
        assignee = row["assignee"] if row else None
        status = row["status"] if row else None
        task_branch = row["branch_name"] if row else None
        md_keys: list[str] = []
        if isinstance(metadata, dict):
            md_keys = sorted(
                str(k) for k in metadata.keys() if k != "closeout_packet")
        preview = (summary or "").strip().splitlines()[0][:400] if summary else None
        packet = {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": int(run_id) if run_id is not None else None,
            "outcome": outcome,
            "terminal_status": status,
            "assignee": assignee,
            "lane_type": _lane_type_for_assignee(assignee),
            "summary_preview": preview,
            "metadata_keys": md_keys,
        }
        packet.update(_closeout_pr_evidence(metadata, task_branch=task_branch))
        external = _closeout_external_handoff_evidence(metadata)
        if external:
            packet["external_handoff"] = external
        return packet

    def _pg_metadata_with_closeout_packet(
        self, cur, task_id: str, metadata: Optional[dict], *,
        run_id: Optional[int], outcome: str, summary: Optional[str],
    ) -> dict:
        enriched = dict(metadata or {})
        enriched["closeout_packet"] = self._pg_closeout_packet(
            cur, task_id, run_id=run_id, outcome=outcome,
            summary=summary, metadata=enriched)
        return enriched

    def _pg_synthesize_ended_run(
        self, cur, task_id: str, *, outcome: str,
        summary: Optional[str] = None, error: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Insert a zero-duration, already-closed run (board-scoped).

        Used when complete_task fires on a never-claimed task so the handoff
        fields are not silently lost. Does NOT touch the tasks row.
        """
        now = int(time.time())
        cur.execute(
            "SELECT assignee, current_step_key FROM tasks "
            "WHERE board=%s AND id=%s",
            (self.board, task_id))
        trow = cur.fetchone()
        profile = trow["assignee"] if trow else None
        step_key = trow["current_step_key"] if trow else None
        cur.execute(
            "INSERT INTO task_runs (board, task_id, profile, step_key, "
            "status, outcome, summary, error, metadata, started_at, ended_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (self.board, task_id, profile, step_key,
             outcome, outcome, summary, error,
             Jsonb(metadata) if metadata else None, now, now))
        return int(cur.fetchone()["id"])

    def _pg_record_pre_spawn_validation_failure(self, task_id: str,
                                                errors: list[str]) -> bool:
        """Mirror kanban_db._record_pre_spawn_validation_failure: flip a ready
        task to blocked, synth an ended run, emit the failure/gave_up/blocked
        events. Returns True if it blocked the task."""
        reason = "pre-spawn validation failed: " + "; ".join(errors)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT consecutive_failures, status FROM tasks "
                    "WHERE board=%s AND id=%s", (self.board, task_id))
                row = cur.fetchone()
                if row is None or row["status"] != "ready":
                    return False
                failures = int(row["consecutive_failures"] or 0) + 1
                cur.execute(
                    "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, "
                    "consecutive_failures=%s, last_failure_error=%s "
                    "WHERE board=%s AND id=%s AND status='ready' "
                    "AND claim_lock IS NULL",
                    (failures, reason[:500], self.board, task_id))
                if cur.rowcount != 1:
                    return False
                metadata = {
                    "failure_class": "pre_spawn_validation",
                    "validation_errors": list(errors),
                    "failures": failures,
                    "effective_limit": 1,
                    "limit_source": "pre_spawn_validation",
                }
                run_id = self._pg_synthesize_ended_run(
                    cur, task_id, outcome="spawn_failed", summary=reason,
                    error=reason[:500], metadata=metadata)
                payload = dict(metadata)
                payload["error"] = reason[:500]
                self._emit(cur, task_id, "pre_spawn_validation_failed",
                           payload, run_id=run_id)
                self._emit(cur, task_id, "gave_up", payload, run_id=run_id)
                self._emit(cur, task_id, "blocked", {"reason": reason},
                           run_id=run_id)
                return True

    def _pg_clear_failure_counter(self, task_id: str) -> None:
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE tasks SET consecutive_failures=0, "
                    "last_failure_error=NULL WHERE board=%s AND id=%s",
                    (self.board, task_id))

    def _pg_end_run(self, cur, task_id, *, outcome, summary=None, error=None,
                    metadata=None, status=None):
        """Mirror kanban_db._end_run: close the active run + clear the pointer.

        Runs inside the caller's open transaction (takes the open ``cur``).
        Returns the closed run_id, or None if no active run existed. If the
        active run was already ended by a racing reclaim/crash path
        (rowcount==0), emit a ``double_close_attempt`` event but STILL clear
        ``tasks.current_run_id`` and return the run_id (backward-compatible
        with the reference, avoiding a dangling pointer + spurious synth run).
        """
        cur.execute(
            "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
            (self.board, task_id))
        row = cur.fetchone()
        if not row or not row["current_run_id"]:
            return None
        run_id = int(row["current_run_id"])
        cur.execute(
            "UPDATE task_runs SET status=%s, outcome=%s, summary=%s, error=%s, "
            "metadata=%s, ended_at=%s, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE board=%s AND id=%s AND ended_at IS NULL",
            (status or outcome, outcome, summary, error,
             Jsonb(metadata) if metadata else None, int(time.time()),
             self.board, run_id))
        if cur.rowcount == 0:
            self._emit(cur, task_id, "double_close_attempt",
                       {"run_id": run_id, "caller_outcome": outcome},
                       run_id=run_id)
        cur.execute(
            "UPDATE tasks SET current_run_id=NULL WHERE board=%s AND id=%s",
            (self.board, task_id))
        return run_id

    def complete_task(self, task_id: str, *, result=None, summary=None,
                      metadata=None, created_cards=None,
                      expected_run_id=None,
                      on_cleanup: Optional[Callable[[str], None]] = None) -> bool:
        now = int(time.time())
        handoff_summary = summary if summary is not None else result

        # Step 1: idempotent duplicate closeout for the same run_id.
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            if self._pg_terminal_run_already_closed(
                    cur, task_id, expected_run_id,
                    outcome="completed", task_status="done"):
                return True

        # Step 2: reviewer PR-head gate (DB/metadata-only). Raises on mismatch.
        self._pg_enforce_review_pr_head_gate(
            task_id, metadata, summary=handoff_summary)

        # Step 3: opt-in external-handoff gate. Raises if required w/o evidence.
        self._pg_enforce_external_handoff_gate(
            task_id, metadata, summary=handoff_summary)

        # Step 4: created_cards gate BEFORE the main txn. A rejection is
        # auditable (its own tiny txn) and never mutates task state.
        if created_cards:
            verified_cards, phantom_cards = self._pg_verify_created_cards(
                task_id, created_cards)
            if phantom_cards:
                with self._pool.connection() as conn, \
                        conn.cursor(row_factory=dict_row) as cur:
                    with conn.transaction():
                        self._emit(
                            cur, task_id, "completion_blocked_hallucination",
                            {
                                "phantom_cards": phantom_cards,
                                "verified_cards": verified_cards,
                                "summary_preview": (
                                    (summary or result or "").strip()
                                    .splitlines()[0][:200]
                                    if (summary or result) else None
                                ),
                            })
                raise HallucinatedCardsError(phantom_cards, task_id)
        else:
            verified_cards = []

        # Step 5: main write txn.
        run_id: Optional[int] = None
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s "
                    "FOR UPDATE",
                    (self.board, task_id))
                row = cur.fetchone()
                current_run_id = row["current_run_id"] if row else None
                if expected_run_id is None:
                    cur.execute(
                        "UPDATE tasks SET status='done', result=%s, "
                        "completed_at=%s, claim_lock=NULL, claim_expires=NULL, "
                        "worker_pid=NULL WHERE board=%s AND id=%s "
                        "AND status IN ('running','ready','blocked','scheduled')",
                        (result, now, self.board, task_id))
                else:
                    cur.execute(
                        "UPDATE tasks SET status='done', result=%s, "
                        "completed_at=%s, claim_lock=NULL, claim_expires=NULL, "
                        "worker_pid=NULL WHERE board=%s AND id=%s "
                        "AND status IN ('running','ready','blocked','scheduled') "
                        "AND current_run_id=%s",
                        (result, now, self.board, task_id, int(expected_run_id)))
                if cur.rowcount != 1:
                    return False
                active_run_id = current_run_id
                # Build the closeout packet with the active run id BEFORE
                # ending the run (same order as the reference _end_run flow).
                closeout_metadata = self._pg_metadata_with_closeout_packet(
                    cur, task_id, metadata,
                    run_id=active_run_id, outcome="completed",
                    summary=handoff_summary)
                # End the current run via the shared _end_run mirror. It
                # re-reads current_run_id itself and handles the defensive
                # double-close branch (already-ended run): emits the event,
                # clears the pointer unconditionally, and returns the run_id.
                run_id = self._pg_end_run(
                    cur, task_id, outcome="completed", status="done",
                    summary=handoff_summary, metadata=closeout_metadata)
                # No active run + handoff data: synthesize a zero-duration run
                # so the handoff isn't lost, re-resolving the packet w/ run_id.
                if run_id is None and (summary or metadata or result):
                    closeout_metadata = self._pg_metadata_with_closeout_packet(
                        cur, task_id, metadata,
                        run_id=None, outcome="completed",
                        summary=handoff_summary)
                    run_id = self._pg_synthesize_ended_run(
                        cur, task_id, outcome="completed",
                        summary=handoff_summary, metadata=closeout_metadata)
                # If we now have a run_id but the packet still points at None,
                # re-resolve the packet w/ the real run_id and persist it.
                if run_id is not None:
                    packet = closeout_metadata.get("closeout_packet")
                    if isinstance(packet, dict) and packet.get("run_id") is None:
                        closeout_metadata = \
                            self._pg_metadata_with_closeout_packet(
                                cur, task_id, metadata,
                                run_id=run_id, outcome="completed",
                                summary=handoff_summary)
                        cur.execute(
                            "UPDATE task_runs SET metadata=%s "
                            "WHERE board=%s AND id=%s",
                            (Jsonb(closeout_metadata), self.board, run_id))
                # Build the completed event payload.
                ev_summary = handoff_summary or ""
                ev_summary = (ev_summary.strip().splitlines()[0][:400]
                              if ev_summary else "")
                completed_payload: dict = {
                    "result_len": len(result) if result else 0,
                    "summary": ev_summary or None,
                }
                if isinstance(closeout_metadata.get("closeout_packet"), dict):
                    completed_payload["closeout_packet"] = \
                        closeout_metadata["closeout_packet"]
                if verified_cards:
                    completed_payload["verified_cards"] = verified_cards
                md_artifacts = closeout_metadata.get("artifacts")
                if isinstance(md_artifacts, (list, tuple)):
                    cleaned_artifacts = [
                        str(p).strip() for p in md_artifacts
                        if isinstance(p, str) and str(p).strip()
                    ]
                    if cleaned_artifacts:
                        completed_payload["artifacts"] = cleaned_artifacts
                self._emit(cur, task_id, "completed", completed_payload,
                           run_id=run_id)

        # Step 6: prose-scan (own txn, after main commit). Advisory, never blocks.
        scan_text = " ".join(filter(None, [summary, result]))
        if scan_text:
            phantom_refs = self._pg_scan_prose_for_phantom_ids(scan_text)
            phantom_refs = [p for p in phantom_refs
                            if p not in set(verified_cards)]
            if phantom_refs:
                with self._pool.connection() as conn, \
                        conn.cursor(row_factory=dict_row) as cur:
                    with conn.transaction():
                        self._emit(
                            cur, task_id,
                            "suspected_hallucinated_references",
                            {"phantom_refs": phantom_refs,
                             "source": "completion_summary"},
                            run_id=run_id)

        # Step 7: wipe the consecutive-failures counter.
        self._pg_clear_failure_counter(task_id)
        # Step 8: recompute ready status for dependents.
        self.recompute_ready()
        # Step 9: OS-level cleanup is delegated. The store never touches the
        # filesystem/tmux; fire the hook now that completion is durable.
        if on_cleanup is not None:
            on_cleanup(task_id)
        return True

    # --- claim_task (atomic ready->running) ------------------------------

    def claim_task(self, task_id, *, ttl_seconds=None, claimer=None):
        now = int(time.time())
        ttl = int(ttl_seconds) if ttl_seconds else 900
        claimer = claimer or _claimer_id()   # sqlite parity: default lock = host:pid
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT id FROM tasks WHERE board=%s AND id=%s AND status='ready' "
                    "AND claim_lock IS NULL FOR UPDATE SKIP LOCKED",
                    (self.board, task_id))
                if cur.fetchone() is None:
                    return None
                # Phase-2: SQLite's last-chance parent-dependency re-check (demote to
                # 'todo' + return None if a parent is still undone) is deferred
                # (phase-2-tail). Not reached in Phase 2 — dispatch is Phase 3 glue.
                cur.execute(
                    "INSERT INTO task_runs (board,task_id,profile,step_key,status,claim_lock,"
                    "claim_expires,max_runtime_seconds,started_at) "
                    "SELECT %s,%s,assignee,current_step_key,'running',%s,%s,max_runtime_seconds,%s "
                    "FROM tasks WHERE board=%s AND id=%s RETURNING id",
                    (self.board, task_id, claimer, now + ttl, now, self.board, task_id))
                run_id = cur.fetchone()["id"]
                cur.execute(
                    "UPDATE tasks SET status='running', claim_lock=%s, claim_expires=%s, "
                    "started_at=COALESCE(started_at,%s), current_run_id=%s "
                    "WHERE board=%s AND id=%s",
                    (claimer, now + ttl, now, run_id, self.board, task_id))
                self._emit(cur, task_id, "claimed",
                           {"lock": claimer, "expires": now + ttl, "run_id": run_id},
                           run_id=run_id)
        return self.get_task(task_id)

    # --- record_task_failure (circuit breaker / gave_up) ------------------

    def record_task_failure(self, task_id, error, *, outcome, failure_limit=None,
                            failure_limit_is_cap=False, release_claim=True,
                            end_run=True, event_payload_extra=None) -> bool:
        """Mirror kanban_db._record_task_failure board-scoped.

        Returns True when the breaker tripped (task auto-blocked + gave_up),
        False when the task was just updated in place (retry -> ready, or a
        counter-only bookkeep on the timeout/crash path).
        """
        blocked = False
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "SELECT consecutive_failures, status, max_retries "
                    "FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                if row is None:
                    return False
                failures = int(row["consecutive_failures"]) + 1

                # Per-task override remains authoritative for ordinary failures.
                # Deterministic/systemic failure paths can opt into cap semantics
                # with ``failure_limit_is_cap=True``.
                task_override = row["max_retries"]
                if (
                    task_override is not None
                    and failure_limit is not None
                    and failure_limit_is_cap
                ):
                    task_limit = int(task_override)
                    caller_limit = int(failure_limit)
                    effective_limit = min(task_limit, caller_limit)
                    limit_source = ("task" if effective_limit == task_limit
                                    else "dispatcher")
                elif task_override is not None:
                    effective_limit = int(task_override)
                    limit_source = "task"
                else:
                    effective_limit = int(
                        failure_limit if failure_limit is not None
                        else DEFAULT_FAILURE_LIMIT)
                    limit_source = "dispatcher"

                if failures >= effective_limit:
                    # Trip the breaker.
                    if release_claim:
                        # Spawn path: still running, also clear claim state.
                        cur.execute(
                            "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                            "claim_expires=NULL, worker_pid=NULL, "
                            "consecutive_failures=%s, last_failure_error=%s "
                            "WHERE board=%s AND id=%s "
                            "AND status IN ('running','ready')",
                            (failures, error[:500], self.board, task_id))
                    else:
                        # Timeout/crash path: task already at ``ready`` with
                        # claim cleared; just flip to blocked + update counter.
                        cur.execute(
                            "UPDATE tasks SET status='blocked', "
                            "consecutive_failures=%s, last_failure_error=%s "
                            "WHERE board=%s AND id=%s "
                            "AND status IN ('ready','running')",
                            (failures, error[:500], self.board, task_id))
                    run_id = None
                    if end_run:
                        # Only the spawn path has an open run to close.
                        run_id = self._pg_end_run(
                            cur, task_id,
                            outcome="gave_up", status="gave_up",
                            error=error[:500],
                            metadata={
                                "failures": failures,
                                "trigger_outcome": outcome,
                                "effective_limit": effective_limit,
                                "limit_source": limit_source,
                            })
                    payload = {
                        "failures": failures,
                        "effective_limit": effective_limit,
                        "limit_source": limit_source,
                        "error": error[:500],
                        "trigger_outcome": outcome,
                    }
                    if event_payload_extra:
                        payload.update(event_payload_extra)
                    self._emit(cur, task_id, "gave_up", payload, run_id=run_id)
                    blocked = True
                else:
                    # Below threshold.
                    if release_claim:
                        # Spawn path: transition running -> ready + clear claim.
                        cur.execute(
                            "UPDATE tasks SET status='ready', claim_lock=NULL, "
                            "claim_expires=NULL, worker_pid=NULL, "
                            "consecutive_failures=%s, last_failure_error=%s "
                            "WHERE board=%s AND id=%s AND status='running'",
                            (failures, error[:500], self.board, task_id))
                    else:
                        # Timeout/crash path: task already at ``ready`` via its
                        # own UPDATE. Just bookkeep the counter + last error.
                        cur.execute(
                            "UPDATE tasks SET consecutive_failures=%s, "
                            "last_failure_error=%s WHERE board=%s AND id=%s",
                            (failures, error[:500], self.board, task_id))
                    if end_run:
                        # Spawn path: close the open run with outcome.
                        run_id = self._pg_end_run(
                            cur, task_id,
                            outcome=outcome, status=outcome,
                            error=error[:500],
                            metadata={"failures": failures})
                        self._emit(
                            cur, task_id, outcome,
                            {"error": error[:500], "failures": failures},
                            run_id=run_id)
                    # Timeout/crash path's caller already emitted its own event.
        return blocked

    # --- spawn result recording (A5) -------------------------------------

    def record_spawn_success(self, task_id: str, pid: int) -> None:
        """Stamp ``worker_pid`` + emit a ``spawned`` event.

        Mirrors ``kanban_db._set_worker_pid``: updates ``tasks.worker_pid`` and
        the current run's ``task_runs.worker_pid``, then emits a ``spawned``
        event carrying the pid. Called by the glue (Part B) after a successful
        spawn of a task that ``dispatch_plan`` already claimed.
        """
        pid = int(pid)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE tasks SET worker_pid=%s WHERE board=%s AND id=%s",
                    (pid, self.board, task_id))
                cur.execute(
                    "SELECT current_run_id FROM tasks WHERE board=%s AND id=%s",
                    (self.board, task_id))
                row = cur.fetchone()
                run_id = row["current_run_id"] if row else None
                if run_id is not None:
                    cur.execute(
                        "UPDATE task_runs SET worker_pid=%s WHERE board=%s AND id=%s",
                        (pid, self.board, int(run_id)))
                self._emit(cur, task_id, "spawned", {"pid": pid}, run_id=run_id)

    def record_spawn_failure(self, task_id, error, *, failure_limit=None) -> bool:
        # Thin wrapper over the A4 record_task_failure spawn_failed path. The
        # systemic-failure-signature grouping from dispatch_once lives in the
        # Part-B glue, NOT here.
        return self.record_task_failure(task_id, error, outcome="spawn_failed",
                                        failure_limit=failure_limit,
                                        release_claim=True, end_run=True)

    def block_systemic_spawn_failure_signature(self, task_ids, *,
                                               failure_signature, error,
                                               signature_count):
        """Mirror kanban_db._block_systemic_spawn_failure_signature: block ready
        siblings sharing a spawn-failure signature WITHOUT re-incrementing their
        counters. Returns the ids actually blocked.

        Unlike the SQLite reference (single write_txn over all ids), this
        blocks each id in its own transaction — intentional, since the
        siblings are independent and the PG store uses per-op connections.
        """
        reason = ("systemic spawn failure: multiple tasks failed with the same "
                  "spawn error signature; platform/profile fix required before retry")
        blocked = []
        seen = list(dict.fromkeys(task_ids))
        for task_id in seen:
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "SELECT status, consecutive_failures FROM tasks "
                        "WHERE board=%s AND id=%s", (self.board, task_id))
                    row = cur.fetchone()
                    if row is None or row["status"] != "ready":
                        continue
                    cur.execute(
                        "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, last_failure_error=%s "
                        "WHERE board=%s AND id=%s AND status='ready' "
                        "AND claim_lock IS NULL",
                        (error[:500], self.board, task_id))
                    if cur.rowcount != 1:
                        continue
                    payload = {
                        "failure_class": kanban_db.FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE,
                        "failure_signature": failure_signature,
                        "signature_count": int(signature_count),
                        "signature_threshold":
                            kanban_db.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD,
                        "failures": int(row["consecutive_failures"] or 0),
                        "effective_limit": 1,
                        "limit_source": "systemic_failure_signature",
                        "trigger_outcome": "spawn_failed",
                        "error": error[:500],
                        "guidance": kanban_db._SYSTEMIC_SPAWN_FAILURE_GUIDANCE,
                    }
                    self._emit(cur, task_id, "systemic_failure_signature", payload)
                    self._emit(cur, task_id, "gave_up", payload)
                    self._emit(cur, task_id, "blocked", {"reason": reason})
                    blocked.append(task_id)
        return blocked

    # --- dispatch reclaim primitives (A5) --------------------------------
    #
    # Each mirrors a kanban_db reclaim primitive board-scoped, reusing
    # ``_pg_end_run`` + ``record_task_failure`` where the reference uses
    # ``_end_run`` / ``_record_task_failure``. OS-level liveness/kill/exit are
    # injected and default to no-op: ``terminate_fn(pid, claim_lock)`` runs the
    # full host-guarded SIGTERM->grace->SIGKILL ladder (``signal_fn`` is the
    # single-shot fallback), ``pid_alive_fn`` probes liveness, and
    # ``classify_exit_fn`` classifies a dead pid's exit (rc=0 => protocol
    # violation). The gateway (Part B) wires the real host-local callbacks.

    @staticmethod
    def _invoke_kill(terminate_fn, signal_fn, pid, claim_lock):
        """Prefer the full host-guarded ladder (terminate_fn(pid, claim_lock));
        fall back to a single best-effort SIGTERM (signal_fn(pid, SIGTERM))."""
        if not pid:
            return
        if terminate_fn is not None:
            try:
                terminate_fn(int(pid), claim_lock)
            except Exception:
                pass
        elif signal_fn is not None:
            try:
                import signal as _sig
                signal_fn(int(pid), _sig.SIGTERM)
            except Exception:
                pass

    def _pg_release_stale_claims(self, *, terminate_fn=None, signal_fn=None,
                                  pid_alive_fn=None) -> int:
        """Mirror ``release_stale_claims``: TTL-expired running claims -> ready.

        When ``pid_alive_fn`` is supplied, a stale claim whose ``worker_pid``
        is still alive is EXTENDED (new claim_expires + ``claim_extended``
        event) instead of being reclaimed — preventing a duplicate spawn of a
        slow-but-alive worker (mirrors the SQLite reference behaviour).

        Pure-DB form otherwise. PG reclaims any claim whose ``claim_expires``
        has passed and closes the run with ``outcome='reclaimed'``.

        Note: the SQLite host_prefix (claim_lock) filter is NOT applied here —
        host-locality is a glue/single-host concern, not a store concern. This
        is a deliberate divergence from the SQLite reference; the glue layer
        supplies host-local OS callbacks (pid_alive_fn / terminate_fn) instead.
        """
        now = int(time.time())
        reclaimed = 0
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, claim_lock, worker_pid, claim_expires, "
                "last_heartbeat_at FROM tasks "
                "WHERE board=%s AND status='running' AND claim_expires IS NOT NULL "
                "AND claim_expires < %s",
                (self.board, now))
            stale = cur.fetchall()
        for row in stale:
            pid = row["worker_pid"]
            if pid_alive_fn is not None and pid:
                try:
                    alive = bool(pid_alive_fn(int(pid)))
                except Exception:
                    alive = False
                if alive:
                    new_expires = now + kanban_db._resolve_claim_ttl_seconds()
                    with self._pool.connection() as conn, \
                            conn.cursor(row_factory=dict_row) as cur:
                        with conn.transaction():
                            cur.execute(
                                "UPDATE tasks SET claim_expires=%s "
                                "WHERE board=%s AND id=%s AND status='running' "
                                "AND claim_expires IS NOT NULL AND claim_expires < %s",
                                (new_expires, self.board, row["id"], now))
                            if cur.rowcount != 1:
                                continue
                            cur.execute(
                                "SELECT current_run_id FROM tasks "
                                "WHERE board=%s AND id=%s",
                                (self.board, row["id"]))
                            rr = cur.fetchone()
                            run_id = rr["current_run_id"] if rr else None
                            if run_id is not None:
                                cur.execute(
                                    "UPDATE task_runs SET claim_expires=%s "
                                    "WHERE board=%s AND id=%s",
                                    (new_expires, self.board, run_id))
                            self._emit(cur, row["id"], "claim_extended", {
                                "reason": "pid_alive",
                                "worker_pid": int(pid),
                                "claim_lock": row["claim_lock"],
                                "claim_expires_was": int(row["claim_expires"]),
                                "claim_expires_now": new_expires,
                                "last_heartbeat_at": (
                                    int(row["last_heartbeat_at"])
                                    if row["last_heartbeat_at"] is not None
                                    else None),
                            }, run_id=run_id)
                    continue
            # --- existing kill + reclaim path ---
            self._invoke_kill(terminate_fn, signal_fn, pid, row["claim_lock"])
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "UPDATE tasks SET status='ready', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL "
                        "WHERE board=%s AND id=%s AND status='running' "
                        "AND claim_expires IS NOT NULL AND claim_expires < %s",
                        (self.board, row["id"], now))
                    if cur.rowcount != 1:
                        continue
                    run_id = self._pg_end_run(
                        cur, row["id"], outcome="reclaimed", status="reclaimed",
                        error=f"stale_lock={row['claim_lock']}")
                    payload = {
                        "stale_lock": row["claim_lock"],
                        "worker_pid": int(pid) if pid is not None else None,
                        "claim_expires": int(row["claim_expires"]),
                        "last_heartbeat_at": (
                            int(row["last_heartbeat_at"])
                            if row["last_heartbeat_at"] is not None else None),
                        "now": now,
                    }
                    self._emit(cur, row["id"], "reclaimed", payload, run_id=run_id)
                    reclaimed += 1
        return reclaimed

    def _pg_enforce_max_runtime(self, *, terminate_fn=None, signal_fn=None) -> list:
        """Mirror ``enforce_max_runtime``: running tasks past ``max_runtime_seconds``
        -> ready (+ timed_out event + failure counter).

        Identifies tasks past their per-attempt runtime budget (measured from
        the active run's ``started_at``, falling back to ``tasks.started_at``),
        signals the worker via the injected ``terminate_fn`` (preferred, full
        host-guarded ladder) or ``signal_fn`` (fallback, single best-effort
        SIGTERM), flips the task back to ``ready``, closes the run
        ``timed_out``, and records a failure (``release_claim=False``,
        ``end_run=False``) so the breaker can trip.

        # The PG store is host-agnostic by design (no host_prefix filter at this
        # layer; host-locality is enforced inside the injected callback).  Kill
        # is delegated to ``terminate_fn(pid, claim_lock)`` (preferred), which
        # runs the full host-guarded SIGTERM→grace→SIGKILL ladder — the
        # host-prefix guard lives inside that callback.  ``signal_fn`` is the
        # single-shot SIGTERM fallback used when ``terminate_fn`` is not
        # injected.
        """
        now = int(time.time())
        timed_out: list = []
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT t.id, t.worker_pid, t.claim_lock, "
                "COALESCE(r.started_at, t.started_at) AS active_started_at, "
                "t.max_runtime_seconds "
                "FROM tasks t LEFT JOIN task_runs r "
                "ON r.board=t.board AND r.id=t.current_run_id "
                "WHERE t.board=%s AND t.status='running' "
                "AND t.max_runtime_seconds IS NOT NULL "
                "AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
                "AND t.worker_pid IS NOT NULL",
                (self.board,))
            rows = cur.fetchall()
        for row in rows:
            elapsed = now - int(row["active_started_at"])
            limit = int(row["max_runtime_seconds"])
            if elapsed < limit:
                continue
            pid = int(row["worker_pid"])
            tid = row["id"]
            self._invoke_kill(terminate_fn, signal_fn, pid, row["claim_lock"])
            tripped = False
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "UPDATE tasks SET status='ready', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, "
                        "last_heartbeat_at=NULL "
                        "WHERE board=%s AND id=%s AND status='running'",
                        (self.board, tid))
                    if cur.rowcount != 1:
                        continue
                    payload = {
                        "pid": pid,
                        "elapsed_seconds": int(elapsed),
                        "limit_seconds": limit,
                    }
                    run_id = self._pg_end_run(
                        cur, tid, outcome="timed_out", status="timed_out",
                        error=f"elapsed {int(elapsed)}s > limit {limit}s",
                        metadata=payload)
                    self._emit(cur, tid, "timed_out", payload, run_id=run_id)
                    timed_out.append(tid)
                    tripped = True
            if tripped:
                # Counter-only failure bookkeep (task already at ready, run
                # already closed). May flip ready -> blocked if the breaker trips.
                self.record_task_failure(
                    tid, f"elapsed {int(elapsed)}s > limit {limit}s",
                    outcome="timed_out", release_claim=False, end_run=False,
                    event_payload_extra={"pid": pid})
        return timed_out

    def _pg_detect_stale_running(self, *, stale_timeout_seconds=0,
                                 terminate_fn=None, signal_fn=None) -> list:
        """Mirror ``detect_stale_running``: heartbeat-stale running tasks -> ready.

        A task is stale when it has been running longer than
        ``stale_timeout_seconds`` AND its ``last_heartbeat_at`` is older than
        ``_STALE_HEARTBEAT_GAP_SECONDS`` (or never sent). On reclaim it goes
        back to ``ready`` and the run closes ``outcome='stale'``. Deliberately
        does NOT call ``record_task_failure`` (matches the reference: stale
        reclaim is detection of an absent heartbeat, not a worker failure —
        counting it would let two legitimately long runs trip the breaker).

        # The PG store is host-agnostic by design (no host_prefix filter at this
        # layer).  Kill is delegated to ``terminate_fn(pid, claim_lock)``
        # (preferred), which runs the full host-guarded SIGTERM→grace→SIGKILL
        # ladder; ``signal_fn`` is the single-shot SIGTERM fallback used when
        # ``terminate_fn`` is not injected.
        """
        if stale_timeout_seconds <= 0:
            return []
        now = int(time.time())
        reclaimed: list = []
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT t.id, t.worker_pid, t.claim_lock, t.last_heartbeat_at, "
                "COALESCE(r.started_at, t.started_at) AS active_started_at "
                "FROM tasks t LEFT JOIN task_runs r "
                "ON r.board=t.board AND r.id=t.current_run_id "
                "WHERE t.board=%s AND t.status='running'",
                (self.board,))
            rows = cur.fetchall()
        for row in rows:
            if row["active_started_at"] is None:
                continue
            elapsed = now - int(row["active_started_at"])
            if elapsed < stale_timeout_seconds:
                continue
            last_hb = row["last_heartbeat_at"]
            hb_age = (now - int(last_hb)) if last_hb is not None else None
            if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
                continue
            pid = row["worker_pid"]
            tid = row["id"]
            self._invoke_kill(terminate_fn, signal_fn, pid, row["claim_lock"])
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "UPDATE tasks SET status='ready', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, "
                        "last_heartbeat_at=NULL "
                        "WHERE board=%s AND id=%s AND status='running'",
                        (self.board, tid))
                    if cur.rowcount != 1:
                        continue
                    payload = {
                        "elapsed_seconds": int(elapsed),
                        "last_heartbeat_at": (
                            int(last_hb) if last_hb is not None else None),
                        "heartbeat_age_seconds": (
                            int(hb_age) if hb_age is not None else None),
                        "timeout_seconds": stale_timeout_seconds,
                        "pid": int(pid) if pid else None,
                    }
                    run_id = self._pg_end_run(
                        cur, tid, outcome="stale", status="stale",
                        error=(
                            f"no heartbeat for {int(hb_age)}s "
                            if hb_age is not None else "no heartbeat ever")
                        + f"after {int(elapsed)}s running",
                        metadata=payload)
                    self._emit(cur, tid, "stale", payload, run_id=run_id)
                    reclaimed.append(tid)
        return reclaimed

    def _pg_detect_crashed_workers(self, *, pid_alive_fn=None,
                                   classify_exit_fn=None, skip_unknown=False) -> list:
        """Liveness-based crash reclaim with rc=0 protocol-violation classification
        + systemic-crash fingerprint cap-block (sqlite detect_crashed_workers
        parity). pid_alive_fn None => [] (no server-side OS liveness). skip_unknown
        leaves dead pids that classify 'unknown' for the stale/TTL lane (mirrors
        dispatch_once). Single-host: the sqlite host_prefix(claim_lock) filter is
        intentionally NOT applied (deferred multi-host parity item)."""
        if pid_alive_fn is None:
            return []
        crashed: list = []
        crash_details: list = []  # (tid, pid, lock, protocol_violation, error_text)
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, worker_pid, claim_lock FROM tasks "
                "WHERE board=%s AND status='running' AND worker_pid IS NOT NULL",
                (self.board,))
            rows = cur.fetchall()
        for row in rows:
            pid = int(row["worker_pid"])
            try:
                alive = bool(pid_alive_fn(pid))
            except Exception:
                alive = True  # be conservative: don't reclaim on probe error
            if alive:
                continue
            tid = row["id"]
            lock = row["claim_lock"] or ""
            kind, code = ("unknown", None)
            if classify_exit_fn is not None:
                try:
                    kind, code = classify_exit_fn(pid)
                except Exception:
                    kind, code = ("unknown", None)
            if kind == "unknown" and skip_unknown:
                continue
            protocol_violation = (kind == "clean_exit")
            if protocol_violation:
                error_text = ("worker exited cleanly (rc=0) without calling "
                              "kanban_complete or kanban_block — protocol violation")
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid, "claimer": lock, "exit_code": code,
                    "failure_class":
                        kanban_db.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT,
                    "guidance": kanban_db._PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE,
                }
            else:
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": lock}
                if code is not None and kind != "unknown":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "UPDATE tasks SET status='ready', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL "
                        "WHERE board=%s AND id=%s AND status='running' "
                        "AND worker_pid=%s", (self.board, tid, pid))
                    if cur.rowcount != 1:
                        continue
                    run_id = self._pg_end_run(
                        cur, tid, outcome="crashed", status="crashed",
                        error=error_text, metadata=event_payload)
                    self._emit(cur, tid, event_kind, event_payload, run_id=run_id)
                    crashed.append(tid)
                    crash_details.append(
                        (tid, pid, lock, protocol_violation, error_text))
        # Pass 2: fingerprint-aware failure accounting (sqlite parity). A genuine
        # (non-protocol-violation) crash whose error fingerprint recurs >= the
        # systemic threshold within this tick caps the breaker immediately.
        if crash_details:
            _fp_counts: dict = {}
            for _, _, _, _, err in crash_details:
                fp = kanban_db._error_fingerprint(err)
                _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
            for tid, pid, lock, protocol_violation, error_text in crash_details:
                fp = kanban_db._error_fingerprint(error_text)
                is_systemic = (
                    not protocol_violation
                    and _fp_counts.get(fp, 0)
                    >= kanban_db.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD)
                extra = {"pid": pid, "claimer": lock}
                if protocol_violation:
                    extra["failure_class"] = \
                        kanban_db.FAILURE_CLASS_PROTOCOL_VIOLATION_CLEAN_EXIT
                    extra["guidance"] = \
                        kanban_db._PROTOCOL_VIOLATION_CLEAN_EXIT_GUIDANCE
                self.record_task_failure(
                    tid, error_text, outcome="crashed",
                    failure_limit=1 if (protocol_violation or is_systemic) else None,
                    failure_limit_is_cap=bool(protocol_violation or is_systemic),
                    release_claim=False, end_run=False, event_payload_extra=extra)
        return crashed

    def _pg_promote_cleared_scheduled(self) -> int:
        """Mirror ``promote_cleared_scheduled``: un-park ``active_pr`` scheduled
        tasks whose PR guard has cleared, back to ``ready``. Pure DB.

        Targets ONLY parks marked ``respawn_guard='active_pr'`` on the most
        recent ``scheduled`` event; re-evaluates the same active-PR predicate.
        """
        promoted = 0
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, assignee FROM tasks WHERE board=%s AND status='scheduled'",
                (self.board,))
            rows = cur.fetchall()
        for row in rows:
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT payload FROM task_events "
                    "WHERE board=%s AND task_id=%s AND kind='scheduled' "
                    "ORDER BY id DESC LIMIT 1",
                    (self.board, row["id"]))
                ev = cur.fetchone()
            if not ev or not ev["payload"]:
                continue
            payload = ev["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    continue
            if not isinstance(payload, dict) or \
                    payload.get("respawn_guard") != "active_pr":
                continue  # time-based / operator park — leave alone
            if self._pg_active_pr_guard_holds(row["id"], row["assignee"]):
                continue  # PR still recent — stay parked
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                with conn.transaction():
                    cur.execute(
                        "UPDATE tasks SET status='ready' "
                        "WHERE board=%s AND id=%s AND status='scheduled'",
                        (self.board, row["id"]))
                    if cur.rowcount != 1:
                        continue
                    self._emit(
                        cur, row["id"], "ready",
                        {"reason": "active_pr respawn guard cleared; "
                                   "auto-promoted to ready"})
                    promoted += 1
        return promoted

    # --- respawn guard (A5, board-scoped reimplementation of check_respawn_guard) ---

    def _pg_active_pr_guard_holds(self, task_id: str,
                                  assignee: Optional[str]) -> bool:
        """Mirror ``active_pr_guard_holds`` board-scoped: True iff a non-reviewer
        task has a GitHub PR URL in a comment newer than the PR guard window."""
        if (assignee or "").casefold() == "reviewer":
            return False
        pr_cutoff = int(time.time()) - _RESPAWN_GUARD_PR_WINDOW
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT body FROM task_comments "
                "WHERE board=%s AND task_id=%s AND created_at >= %s",
                (self.board, task_id, pr_cutoff))
            for c in cur.fetchall():
                if c["body"] and _RESPAWN_GUARD_PR_URL_RE.search(c["body"]):
                    return True
        return False

    def _pg_check_respawn_guard(self, task_id: str) -> Optional[str]:
        """Mirror ``check_respawn_guard`` board-scoped. Returns a guard reason
        (``blocker_auth`` / ``recent_success`` / ``active_pr``) or None."""
        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT assignee, last_failure_error FROM tasks "
                "WHERE board=%s AND id=%s",
                (self.board, task_id))
            row = cur.fetchone()
            if row is None:
                return None
            err = row["last_failure_error"]
            if err and _RESPAWN_BLOCKER_RE.search(err):
                return "blocker_auth"
            cutoff = int(time.time()) - _RESPAWN_GUARD_SUCCESS_WINDOW
            cur.execute(
                "SELECT id FROM task_runs "
                "WHERE board=%s AND task_id=%s AND outcome='completed' "
                "AND ended_at >= %s LIMIT 1",
                (self.board, task_id, cutoff))
            if cur.fetchone():
                return "recent_success"
            assignee = row["assignee"]
        if self._pg_active_pr_guard_holds(task_id, assignee):
            return "active_pr"
        return None

    # --- dispatch_plan (A5 dispatch core) --------------------------------

    def dispatch_plan(self, *, max_spawn=None, max_in_progress=None,
                      failure_limit=DEFAULT_FAILURE_LIMIT,
                      stale_timeout_seconds=0, default_assignee=None,
                      max_in_progress_per_profile=None, ttl_seconds=None,
                      resolve_workspace=None, profile_exists=None,
                      terminate_fn=None, signal_fn=None, pid_alive_fn=None,
                      classify_exit_fn=None):
        """One dispatcher tick reimplemented for PG: reclaim + ready-scan +
        claim + workspace-resolve, WITHOUT zombie-reap and WITHOUT the real
        spawn (the glue spawns; this returns the claimed tasks in
        ``DispatchPlan.to_spawn``).

        Mirrors ``kanban_db.dispatch_once`` MINUS the os.waitpid zombie reap
        (OS, host-local) and MINUS spawn_fn invocation. Reclaim OS callbacks
        (``signal_fn`` / ``pid_alive_fn``) are injected and default to no-op.
        ``resolve_workspace`` / ``profile_exists`` are injected callbacks:
        ``resolve_workspace(task, board=...) -> path``; ``profile_exists(name)
        -> bool``. When ``profile_exists`` is None every assignee is treated as
        spawnable. When ``resolve_workspace`` is None the workspace is left as
        the task's existing path (may be None).
        """
        from hermes_cli.kanban.store import DispatchPlan

        result = DispatchResult()

        # --- reclaim phase (mirror dispatch_once ordering) ---------------
        result.crashed = self._pg_detect_crashed_workers(
            pid_alive_fn=pid_alive_fn, classify_exit_fn=classify_exit_fn,
            skip_unknown=True)
        result.reclaimed = self._pg_release_stale_claims(
            terminate_fn=terminate_fn, signal_fn=signal_fn,
            pid_alive_fn=pid_alive_fn)
        result.stale = self._pg_detect_stale_running(
            stale_timeout_seconds=stale_timeout_seconds,
            terminate_fn=terminate_fn, signal_fn=signal_fn)
        result.timed_out = self._pg_enforce_max_runtime(
            terminate_fn=terminate_fn, signal_fn=signal_fn)
        if kanban_db._promote_scheduled_enabled():
            self._pg_promote_cleared_scheduled()
        result.promoted = self.recompute_ready()

        # --- concurrency caps --------------------------------------------
        running_count = 0
        if max_spawn is not None or max_in_progress is not None:
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM tasks "
                    "WHERE board=%s AND status='running'", (self.board,))
                running_count = int(cur.fetchone()["n"])

        with self._pool.connection() as conn, \
                conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, assignee FROM tasks "
                "WHERE board=%s AND status='ready' AND claim_lock IS NULL "
                "ORDER BY priority DESC, created_at ASC",
                (self.board,))
            ready_rows = cur.fetchall()
        result.ready_count = len(ready_rows)

        if max_in_progress is not None:
            if max_spawn is None or max_spawn > max_in_progress:
                max_spawn = max_in_progress
            if ready_rows and max_spawn is not None and running_count >= max_spawn:
                result.max_in_progress_blocked = True
                return DispatchPlan(to_spawn=[], result=result)

        # --- per-profile cap (count currently-running per assignee) ------
        per_profile_cap = (
            max_in_progress_per_profile
            if isinstance(max_in_progress_per_profile, int)
            and max_in_progress_per_profile > 0 else None)
        per_profile_running: dict = {}
        if per_profile_cap is not None:
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT assignee, count(*) AS n FROM tasks "
                    "WHERE board=%s AND status='running' AND assignee IS NOT NULL "
                    "GROUP BY assignee", (self.board,))
                for prow in cur.fetchall():
                    per_profile_running[str(prow["assignee"])] = int(prow["n"])

        # --- default-assignee resolution ---------------------------------
        default_assignee = (default_assignee or "").strip() or None
        default_assignee_resolved = False
        if default_assignee:
            if profile_exists is None:
                default_assignee_resolved = True
            else:
                try:
                    default_assignee_resolved = bool(profile_exists(default_assignee))
                except Exception:
                    default_assignee_resolved = True

        def _profile_ok(name) -> bool:
            if profile_exists is None:
                return True
            try:
                return bool(profile_exists(name))
            except Exception:
                return True

        to_spawn: list = []
        spawned = 0

        for row in ready_rows:
            if max_spawn is not None and running_count + spawned >= max_spawn:
                break
            tid = row["id"]
            row_assignee = row["assignee"]
            if not row_assignee:
                if default_assignee and default_assignee_resolved:
                    with self._pool.connection() as conn, \
                            conn.cursor(row_factory=dict_row) as cur:
                        with conn.transaction():
                            cur.execute(
                                "UPDATE tasks SET assignee=%s WHERE board=%s "
                                "AND id=%s AND (assignee IS NULL OR assignee='')",
                                (default_assignee, self.board, tid))
                            if cur.rowcount == 1:
                                self._emit(cur, tid, "assigned", {
                                    "assignee": default_assignee,
                                    "source": "kanban.default_assignee"})
                    row_assignee = default_assignee
                    result.auto_assigned_default.append(tid)
                else:
                    result.skipped_unassigned.append(tid)
                    continue
            # Skip ready tasks whose assignee is not a real spawnable profile.
            if not _profile_ok(row_assignee):
                result.skipped_nonspawnable.append(tid)
                continue
            if per_profile_cap is not None:
                current = per_profile_running.get(str(row_assignee), 0)
                if current >= per_profile_cap:
                    result.skipped_per_profile_capped.append(
                        (tid, str(row_assignee), current))
                    continue
            result.spawnable_ready += 1

            task_for_validation = self.get_task(tid)
            if task_for_validation is None:
                continue
            validation_errors = _pre_spawn_validation_errors(task_for_validation)
            if validation_errors:
                reason = "; ".join(validation_errors)
                result.pre_spawn_blocked.append((tid, reason))
                if self._pg_record_pre_spawn_validation_failure(tid, validation_errors):
                    result.auto_blocked.append(tid)
                continue

            guard_reason = self._pg_check_respawn_guard(tid)
            if guard_reason is not None:
                result.respawn_guarded.append((tid, guard_reason))
                with self._pool.connection() as conn, \
                        conn.cursor(row_factory=dict_row) as cur:
                    with conn.transaction():
                        self._emit(cur, tid, "respawn_guarded",
                                   {"reason": guard_reason})
                        if guard_reason == "blocker_auth":
                            reason = ("respawn guard: auth/quota blocker "
                                      "detected; operator action is required "
                                      "before retry")
                            cur.execute(
                                "UPDATE tasks SET status='blocked', "
                                "claim_lock=NULL, claim_expires=NULL, "
                                "worker_pid=NULL WHERE board=%s AND id=%s "
                                "AND status='ready'", (self.board, tid))
                            run_id = self._pg_end_run(
                                cur, tid, outcome="blocked", status="blocked",
                                summary=reason,
                                metadata={"respawn_guard": guard_reason})
                            self._emit(cur, tid, "blocked", {"reason": reason},
                                       run_id=run_id)
                        elif guard_reason == "active_pr":
                            reason = ("respawn guard: recent PR URL detected; "
                                      "task parked to prevent duplicate PR "
                                      "creation")
                            cur.execute(
                                "UPDATE tasks SET status='scheduled', "
                                "claim_lock=NULL, claim_expires=NULL, "
                                "worker_pid=NULL WHERE board=%s AND id=%s "
                                "AND status='ready'", (self.board, tid))
                            run_id = self._pg_end_run(
                                cur, tid, outcome="scheduled", status="scheduled",
                                summary=reason,
                                metadata={"respawn_guard": guard_reason})
                            self._emit(
                                cur, tid, "scheduled",
                                {"reason": reason, "respawn_guard": guard_reason},
                                run_id=run_id)
                continue

            claimed = self.claim_task(tid, ttl_seconds=ttl_seconds)
            if claimed is None:
                continue
            result.spawn_attempts += 1
            workspace = None
            if resolve_workspace is not None:
                try:
                    workspace = resolve_workspace(claimed, board=self.board)
                except Exception as exc:
                    self.record_spawn_failure(claimed.id, f"workspace: {exc}",
                                              failure_limit=failure_limit)
                    result.spawn_failures += 1
                    continue
                self.set_workspace_path(claimed.id, str(workspace))
            else:
                workspace = claimed.workspace_path
            to_spawn.append(
                (claimed, str(workspace) if workspace is not None else None))
            result.spawned.append(
                (claimed.id, claimed.assignee or "",
                 str(workspace) if workspace is not None else ""))
            spawned += 1
            if per_profile_cap is not None and claimed.assignee:
                key = str(claimed.assignee)
                per_profile_running[key] = per_profile_running.get(key, 0) + 1

        return DispatchPlan(to_spawn=to_spawn, result=result)

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

    # --- worker context --------------------------------------------------

    def build_worker_context(self, task_id: str) -> str:
        """Return the full text a worker should read to understand its task.

        Mirror of kanban_db.build_worker_context (hermes_cli/kanban_db.py);
        byte-parity pinned by tests/hermes_cli/kanban/test_build_worker_context_parity.py
        — keep in sync.

        Byte-identical to ``kanban_db.build_worker_context`` for identical
        logical data. Reproduces the upstream section order and per-field
        caps so PG workers see the same context sqlite workers do (closes the
        worker split-brain). See the upstream docstring for the full ordering
        contract; do not let this drift from it.
        """
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"unknown task {task_id}")

        # Per-field truncation helper — reproduced verbatim from upstream.
        def _cap(s: Optional[str],
                 limit: int = kanban_db._CTX_MAX_FIELD_BYTES) -> str:
            if not s:
                return ""
            s = s.strip()
            if len(s) <= limit:
                return s
            return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"

        lines: list[str] = []
        lines.append(f"# Kanban task {task.id}: {task.title}")
        lines.append("")
        lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
        lines.append(f"Status:   {task.status}")
        if task.tenant:
            lines.append(f"Tenant:   {task.tenant}")
        lines.append(
            f"Workspace: {task.workspace_kind} @ "
            f"{task.workspace_path or '(unresolved)'}")
        if task.max_runtime_seconds is not None:
            terminal_timeout = kanban_db._worker_terminal_timeout_env(
                task.max_runtime_seconds,
                os.environ.get("TERMINAL_TIMEOUT"),
            )
            effective_terminal_timeout = (
                terminal_timeout or os.environ.get("TERMINAL_TIMEOUT"))
            lines.append(f"Max runtime: {task.max_runtime_seconds}s")
            if effective_terminal_timeout:
                lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
        if task.branch_name:
            lines.append(f"Branch:   {task.branch_name}")
        lines.append("")

        lines.append("## Closeout requirement (do not skip)")
        lines.append(
            "Before you exit, you MUST call exactly one terminal kanban tool: "
            "`kanban_complete(summary=..., metadata=...)` on success, or "
            "`kanban_block(reason=...)` if you cannot continue. Exiting "
            "cleanly without one of these is a protocol violation "
            "(`failure_class=protocol_violation_clean_exit`) and will auto-"
            "block this task on the first occurrence."
        )
        lines.append("")

        expected_review_head = None
        lane_type = _lane_type_for_assignee(task.assignee)
        if lane_type == "implementation":
            lines.append("## Implementation PR evidence")
            lines.append(
                "If this task opens or updates a PR, include structured PR "
                "evidence in `kanban_complete(..., metadata=...)`: `pr_url`, "
                "`pull_request_head_sha`, and `branch_name`. Downstream "
                "final-review tasks use that SHA to prevent stale approvals. "
                "If no PR exists yet, say so explicitly in the summary/metadata."
            )
            lines.append("")
        if lane_type == "review":
            expected_review_head = self._pg_expected_parent_pr_head_sha(task_id)
        if expected_review_head is not None:
            expected_sha, parent_task_id, parent_run_id = expected_review_head
            lines.append("## Final-review PR-head gate")
            lines.append(
                "This review has a parent implementation closeout with current "
                f"PR head `{expected_sha}` (parent `{parent_task_id}`"
                + (f", run `{parent_run_id}`" if parent_run_id is not None else "")
                + "). To complete this task, your `kanban_complete` metadata "
                f"MUST include `reviewed_pr_head_sha: {expected_sha}`. If the "
                "PR head has changed or you cannot verify it, call "
                "`kanban_block` instead of approving stale evidence."
            )
            lines.append("")

        if task.body and task.body.strip():
            lines.append("## Body")
            lines.append(_cap(task.body, kanban_db._CTX_MAX_BODY_BYTES))
            lines.append("")

        # Prior attempts — closed runs only (skip the active worker's run).
        all_prior = [r for r in self.list_runs(task_id) if r.ended_at is not None]
        if len(all_prior) > kanban_db._CTX_MAX_PRIOR_ATTEMPTS:
            omitted = len(all_prior) - kanban_db._CTX_MAX_PRIOR_ATTEMPTS
            shown = all_prior[-kanban_db._CTX_MAX_PRIOR_ATTEMPTS:]
            first_shown_idx = omitted + 1
        else:
            omitted = 0
            shown = all_prior
            first_shown_idx = 1
        if shown:
            lines.append("## Prior attempts on this task")
            if omitted:
                lines.append(
                    f"_({omitted} earlier attempt{'s' if omitted != 1 else ''} "
                    f"omitted; showing most recent {len(shown)})_"
                )
            for offset, run in enumerate(shown):
                idx = first_shown_idx + offset
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(run.started_at))
                profile = run.profile or "(unknown)"
                outcome = run.outcome or run.status
                lines.append(f"### Attempt {idx} — {outcome} ({profile}, {ts})")
                if run.summary and run.summary.strip():
                    lines.append(_cap(run.summary))
                if run.error and run.error.strip():
                    lines.append(f"_error_: {_cap(run.error)}")
                if run.metadata:
                    try:
                        meta_str = json.dumps(
                            run.metadata, ensure_ascii=False, sort_keys=True)
                        lines.append(f"_metadata_: `{_cap(meta_str)}`")
                    except Exception:
                        pass
                lines.append("")

        # Parents: prefer the most-recent 'completed' run's summary + metadata,
        # fall back to ``task.result`` when no run rows exist.
        dependency_parent_ids = self.parent_ids(
            task_id, relation_type=LINK_RELATION_DEPENDENCY)

        if dependency_parent_ids:
            wrote_header = False
            for pid in dependency_parent_ids:
                pt = self.get_task(pid)
                if not pt or pt.status != "done":
                    continue
                runs = [r for r in self.list_runs(pid)
                        if r.outcome == "completed"]
                runs.sort(key=lambda r: r.started_at, reverse=True)
                run = runs[0] if runs else None

                if not wrote_header:
                    lines.append("## Parent task results")
                    wrote_header = True
                lines.append(f"### {pid}")

                body_lines: list[str] = []
                if run is not None and run.summary and run.summary.strip():
                    body_lines.append(_cap(run.summary))
                elif pt.result:
                    body_lines.append(_cap(pt.result))
                else:
                    body_lines.append("(no result recorded)")

                if run is not None and run.metadata:
                    try:
                        meta_str = json.dumps(
                            run.metadata, ensure_ascii=False, sort_keys=True)
                        body_lines.append(f"_metadata_: `{_cap(meta_str)}`")
                    except Exception:
                        pass
                lines.extend(body_lines)
                lines.append("")

        # Cross-task role history: most recent 5 completed runs on OTHER tasks
        # by this assignee. Board-scoped (the one query not behind a store
        # method). Safe on assignee=None (skipped).
        if task.assignee:
            with self._pool.connection() as conn, \
                    conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT t.id, t.title, r.summary, r.ended_at "
                    "FROM task_runs r "
                    "JOIN tasks t ON r.board = t.board AND r.task_id = t.id "
                    "WHERE r.board = %s AND r.profile = %s AND r.task_id <> %s "
                    "  AND r.outcome = 'completed' "
                    "ORDER BY r.ended_at DESC LIMIT 5",
                    (self.board, task.assignee, task_id))
                role_rows = cur.fetchall()
            if role_rows:
                lines.append(f"## Recent work by @{task.assignee}")
                for row in role_rows:
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(int(row["ended_at"])))
                    s = (row["summary"] or "").strip().splitlines()
                    first = s[0][:200] if s else "(no summary)"
                    lines.append(f"- {row['id']} — {row['title']} ({ts}): {first}")
                lines.append("")

        # Comments: cap at the most-recent _CTX_MAX_COMMENTS.
        all_comments = self.list_comments(task_id)
        if len(all_comments) > kanban_db._CTX_MAX_COMMENTS:
            omitted_c = len(all_comments) - kanban_db._CTX_MAX_COMMENTS
            shown_c = all_comments[-kanban_db._CTX_MAX_COMMENTS:]
        else:
            omitted_c = 0
            shown_c = all_comments
        if shown_c:
            lines.append("## Comment thread")
            if omitted_c:
                lines.append(
                    f"_({omitted_c} earlier comment"
                    f"{'s' if omitted_c != 1 else ''} "
                    f"omitted; showing most recent {len(shown_c)})_"
                )
            for c in shown_c:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(c.created_at))
                safe_author = (c.author or "").replace("`", "")
                lines.append(f"comment from worker `{safe_author}` at {ts}:")
                lines.append(_cap(c.body, kanban_db._CTX_MAX_COMMENT_BYTES))
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

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
                if cur.rowcount != 1:
                    return (old_cursor, old_cursor, [])
                events = [
                    Event(**{k: r[k] for k in Event.__dataclass_fields__})
                    for r in event_rows
                ]
                return (old_cursor, new_cursor, events)

    def advance_notify_cursor(self, *, task_id, platform, chat_id,
                              thread_id=None, new_cursor) -> None:
        thread_id = thread_id or ''
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE kanban_notify_subs "
                    "SET last_event_id = GREATEST(last_event_id, %s) "
                    "WHERE board=%s AND task_id=%s AND platform=%s "
                    "AND chat_id=%s AND thread_id=%s",
                    (int(new_cursor), self.board, task_id, platform,
                     chat_id, thread_id))

    def rewind_notify_cursor(self, *, task_id, platform, chat_id,
                             thread_id=None, claimed_cursor, old_cursor) -> bool:
        thread_id = thread_id or ''
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE kanban_notify_subs SET last_event_id=%s "
                    "WHERE board=%s AND task_id=%s AND platform=%s "
                    "AND chat_id=%s AND thread_id=%s AND last_event_id=%s",
                    (int(old_cursor), self.board, task_id, platform,
                     chat_id, thread_id, int(claimed_cursor)))
                return cur.rowcount > 0

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
                if cur.rowcount != 1:
                    return (old_cursor, old_cursor, [])
                return (old_cursor, new_cursor, claimed_events)

    def advance_profile_event_cursor(self, *, task_id, profile, name="",
                                     new_cursor, last_wake_at=None) -> None:
        name = name or ""
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                if last_wake_at is None:
                    cur.execute(
                        "UPDATE kanban_profile_event_subs "
                        "SET last_event_id = GREATEST(last_event_id, %s) "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (int(new_cursor), self.board, task_id, profile, name))
                else:
                    cur.execute(
                        "UPDATE kanban_profile_event_subs "
                        "SET last_event_id = GREATEST(last_event_id, %s), "
                        "    last_wake_at = %s "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                        (int(new_cursor), int(last_wake_at),
                         self.board, task_id, profile, name))

    def rewind_profile_event_cursor(self, *, task_id, profile, name="",
                                    claimed_cursor, old_cursor) -> bool:
        name = name or ""
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE kanban_profile_event_subs SET last_event_id=%s "
                    "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s "
                    "AND last_event_id=%s",
                    (int(old_cursor), self.board, task_id, profile, name,
                     int(claimed_cursor)))
                rewound = cur.rowcount > 0
                cur.execute(
                    "DELETE FROM kanban_profile_event_claims "
                    "WHERE board=%s AND root_task_id=%s AND profile=%s AND name=%s "
                    "AND event_id > %s AND event_id <= %s",
                    (self.board, task_id, profile, name,
                     int(old_cursor), int(claimed_cursor)))
                return rewound

    def record_profile_wake_success(self, *, task_id, profile, name="",
                                    new_cursor, last_wake_at) -> int:
        name = name or ""
        when = int(last_wake_at)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                cur.execute(
                    "UPDATE kanban_profile_event_subs SET "
                    "    last_event_id      = GREATEST(last_event_id, %s), "
                    "    last_wake_at       = %s, "
                    "    last_wake_error_at = NULL, "
                    "    last_wake_error    = NULL, "
                    "    wake_failure_count = 0 "
                    "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                    (int(new_cursor), when, self.board, task_id, profile, name))
                cur.execute(
                    "INSERT INTO kanban_profile_wake_events "
                    "(board, task_id, profile, name, status, error, "
                    " claimed_event_cursor, created_at) "
                    "VALUES (%s,%s,%s,%s,'success',NULL,%s,%s) RETURNING id",
                    (self.board, task_id, profile, name, int(new_cursor), when))
                return int(cur.fetchone()["id"])

    def record_profile_wake_failure(
        self, *, task_id, profile, name="", claimed_cursor, old_cursor,
        error=None, at=None,
        min_event_interval_seconds=DEFAULT_PROFILE_WAKE_FAILURE_EVENT_MIN_INTERVAL_SECONDS,
    ) -> int:
        name = name or ""
        when = int(at if at is not None else time.time())
        min_event_interval_seconds = max(0, int(min_event_interval_seconds or 0))
        sanitized = kanban_db._sanitize_wake_error(error)
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                # CAS-guarded cursor rewind (mirrors rewind_profile_event_cursor).
                cur.execute(
                    "UPDATE kanban_profile_event_subs SET last_event_id=%s "
                    "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s "
                    "AND last_event_id=%s",
                    (int(old_cursor), self.board, task_id, profile, name,
                     int(claimed_cursor)))
                cur.execute(
                    "DELETE FROM kanban_profile_event_claims "
                    "WHERE board=%s AND root_task_id=%s AND profile=%s AND name=%s "
                    "AND event_id > %s AND event_id <= %s",
                    (self.board, task_id, profile, name,
                     int(old_cursor), int(claimed_cursor)))
                cur.execute(
                    "UPDATE kanban_profile_event_subs SET "
                    "    last_wake_error_at = %s, "
                    "    last_wake_error    = %s, "
                    "    wake_failure_count = wake_failure_count + 1 "
                    "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s",
                    (when, sanitized, self.board, task_id, profile, name))
                if min_event_interval_seconds:
                    cur.execute(
                        "SELECT id FROM kanban_profile_wake_events "
                        "WHERE board=%s AND task_id=%s AND profile=%s AND name=%s "
                        "  AND status='failed' AND created_at >= %s "
                        "ORDER BY id DESC LIMIT 1",
                        (self.board, task_id, profile, name,
                         when - min_event_interval_seconds))
                    existing = cur.fetchone()
                    if existing is not None:
                        return int(existing["id"])
                cur.execute(
                    "INSERT INTO kanban_profile_wake_events "
                    "(board, task_id, profile, name, status, error, "
                    " claimed_event_cursor, created_at) "
                    "VALUES (%s,%s,%s,%s,'failed',%s,%s,%s) RETURNING id",
                    (self.board, task_id, profile, name, sanitized,
                     int(claimed_cursor), when))
                return int(cur.fetchone()["id"])

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

    def list_profile_wake_events(
        self,
        *,
        task_id: Optional[str] = None,
        profile: Optional[str] = None,
        name: Optional[str] = None,
        since_id: int = 0,
        limit: int = 200,
    ) -> list:
        where = ["board = %s", "id > %s"]
        params: list[Any] = [self.board, int(since_id)]
        if task_id is not None:
            where.append("task_id = %s")
            params.append(task_id)
        if profile is not None:
            where.append("profile = %s")
            params.append(profile)
        if name is not None:
            where.append("name = %s")
            params.append(name)
        sql = (
            "SELECT id, task_id, profile, name, status, error, "
            "       claimed_event_cursor, created_at "
            "FROM kanban_profile_wake_events "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY id ASC LIMIT %s"
        )
        params.append(int(limit))
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def record_notifier_heartbeat(self, **kwargs) -> None:
        # Notifier-heartbeat telemetry is board-independent and intentionally
        # lives in a shared SQLite sidecar for BOTH backends, never in the
        # board DB. Delegate to the kanban_db wrapper (not the raw sidecar) so
        # PG inherits its swallow-and-warn guard: a corrupt sidecar must not
        # crash a notifier tick. Gives byte-identical cross-backend behavior.
        return kanban_db.record_notifier_heartbeat(**kwargs)

    def list_notifier_heartbeats(self, **kwargs) -> list:
        # Delegate to the kanban_db wrapper for cross-backend parity.
        return kanban_db.list_notifier_heartbeats(**kwargs)

    def heartbeat_worker(
        self,
        *,
        task_id: str,
        note: Optional[str] = None,
        expected_run_id: Optional[int] = None,
        min_event_interval_seconds: Optional[int] = None,
    ) -> bool:
        if min_event_interval_seconds is None:
            min_event_interval_seconds = DEFAULT_HEARTBEAT_EVENT_MIN_INTERVAL_SECONDS
        min_event_interval_seconds = max(0, int(min_event_interval_seconds or 0))
        now = int(time.time())
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            with conn.transaction():
                if expected_run_id is None:
                    cur.execute(
                        "UPDATE tasks SET last_heartbeat_at=%s "
                        "WHERE board=%s AND id=%s AND status='running'",
                        (now, self.board, task_id))
                else:
                    cur.execute(
                        "UPDATE tasks SET last_heartbeat_at=%s "
                        "WHERE board=%s AND id=%s AND status='running' "
                        "AND current_run_id=%s",
                        (now, self.board, task_id, int(expected_run_id)))
                if cur.rowcount != 1:
                    return False
                if expected_run_id is not None:
                    run_id = int(expected_run_id)
                else:
                    cur.execute(
                        "SELECT current_run_id FROM tasks "
                        "WHERE board=%s AND id=%s",
                        (self.board, task_id))
                    row = cur.fetchone()
                    run_id = (int(row["current_run_id"])
                              if row and row["current_run_id"] else None)
                if run_id is not None:
                    cur.execute(
                        "UPDATE task_runs SET last_heartbeat_at=%s "
                        "WHERE board=%s AND id=%s",
                        (now, self.board, run_id))

                should_append_event = True
                if min_event_interval_seconds:
                    if run_id is None:
                        cur.execute(
                            "SELECT created_at FROM task_events "
                            "WHERE board=%s AND task_id=%s AND kind='heartbeat' "
                            "ORDER BY id DESC LIMIT 1",
                            (self.board, task_id))
                    else:
                        cur.execute(
                            "SELECT created_at FROM task_events "
                            "WHERE board=%s AND task_id=%s AND kind='heartbeat' "
                            "AND run_id=%s ORDER BY id DESC LIMIT 1",
                            (self.board, task_id, run_id))
                    last_event = cur.fetchone()
                    if last_event is not None:
                        should_append_event = (
                            now - int(last_event["created_at"])
                            >= min_event_interval_seconds
                        )
                if should_append_event:
                    self._emit(cur, task_id, "heartbeat",
                               {"note": note} if note else None,
                               run_id=run_id)
        return True

    def edit_completed_task_result(self, task_id, **kwargs):
        raise NotImplementedError("phase-2-tail: edit_completed_task_result")
