"""Ownership contract between kanban core and out-of-process lane supervisors.

Most kanban workers are direct children of the dispatcher: it knows their pid
because ``spawn_fn`` returned it, and their in-process heartbeat bridge keeps
the claim alive. A *lane supervisor* is the other shape — an out-of-process
babysitter (the local ``claude-kanban-lane`` plugin runs one per tmux/claude-CLI
session) that owns the card on behalf of a session it did not fork from Python.
It has to do three things the in-process path gets for free, and each one has a
way to get it wrong that this module exists to remove:

1. **Heartbeat under the card's identity, fenced to an exact run.** The
   supervisor runs in the gateway's environment, not the worker's, so nothing
   in ``os.environ`` says which task, run or claim it is speaking for. It has
   to pin ``HERMES_KANBAN_TASK`` / ``BOARD`` / ``DB`` / ``RUN_ID`` /
   ``CLAIM_LOCK`` explicitly — see :func:`supervisor_shim_env`. ``RUN_ID`` is
   the load-bearing one and is therefore **required**: without it
   ``hermes kanban heartbeat`` runs with ``expected_run_id=None``, and a
   supervisor left over from run N keeps renewing the claim of run N+1 (it
   still holds a valid task id and, because the dispatcher reuses one
   ``host:pid`` claim lock, usually the very same lock — the run is the only
   thing that can tell the two apart). There is no unfenced fallback here on
   purpose: a supervisor that cannot name its run must not mutate a card at
   all. :func:`pin_supervised_run` is how it establishes that run once, and it
   raises :class:`SupervisorRunUnresolved` rather than returning None.

2. **Declare its own pid as the owner.** Nothing else can: the dispatcher
   sees only whatever ``spawn_fn`` returned, and for out-of-process launchers
   that is either nothing or a pid that has already exited. The supervisor
   must pass ``--worker-pid <its own pid>`` (:func:`supervisor_heartbeat_argv`)
   — its own, never a short-lived child's, since a pid that exits immediately
   reads to ``detect_crashed_workers`` as a crashed worker.

3. **Adopt an already-live session safely.** On readopt (the gateway restarted
   while a session kept running) the card still names the dead original
   supervisor. The replacement adopts ownership via
   :func:`acquire_session_ownership`, which is run-fenced and
   compare-and-set: it can take over a dead pid but never a live one, and
   never across runs. Its three verdicts are the whole decision the caller
   has to make — supervise, stand down, or come back later — and none of them
   is "supervise anyway": an unreadable DB defers, it does not grant
   ownership.

Keeping this here rather than in the plugin means the contract is versioned
and tested next to the core API it depends on; a supervisor script only has
to call these functions and honour the verdict.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_guards as kbg


from hermes_cli import kanban_db_connect as kbc
__all__ = [
    "ADOPTION_ATTEMPTS",
    "ADOPTION_RETRY_SECONDS",
    "OWNERSHIP_HELD",
    "OWNERSHIP_HELD_OUTCOMES",
    "OWNERSHIP_REFUSED",
    "OWNERSHIP_UNVERIFIED",
    "SUPERVISOR_CONTRACT_ENV_KEYS",
    "SupervisorRunUnresolved",
    "acquire_session_ownership",
    "adopt_session_ownership",
    "pin_supervised_run",
    "require_run_id",
    "resolve_supervised_run",
    "supervisor_heartbeat_argv",
    "supervisor_shim_env",
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
    kbg._OWNERSHIP_RECORDED,
    kbg._OWNERSHIP_REPLACED_DEAD,
    kbg._OWNERSHIP_ALREADY_OURS,
)

# The three verdicts :func:`acquire_session_ownership` can return. They are
# deliberately coarser than the ``_OWNERSHIP_*`` outcomes because the caller's
# decision is coarser: supervise, stand down, or defer.
OWNERSHIP_HELD = "held"              # the card records our pid -> supervise
OWNERSHIP_REFUSED = "refused"        # live foreign owner / superseded run
OWNERSHIP_UNVERIFIED = "unverified"  # DB unreadable -> defer, touch nothing

# How hard to try before calling adoption unverified. Transient WAL / disk-I/O
# contention at gateway startup clears in seconds; a longer outage is a
# deferral, not something to wait out holding a pane hostage.
ADOPTION_ATTEMPTS = 5
ADOPTION_RETRY_SECONDS = 2.0


class SupervisorRunUnresolved(ValueError):
    """No exact positive run id could be established for this supervisor.

    Raised instead of returning None because every caller of a run id is a
    fence, and a fence with no value silently stops fencing —
    ``expected_run_id=None`` is precisely how a stale supervisor kept renewing
    its successor's claim.

    ``transient`` says whether the answer might exist and we merely could not
    read it (an unreadable DB) as opposed to there being no run to speak for
    (no such card, or a card with no ``current_run_id``). Both mean "do not
    mutate the card"; only the first is worth retrying later.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = bool(transient)


def require_run_id(run_id: Any) -> int:
    """Coerce ``run_id`` to an exact positive run id, or raise.

    The boundary that makes "fenced" unfalsifiable: everything downstream can
    assume an int it can compare against ``tasks.current_run_id``. ``None``,
    ``0`` and negatives are rejected rather than passed through, because each
    of them turns a fenced statement back into an unfenced one.
    """
    try:
        value = int(run_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise SupervisorRunUnresolved(
            f"not an exact run id: {run_id!r}", transient=False,
        ) from None
    if value <= 0:
        raise SupervisorRunUnresolved(
            f"not a positive run id: {run_id!r}", transient=False,
        )
    return value


def supervisor_shim_env(
    base_env: Optional[Mapping[str, str]],
    *,
    task_id: str,
    board: Optional[str],
    run_id: Any,
    claim_lock: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, str]:
    """Env for a supervisor-issued ``hermes kanban`` / shim invocation.

    Returns a copy of ``base_env`` (defaulting to ``os.environ``) with the
    contract pinned.

    ``run_id`` is required and must be an exact positive run — this function
    is the only way a supervisor addresses a card, so refusing here is what
    makes "no unfenced supervisor lifecycle call" a property of the code
    rather than a convention. A supervisor that cannot resolve its run must
    exit, not call this with None.

    ``claim_lock`` of None is *removed* rather than left alone: the
    supervisor's own environment may carry another lane's lock (it is a
    gateway child, and the gateway is the parent of every lane), and
    inheriting that would address someone else's claim. Any inherited
    ``HERMES_KANBAN_RUN_ID`` is likewise overwritten by ours, never trusted.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["HERMES_KANBAN_TASK"] = str(task_id)
    env["HERMES_KANBAN_BOARD"] = str(board or "default")
    env["HERMES_KANBAN_RUN_ID"] = str(require_run_id(run_id))
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
) -> int:
    """The exact run this supervisor speaks for; pin it once, at startup.

    Prefers the run the launcher passed. Falling back to the card's current
    run is safe *only* here at startup, before the supervisor has done
    anything — pinning it once means the supervisor never silently follows
    the card into a successor run. Re-resolving later would defeat the whole
    fence, so callers must not.

    Raises :class:`SupervisorRunUnresolved` when there is nothing exact to
    pin: an unreadable card (transient), an absent card, or a card with no
    ``current_run_id``. Returning None here is what let the old fallback run
    a supervisor unfenced.
    """
    if run_id is not None:
        return require_run_id(run_id)
    try:
        card = kb.get_task(conn, task_id)
    except Exception as exc:
        raise SupervisorRunUnresolved(
            f"{task_id}: run lookup failed ({exc.__class__.__name__}: {exc})",
            transient=True,
        ) from exc
    if card is None:
        raise SupervisorRunUnresolved(
            f"{task_id}: no such card", transient=False,
        )
    current = getattr(card, "current_run_id", None)
    if current is None:
        raise SupervisorRunUnresolved(
            f"{task_id}: card records no current run", transient=False,
        )
    return require_run_id(current)


def pin_supervised_run(
    task_id: str,
    *,
    board: Optional[str],
    run_id: Optional[int] = None,
) -> int:
    """:func:`resolve_supervised_run` including the connection it needs.

    A supervisor's startup DB failure and its card-lookup failure are the same
    event as far as fencing goes, so both are classified here rather than in
    each caller — the host script's ``except Exception: run_id = None`` is
    exactly the fail-open this replaces.
    """
    if run_id is not None:
        return require_run_id(run_id)
    try:
        with kbc.connect_closing(board=board) as conn:
            return resolve_supervised_run(conn, task_id)
    except SupervisorRunUnresolved:
        raise
    except Exception as exc:
        raise SupervisorRunUnresolved(
            f"{task_id}: board {board!r} unavailable "
            f"({exc.__class__.__name__}: {exc})",
            transient=True,
        ) from exc


def adopt_session_ownership(
    conn: Any,
    task_id: str,
    *,
    worker_pid: int,
    expected_run_id: Any,
) -> str:
    """Adopt ownership of an already-live session. Returns the outcome.

    Thin, deliberately-named wrapper over :func:`kanban_db.adopt_worker_pid`
    so a supervisor never reaches for ``_set_worker_pid`` — that one is the
    dispatcher's spawn write, it cannot take over the dead pid the readopt
    path exists to replace, and its private name gives no signal about the
    fencing a supervisor needs. ``expected_run_id`` is required and validated:
    an adoption that is not run-fenced is how a leftover supervisor stamped
    itself onto a successor run.
    """
    return kbg.adopt_worker_pid(
        conn, task_id,
        worker_pid=int(worker_pid),
        expected_run_id=require_run_id(expected_run_id),
        via="supervisor_adopt",
    )


def acquire_session_ownership(
    task_id: str,
    *,
    board: Optional[str],
    worker_pid: int,
    expected_run_id: Any,
    attempts: int = ADOPTION_ATTEMPTS,
    sleep_fn: Optional[Callable[[float], Any]] = None,
) -> tuple[str, Optional[str]]:
    """Run-fenced adoption with retries. Returns ``(verdict, outcome)``.

    The verdict is the caller's whole decision:

    * :data:`OWNERSHIP_HELD` — the card records our pid; supervise it.
    * :data:`OWNERSHIP_REFUSED` — the DB answered and the answer was no (a
      live foreign owner, or our run has been superseded). Stand down without
      touching the pane: it may well belong to the supervisor that DID win,
      and a process that has just learned it is not the owner is the last one
      that should be killing sessions.
    * :data:`OWNERSHIP_UNVERIFIED` — the DB never became readable, so
      ownership is unknown. Also stand down, also without touching the pane,
      and let the next gateway startup retry. Unknown is NOT ours: treating it
      as ours (the old ``assume_ours`` fallback) permanently disabled the
      foreign-pid check, so two supervisors could run one card with nothing
      but a silent heartbeat conflict to show for it.

    Retries cover transient WAL / disk-I/O contention, which is common while
    the gateway is starting everything at once. A refusal is not retried —
    re-reading a live owner just returns the same answer.
    """
    run_id = require_run_id(expected_run_id)
    pid = int(worker_pid)
    if pid <= 0:
        raise ValueError(f"worker_pid must be a positive pid, got {worker_pid!r}")
    sleep = time.sleep if sleep_fn is None else sleep_fn
    tries = max(1, int(attempts))
    for attempt in range(tries):
        try:
            with kbc.connect_closing(board=board) as conn:
                outcome = adopt_session_ownership(
                    conn, task_id, worker_pid=pid, expected_run_id=run_id,
                )
        except Exception:
            if attempt + 1 < tries:
                sleep(ADOPTION_RETRY_SECONDS)
            continue
        if outcome in OWNERSHIP_HELD_OUTCOMES:
            return (OWNERSHIP_HELD, outcome)
        return (OWNERSHIP_REFUSED, outcome)
    return (OWNERSHIP_UNVERIFIED, None)
