"""Postgres read helpers for the kanban dashboard plugin.

Mirrors the dashboard's direct-sqlite aggregate/tail/diagnostic reads as
board-scoped Postgres SQL, following kanban_board_doctor._run_board_doctor_pg
(pg_pool.get_pool() + dict_row, WHERE board=%s). Used only on the
resolve_backend()=="postgres" branches in plugin_api.py; the sqlite path is
untouched. The DSN is never logged.
"""
from __future__ import annotations

from typing import Optional

from hermes_cli import kanban_db


def slug(board: Optional[str]) -> str:
    """Resolve a board query-param to a normalised slug (defaults to current)."""
    s = board or kanban_db.get_current_board()
    try:
        return kanban_db._normalize_board_slug(s) or kanban_db.DEFAULT_BOARD
    except Exception:
        return kanban_db.DEFAULT_BOARD


def _pool():
    from hermes_cli.kanban import pg_pool
    return pg_pool.get_pool()


def _query(sql: str, params: tuple) -> list[dict]:
    from psycopg.rows import dict_row
    with _pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def link_counts(board: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in _query("SELECT parent_id, child_id FROM task_links WHERE board=%s", (board,)):
        out.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        out.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
    return out


def comment_counts(board: str) -> dict[str, int]:
    rows = _query(
        "SELECT task_id, COUNT(*) AS n FROM task_comments WHERE board=%s GROUP BY task_id",
        (board,),
    )
    return {r["task_id"]: int(r["n"]) for r in rows}


def child_progress(board: str) -> dict[str, dict[str, int]]:
    progress: dict[str, dict[str, int]] = {}
    rows = _query(
        "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l "
        "JOIN tasks t ON t.board = l.board AND t.id = l.child_id WHERE l.board=%s",
        (board,),
    )
    for row in rows:
        p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
        p["total"] += 1
        if row["cstatus"] == "done":
            p["done"] += 1
    return progress


def distinct_tenants(board: str) -> list[str]:
    rows = _query(
        "SELECT DISTINCT tenant FROM tasks WHERE board=%s AND tenant IS NOT NULL "
        "ORDER BY tenant", (board,),
    )
    return [r["tenant"] for r in rows]


def distinct_assignees(board: str) -> list[str]:
    rows = _query(
        "SELECT DISTINCT assignee FROM tasks WHERE board=%s AND assignee IS NOT NULL "
        "AND status != 'archived' ORDER BY assignee", (board,),
    )
    return [r["assignee"] for r in rows]


def latest_event_id(board: str) -> int:
    rows = _query("SELECT COALESCE(MAX(id), 0) AS m FROM task_events WHERE board=%s", (board,))
    return int(rows[0]["m"]) if rows else 0


def board_counts(board: str) -> dict[str, int]:
    rows = _query(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE board=%s GROUP BY status", (board,),
    )
    return {r["status"]: int(r["n"]) for r in rows}
