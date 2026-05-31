"""Read-only board-liveness signals + threshold evaluation (WS6).

The dominant kanban failure mode is a *silent* stall: a task sits in
``ready``/``blocked``/``running`` and nothing screams. :func:`compute_board_liveness`
turns that into measurable signals over a read-only connection, and
:func:`evaluate` flags any dimension past a configured threshold so a gateway
checker can page within minutes.

All timestamps are epoch seconds (the kanban schema's unit). Pure + read-only:
no writes, no clock calls inside compute (the caller passes ``now``) so it is
deterministic and safe to run against a ``mode=ro`` connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Liveness:
    """A point-in-time board-liveness snapshot. Ages are seconds; 0 means
    "no task in that state" (or the subsystem signal is nominal)."""
    oldest_ready_age_seconds: int = 0
    oldest_blocked_done_parents_age_seconds: int = 0
    oldest_stale_running_age_seconds: int = 0
    notifier_enabled: bool = True
    writer_daemon_disabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # Note: there is no gateway-local "dispatcher_enabled" signal. Dispatch
    # stalls are caught by oldest_ready_age_seconds, which is correct whether the
    # dispatcher runs in-gateway or as an external `hermes kanban dispatch`
    # process; a binary in-gateway flag would false-page the external case.


@dataclass
class Breach:
    dimension: str
    value: int
    threshold: int


def _scalar(conn, sql: str, *params) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def compute_board_liveness(conn, *, now: int) -> Liveness:
    """Compute board-liveness signals from a (read-only) connection.

    * ``oldest_ready_age_seconds`` — age of the oldest ``ready`` task; a ready
      task that never gets dispatched is the clearest stall.
    * ``oldest_blocked_done_parents_age_seconds`` — oldest ``blocked`` task with
      NO dependency parent still open (every dep parent done/archived). A task
      that *could* run but is still blocked is a silent stall; one waiting on a
      live parent is legitimately blocked and excluded.
    * ``oldest_stale_running_age_seconds`` — oldest ``running`` task measured
      from its last heartbeat (falling back to ``started_at``/``created_at``),
      i.e. how long since a running worker last proved liveness.
    """
    oldest_ready = _scalar(
        conn,
        "SELECT MAX(? - created_at) FROM tasks WHERE status = 'ready'",
        now,
    )
    oldest_blocked = _scalar(
        conn,
        "SELECT MAX(? - t.created_at) FROM tasks t "
        "WHERE t.status = 'blocked' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "  WHERE l.child_id = t.id AND l.relation_type = 'dependency' "
        "  AND p.status NOT IN ('done', 'archived')"
        ")",
        now,
    )
    oldest_stale_running = _scalar(
        conn,
        "SELECT MAX(? - COALESCE(last_heartbeat_at, started_at, created_at)) "
        "FROM tasks WHERE status = 'running'",
        now,
    )
    return Liveness(
        oldest_ready_age_seconds=max(0, oldest_ready),
        oldest_blocked_done_parents_age_seconds=max(0, oldest_blocked),
        oldest_stale_running_age_seconds=max(0, oldest_stale_running),
    )


def _scalar_pg(cur, sql: str, params) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return 0
    val = row[0] if not isinstance(row, dict) else next(iter(row.values()))
    return int(val) if val is not None else 0


def compute_board_liveness_pg(cur, board: str, *, now: int) -> Liveness:
    """Postgres equivalent of compute_board_liveness: same three invariants,
    board-scoped, over a psycopg cursor. Mirrors the sqlite query logic."""
    oldest_ready = _scalar_pg(
        cur,
        "SELECT MAX(%s - created_at) FROM tasks WHERE board=%s AND status='ready'",
        (now, board))
    oldest_blocked = _scalar_pg(
        cur,
        "SELECT MAX(%s - t.created_at) FROM tasks t "
        "WHERE t.board=%s AND t.status='blocked' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_links l JOIN tasks p "
        "    ON p.board=l.board AND p.id=l.parent_id "
        "  WHERE l.board=t.board AND l.child_id=t.id "
        "    AND l.relation_type='dependency' "
        "    AND p.status NOT IN ('done','archived'))",
        (now, board))
    oldest_stale_running = _scalar_pg(
        cur,
        "SELECT MAX(%s - COALESCE(last_heartbeat_at, started_at, created_at)) "
        "FROM tasks WHERE board=%s AND status='running'",
        (now, board))
    return Liveness(
        oldest_ready_age_seconds=max(0, oldest_ready),
        oldest_blocked_done_parents_age_seconds=max(0, oldest_blocked),
        oldest_stale_running_age_seconds=max(0, oldest_stale_running))


def evaluate(snap: Liveness, *, thresholds: dict[str, int]) -> list[Breach]:
    """Return the threshold breaches in ``snap``.

    Age dimensions breach when ``value > threshold``. The boolean subsystem
    signals (notifier disabled, writer daemon recovery-exhausted) always breach
    when set — they are binary "this is broken now" conditions owned by this
    gateway. (Dispatch health is covered by the oldest_ready age dimension, not
    a binary flag — see Liveness.)
    """
    breaches: list[Breach] = []
    for dim, limit in thresholds.items():
        value = getattr(snap, dim, None)
        if isinstance(value, bool):
            continue  # booleans handled explicitly below, never as age thresholds
        if isinstance(value, int) and value > int(limit):
            breaches.append(Breach(dim, value, int(limit)))
    if snap.writer_daemon_disabled:
        breaches.append(Breach("writer_daemon_disabled", 1, 0))
    if not snap.notifier_enabled:
        breaches.append(Breach("notifier_disabled", 1, 0))
    return breaches
