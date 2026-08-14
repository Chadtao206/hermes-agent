"""Create the reviewer lane. The assignee is the ROLE KEY `reviewer`, never `boris`."""
from __future__ import annotations

import json
import os
import subprocess

from boris_intake.models import ReviewCommand
from boris_intake.template import render

PY = "/Users/ctao/.hermes/hermes-agent/venv/bin/python"
REPO = "/Users/ctao/.hermes/hermes-agent"
HERMES_HOME = "/Users/ctao/.hermes"
CREATED_BY = "boris-slack-intake"
KANBAN_DB = "/Users/ctao/.hermes/kanban.db"

#: Statuses that mean a lane is finished. Anything else counts as in flight.
TERMINAL_STATUSES = ("done", "archived")


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


def create_lane(cmd: ReviewCommand, runner=subprocess.run) -> str:
    title, body = render(cmd)
    argv = [
        PY, "-m", "hermes_cli.main", "kanban", "create", title,
        "--body", body,
        "--assignee", "reviewer",
        "--created-by", CREATED_BY,
        "--idempotency-key", cmd.idempotency_key,
        "--json",
    ]
    proc = runner(
        argv, cwd=REPO, text=True, capture_output=True, timeout=120,
        env=dict(os.environ, HERMES_HOME=HERMES_HOME, PYTHONPATH=REPO),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kanban create rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        obj = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError(f"kanban create returned non-JSON: {exc}") from exc
    task = obj.get("task", obj) if isinstance(obj, dict) else {}
    lane_id = task.get("id") or (obj.get("id") if isinstance(obj, dict) else None)
    if not lane_id:
        raise RuntimeError(f"kanban create returned no id: {proc.stdout[:200]}")
    return str(lane_id)
