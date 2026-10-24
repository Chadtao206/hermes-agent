"""Create a task-scoped reviewer lane for a durable lifecycle request."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from boris_intake.models import LifecycleRequest, PriorReview
from boris_intake.template import render

PY = "/Users/ctao/.hermes/hermes-agent/venv/bin/python"
REPO = "/Users/ctao/.hermes/hermes-agent"
HERMES_HOME = "/Users/ctao/.hermes"
CREATED_BY = "boris-slack-intake"
KANBAN_DB = "/Users/ctao/.hermes/kanban.db"
# Dispatch priority for Boris review lanes. The kanban dispatcher orders the
# ready queue by priority DESC, created_at ASC; everything else on the board is
# priority 0. A Boris review is short (~5 min) and someone is waiting in Slack
# for it, so it must not sit FIFO behind long-running engineer fix lanes when
# in-flight slots are contended (2026-09-04: three reviews starved 70+ min).
LANE_PRIORITY = 10

#: Statuses that mean a lane is finished. Anything else counts as in flight.
TERMINAL_STATUSES = ("done", "archived", "blocked")


def lane_idempotency_key(request: LifecycleRequest, *, retry: bool = False) -> str:
    key = request.review_identity + ":gate=" + request.gate_snapshot.fingerprint
    return f"{key}:retry={request.request_id}" if retry else key


def active_lane_count(db_path: str = KANBAN_DB) -> int:
    """Lanes this intake created that are still in flight.

    Read from the board rather than a local counter so the number survives a
    daemon restart and reflects reality if a lane is completed by hand. Returns
    0 on any error — the per-user limits remain, and failing closed here would
    wedge intake on a transient DB lock.
    """
    import sqlite3

    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                f"SELECT COUNT(*) FROM tasks WHERE created_by=? "
                f"AND status NOT IN ({placeholders})",
                (CREATED_BY, *TERMINAL_STATUSES),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def create_lane(
    request: LifecycleRequest,
    runner=subprocess.run,
    *,
    retry: bool = False,
    prior: PriorReview | None = None,
) -> str:
    title, body = render(request, prior)
    idempotency_key = lane_idempotency_key(request, retry=retry)
    argv = [
        PY,
        "-m",
        "hermes_cli.main",
        "kanban",
        "create",
        title,
        "--body",
        body,
        "--assignee",
        "boris_on_demand",
        "--created-by",
        CREATED_BY,
        "--priority",
        str(LANE_PRIORITY),
        "--idempotency-key",
        idempotency_key,
        "--json",
    ]
    proc = runner(
        argv,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=120,
        env=dict(os.environ, HERMES_HOME=HERMES_HOME, PYTHONPATH=REPO),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"kanban create rc={proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    try:
        obj = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError(f"kanban create returned non-JSON: {exc}") from exc
    task = obj.get("task", obj) if isinstance(obj, dict) else {}
    lane_id = task.get("id") or (obj.get("id") if isinstance(obj, dict) else None)
    if not lane_id:
        raise RuntimeError(f"kanban create returned no id: {proc.stdout[:200]}")
    return str(lane_id)


class KanbanIO:
    def __init__(self, db_path: Path, runner=subprocess.run):
        self._db_path = Path(db_path)
        self._runner = runner

    def _query(self, sql: str, parameters: tuple) -> list[sqlite3.Row]:
        try:
            connection = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=5
            )
            connection.row_factory = sqlite3.Row
            try:
                return connection.execute(sql, parameters).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise RuntimeError("Kanban lifecycle lookup failed") from error

    def find_lane(
        self, request: LifecycleRequest, *, retry: bool = False
    ) -> str | None:
        rows = self._query(
            "SELECT id FROM tasks WHERE idempotency_key=? ORDER BY id",
            (lane_idempotency_key(request, retry=retry),),
        )
        if len(rows) > 1:
            raise RuntimeError("multiple Kanban lanes have the review idempotency key")
        return str(rows[0]["id"]) if rows else None

    def create_lane(
        self,
        request: LifecycleRequest,
        *,
        retry: bool = False,
        prior: PriorReview | None = None,
    ) -> str:
        return create_lane(request, runner=self._runner, retry=retry, prior=prior)

    def lane_status(self, lane_id: str) -> str | None:
        rows = self._query(
            "SELECT status FROM tasks WHERE id=?",
            (lane_id,),
        )
        if not rows:
            return None
        return str(rows[0]["status"])

    def lane_authentication_failed(self, lane_id: str) -> bool:
        rows = self._query(
            "SELECT outcome, summary FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (lane_id,),
        )
        if not rows:
            return False
        summary = rows[0]["summary"] or ""
        # Fork workers used to block the card themselves with this prefix; upstream workers
        # now exit KANBAN_TERMINAL_PROVIDER_EXIT_CODE and the dispatcher trips the breaker
        # on the crashed run with the text below.
        if rows[0]["outcome"] == "blocked" and summary.startswith("PROVIDER_AUTHENTICATION_REQUIRED:"):
            return True
        return rows[0]["outcome"] in ("crashed", "gave_up") and "terminal provider error" in summary

    def lane_resource_window(self, lane_id: str) -> tuple[float, float | None] | None:
        rows = self._query(
            "SELECT waiting.ended_at, "
            "(SELECT MIN(resumed.started_at) FROM task_runs resumed "
            " WHERE resumed.task_id=waiting.task_id AND resumed.id>waiting.id) AS resumed_at "
            "FROM task_runs waiting WHERE waiting.task_id=? "
            "AND waiting.outcome='resource_wait' AND waiting.ended_at IS NOT NULL "
            "ORDER BY waiting.id DESC LIMIT 1",
            (lane_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return float(row["ended_at"]), (
            float(row["resumed_at"]) if row["resumed_at"] is not None else None
        )

    def lane_quota_hold(self, lane_id: str) -> float | None:
        """``ended_at`` of the lane's latest run the dispatcher booked ``rate_limited``
        (the worker bounced off a provider quota wall and was requeued), else None.
        Later runs are deliberately ignored: a lost exit status books a bounce as a
        crash, and the reconciler bounds the hold by recency itself."""
        rows = self._query(
            "SELECT ended_at FROM task_runs"
            " WHERE task_id=? AND outcome='rate_limited' AND ended_at IS NOT NULL"
            " ORDER BY ended_at DESC, id DESC LIMIT 1",
            (lane_id,),
        )
        return float(rows[0]["ended_at"]) if rows else None

    def retire_lane(self, lane_id: str) -> None:
        """Archive a lane whose request is finished, so it neither keeps bouncing
        off a quota wall nor lands a judgment nobody consumes."""
        argv = [PY, "-m", "hermes_cli.main", "kanban", "archive", lane_id]
        proc = self._runner(
            argv,
            cwd=REPO,
            text=True,
            capture_output=True,
            timeout=120,
            env=dict(os.environ, HERMES_HOME=HERMES_HOME, PYTHONPATH=REPO),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"kanban archive rc={proc.returncode}: {proc.stderr.strip()[:300]}"
            )
