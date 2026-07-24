"""Ownership contract between kanban core and out-of-process lane supervisors.

Most kanban workers are direct children of the dispatcher: it knows their pid
because ``spawn_fn`` returned it, and their in-process heartbeat bridge keeps
the claim alive. A *lane supervisor* is the other shape — an out-of-process
babysitter (the local ``claude-kanban-lane`` plugin runs one per tmux/claude-CLI
session) that owns the card on behalf of a session it did not fork from Python.
It has to do three things the in-process path gets for free, and each one has a
way to get it wrong that this module exists to remove:

1. **Heartbeat under the card's identity.** The supervisor runs in the gateway's
   environment, not the worker's, so nothing in ``os.environ`` says which task,
   run or claim it is speaking for. It has to pin
   ``HERMES_KANBAN_TASK`` / ``BOARD`` / ``DB`` / ``RUN_ID`` / ``CLAIM_LOCK``
   explicitly — see :func:`supervisor_shim_env`. Omitting ``RUN_ID`` is the
   dangerous one: ``hermes kanban heartbeat`` then has no
   ``expected_run_id``, so it runs unfenced and a supervisor left over from
   run N keeps renewing the claim of run N+1.

2. **Declare its own pid as the owner.** Nothing else can: the dispatcher
   sees only whatever ``spawn_fn`` returned, and for out-of-process launchers
   that is either nothing or a pid that has already exited. The supervisor
   must pass ``--worker-pid <its own pid>`` (:func:`supervisor_heartbeat_argv`)
   — its own, never a short-lived child's, since a pid that exits immediately
   reads to ``detect_crashed_workers`` as a crashed worker.

3. **Adopt an already-live session safely.** On readopt (the gateway restarted
   while a session kept running) the card still names the dead original
   supervisor. The replacement adopts ownership via
   :func:`adopt_session_ownership`, which is run-fenced and
   compare-and-set: it can take over a dead pid but never a live one, and
   never across runs.

Keeping this here rather than in the plugin means the contract is versioned
and tested next to the core API it depends on; a supervisor script only has
to call these three functions.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db as kb

__all__ = [
    "SUPERVISOR_CONTRACT_ENV_KEYS",
    "OWNERSHIP_HELD_OUTCOMES",
    "supervisor_shim_env",
    "supervisor_heartbeat_argv",
    "adopt_session_ownership",
    "resolve_supervised_run",
]


# Every env var a supervisor-issued lifecycle call needs. Listed so callers
# (and tests) can assert the whole contract rather than the two vars that
# happen to be load-bearing today.
SUPERVISOR_CONTRACT_ENV_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
)

# Adoption outcomes that mean "this pid is the card's recorded owner".
OWNERSHIP_HELD_OUTCOMES = (
    kb._OWNERSHIP_RECORDED,
    kb._OWNERSHIP_REPLACED_DEAD,
    kb._OWNERSHIP_ALREADY_OURS,
)


def supervisor_shim_env(
    base_env: Optional[Mapping[str, str]],
    *,
    task_id: str,
    board: Optional[str],
    run_id: Optional[int] = None,
    claim_lock: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, str]:
    """Env for a supervisor-issued ``hermes kanban`` / shim invocation.

    Returns a copy of ``base_env`` (defaulting to ``os.environ``) with the
    contract pinned. A ``run_id`` / ``claim_lock`` of None is *removed* rather
    than left alone: the supervisor's own environment may carry another
    lane's values (it is a gateway child, and the gateway is the parent of
    every lane), and inheriting those would fence the call to a foreign run.
    Better to fall back to unfenced-but-honest than to fence against the
    wrong run.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["HERMES_KANBAN_TASK"] = str(task_id)
    env["HERMES_KANBAN_BOARD"] = str(board or "default")
    resolved_db = db_path
    if resolved_db is None:
        try:
            resolved_db = str(kb.kanban_db_path(board=board))
        except Exception:
            resolved_db = None
    if resolved_db:
        env["HERMES_KANBAN_DB"] = str(resolved_db)
    else:
        env.pop("HERMES_KANBAN_DB", None)
    if run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(int(run_id))
    else:
        env.pop("HERMES_KANBAN_RUN_ID", None)
    if claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = str(claim_lock)
    else:
        env.pop("HERMES_KANBAN_CLAIM_LOCK", None)
    return env


def supervisor_heartbeat_argv(
    *,
    task_id: str,
    worker_pid: int,
    note: Optional[str] = None,
) -> list[str]:
    """Argv (after the shim/CLI name) for a supervisor heartbeat.

    ``worker_pid`` must be the supervisor's OWN long-lived pid. It is what
    makes the card's ownership record point at a process that outlives the
    heartbeat itself — passing the pid of the heartbeat invocation (or of any
    short-lived child) records an owner that is already dead by the time
    anything reads it.
    """
    pid = int(worker_pid)
    if pid <= 0:
        raise ValueError(f"worker_pid must be a positive pid, got {worker_pid!r}")
    argv = ["heartbeat", str(task_id), "--worker-pid", str(pid)]
    if note:
        argv += ["--note", str(note)]
    return argv


def resolve_supervised_run(
    conn: Any, task_id: str, *, run_id: Optional[int] = None,
) -> Optional[int]:
    """The run this supervisor is speaking for; pin it once, at startup.

    Prefers the run the launcher passed. Falling back to the card's current
    run is safe *only* here at startup, before the supervisor has done
    anything — pinning it once means the supervisor never silently follows
    the card into a successor run. Re-resolving later would defeat the whole
    fence, so callers must not.
    """
    if run_id is not None:
        return int(run_id)
    try:
        card = kb.get_task(conn, task_id)
    except Exception:
        return None
    current = getattr(card, "current_run_id", None) if card else None
    return int(current) if current is not None else None


def adopt_session_ownership(
    conn: Any,
    task_id: str,
    *,
    worker_pid: int,
    expected_run_id: Optional[int],
) -> str:
    """Adopt ownership of an already-live session. Returns the outcome.

    Thin, deliberately-named wrapper over :func:`kanban_db.adopt_worker_pid`
    so a supervisor never reaches for ``_set_worker_pid`` — that one is the
    dispatcher's spawn write, it cannot take over the dead pid the readopt
    path exists to replace, and its private name gives no signal about the
    fencing a supervisor needs.
    """
    return kb.adopt_worker_pid(
        conn, task_id,
        worker_pid=int(worker_pid),
        expected_run_id=expected_run_id,
        via="supervisor_adopt",
    )
