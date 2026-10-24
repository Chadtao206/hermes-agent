"""Worker-liveness fencing and dispatch guards for the kanban dispatcher (Ylopo fork).

This sibling module holds the fork's kanban customizations that survived the
2026-09 upstream sync, kept out of the upstream ``kanban_db*`` siblings so a
future merge only has to reconcile the four one-line hook points that call in:

* **Heartbeat renews the claim TTL** (:func:`heartbeat_worker`) and a stale-by-TTL
  claim with a fresh heartbeat but no recorded pid is held for a grace window
  instead of being reclaimed mid-flight (:func:`stale_claim_hold`).
* **Compare-and-set worker-pid ownership** (:func:`_record_spawn_ownership`,
  :func:`adopt_worker_pid`): first valid owner of a run wins, a live foreign pid
  is a ``spawn_ownership_conflict``, a missing/late pid is recorded as
  ``spawn_ownership_unrecorded`` instead of silently dropped. Out-of-process lane
  supervisors (``hermes_cli.kanban_supervisor`` / the claude-kanban-lane plugin)
  depend on this contract.
* **Respawn-guard semantics** (:func:`check_respawn_guard`): only *deliberate*
  re-queues release the ``active_pr`` guard, a never-run card is not guarded by
  the PR it was created to remediate, PR comments count only from profiles that
  ran the task, and unchanged ``respawn_guarded`` events are throttled
  (:func:`_should_log_respawn_guard`) instead of one row per 10-second tick.
* **Low-disk watermark** (:func:`_ensure_workspace_capacity`): refuse to
  materialize a new workspace when the filesystem is below
  ``kanban.min_free_disk_gb``.

Module-cycle convention follows the upstream siblings: the origin module and the
other siblings are imported LAST as late-bound namespaces.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-disk watermark for new workspaces
# ---------------------------------------------------------------------------

DEFAULT_MIN_FREE_DISK_GB = 5.0


class WorkspaceDiskSpaceError(RuntimeError):
    """Raised before materializing a workspace below the free-disk floor."""

    def __init__(self, message: str, *, path: Path | None = None):
        super().__init__(message)
        self.path = path
        self.board_workspace: tuple[str, str] | None = None


def defer_workspace_capacity(conn: sqlite3.Connection, task, error: WorkspaceDiskSpaceError) -> bool:
    with _kb.write_txn(conn):
        retry_status = _kb._retry_status_for_run(conn, task.id, task.current_run_id)
        changed = conn.execute(
            "UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND status='running' AND current_run_id=? AND claim_lock IS ? "
            "AND worker_pid IS NULL",
            (retry_status, task.id, task.current_run_id, task.claim_lock),
        )
        if changed.rowcount != 1:
            return False
        payload = {
            "reason": "workspace_disk_capacity", "retry_status": retry_status,
            "capacity_path": str(error.path) if error.path is not None else None,
            "workspace_kind": task.workspace_kind, "workspace_path": task.workspace_path,
            "board_workspace": error.board_workspace,
        }
        run_id = _kb._end_run(
            conn, task.id, outcome="resource_wait", error=str(error)[:500], metadata=payload,
        )
        _kb._append_event(conn, task.id, "resource_wait", payload, run_id=run_id)
    return True


def _configured_min_free_disk_gb() -> float:
    """Return the configured workspace creation floor in GiB."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        raw = (cfg.get("kanban", {}) or {}).get("min_free_disk_gb", DEFAULT_MIN_FREE_DISK_GB)
        value = float(raw)
        if value < 0:
            raise ValueError("must be non-negative")
        return value
    except Exception as exc:
        _log.warning(
            "Invalid kanban.min_free_disk_gb; using %.1f GiB default: %s", DEFAULT_MIN_FREE_DISK_GB, exc,
        )
        return DEFAULT_MIN_FREE_DISK_GB


def _ensure_workspace_capacity(path: Path) -> None:
    """Reject a new workspace when its filesystem is below the disk floor.

    Existing workspaces are never affected, so in-flight tasks are not stranded.
    """
    if path.exists():
        return
    minimum_gb = _configured_min_free_disk_gb()
    if minimum_gb <= 0:
        return
    probe = _kbw._nearest_existing_path(path.parent)
    usage = shutil.disk_usage(probe)
    gib = 1024 ** 3
    if usage.free >= minimum_gb * gib:
        return
    free_gb = usage.free / gib
    raise WorkspaceDiskSpaceError(
        f"refusing to create workspace {path}: {free_gb:.1f} GiB free; "
        f"kanban.min_free_disk_gb requires at least {minimum_gb:g} GiB. "
        "Run `hermes kanban gc`, remove unneeded data, or lower the configured watermark.",
        path=path,
    )


# ---------------------------------------------------------------------------
# Respawn guard: deliberate re-runs, runner-scoped PR comments, throttled events
# ---------------------------------------------------------------------------

_RESPAWN_REQUEUE_EVENT_KINDS = ("status", "promoted", "promoted_manual", "unblocked", "reclaimed")
# A handoff after the newest PR comment names the profile that must now work on THAT PR
# (upstream #111910): it lifts ``active_pr`` only, never ``recent_success``.
_RESPAWN_HANDOFF_EVENT_KINDS = ("assigned", "changes_requested", "review_reopened")

# ``status`` payload reasons written by an automatic cascade rather than by an
# operator/API transition. Anything else on a ``status`` event came from somebody
# deciding this card's status.
_AUTOMATIC_STATUS_REASONS = frozenset({"parent_reopened"})

# How often an UNCHANGED respawn_guarded event may repeat for one task. The
# dispatcher ticks every 10 seconds on this install and evaluated the guard —
# and wrote an event — on every tick, so a single held card produced hundreds of
# byte-identical rows (836 on one card in 15 minutes). A *changed* reason is
# emitted immediately so every state transition stays visible;
# ``DispatchResult.respawn_guarded`` is still populated on every tick.
_RESPAWN_GUARD_EVENT_REPEAT_SECONDS = 600  # 10 minutes


def _is_deliberate_requeue(kind: str, payload_raw: Optional[str]) -> bool:
    """True when this event is somebody *deciding* to run the task again.

    Automatic transitions must not count: a TTL reclaim, a crash reclaim and a
    dependency/retry promotion all move a card back toward ``ready`` without
    anyone having looked at it, so reading them as "run it again" switches off
    the duplicate-PR guard on every automatic reclaim.
    """
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if kind == "reclaimed":
        # ``reclaim_task`` (the operator path) stamps ``manual``; the TTL and
        # crash paths stamp ``stale_lock`` / ``prev_pid`` and no ``manual``.
        return payload.get("manual") is True
    if kind == "promoted":
        # ``recompute_ready``'s automatic promotion; the operator's deliberate
        # promote writes ``promoted_manual`` instead.
        return False
    if kind == "status":
        return payload.get("reason") not in _AUTOMATIC_STATUS_REASONS
    if kind == "assigned":
        # Only a move to a DIFFERENT profile is a handoff; a no-op re-assign, an unassign
        # or the dispatcher's own default-assignee write would lift the guard for the
        # very implementer that opened the PR. No ``from`` recorded → fail closed.
        if payload.get("source") == "kanban.default_assignee":
            return False
        to = payload.get("assignee")
        return bool(to) and "from" in payload and payload["from"] != to
    return True  # promoted_manual, unblocked, changes_requested, review_reopened: operator paths


def _payload_acknowledges_pr(payload_raw: Optional[str], pr_urls: tuple[str, ...]) -> bool:
    """True when a re-queue event's payload names one of ``pr_urls`` — the
    same-second tie-break: an operator who wrote the PR URL into the re-queue
    has demonstrably seen that PR."""
    if not pr_urls or not payload_raw:
        return False
    text = payload_raw.lower()
    return any(url in text for url in pr_urls)


def _deliberate_rerun_since(
    conn: sqlite3.Connection,
    task_id: str,
    since: int,
    *,
    strict: bool = False,
    ack_pr_urls: tuple[str, ...] = (),
    include_handoffs: bool = False,
) -> bool:
    """True when a *deliberate* re-queue event landed after ``since``.

    ``strict`` decides the same-second case (``created_at`` has one-second
    resolution): the ``active_pr`` guard sets it so a PR comment and a re-queue
    stamped in the same second always resolve the same way — the comment wins
    and the guard stays armed — unless the re-queue names the PR
    (``ack_pr_urls``). ``recent_success`` keeps the inclusive reading because
    its boundary is the task's own run ending, not a competing external write.
    """
    kinds = _RESPAWN_REQUEUE_EVENT_KINDS + (_RESPAWN_HANDOFF_EVENT_KINDS if include_handoffs else ())
    # SQLite sorts TEXT above every INTEGER, so the range also returns text stamps written
    # outside the mutators; each row is decided on its parsed epoch (unreadable → ignored).
    rows = conn.execute(
        "SELECT kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND created_at >= ? "
        f"AND kind IN ({', '.join('?' * len(kinds))}) "
        "ORDER BY created_at, id",
        (task_id, int(since), *kinds),
    ).fetchall()
    for row in rows:
        if not _is_deliberate_requeue(row["kind"], row["payload"]):
            continue
        created_at = _kb._to_epoch(row["created_at"])
        if created_at is None or created_at < int(since):
            continue
        if not strict or created_at > int(since):
            return True
        if _payload_acknowledges_pr(row["payload"], ack_pr_urls):
            return True
    return False


def _should_log_respawn_guard(conn: sqlite3.Connection, task_id: str, reason: str, now: int) -> bool:
    """Throttle repeated, unchanged ``respawn_guarded`` events for one task.

    True when the event should be written: no prior guard event, a different
    reason than last time, or the previous identical event is older than
    ``_RESPAWN_GUARD_EVENT_REPEAT_SECONDS``.
    """
    row = conn.execute(
        # created_at first so ``idx_events_task(task_id, created_at)`` serves the
        # ordering in reverse — this runs on every guarded task on every tick.
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'respawn_guarded' "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return True
    try:
        prev_reason = (json.loads(row["payload"] or "{}") or {}).get("reason")
    except (TypeError, ValueError):
        prev_reason = None
    if prev_reason != reason:
        return True
    created_at = _kb._to_epoch(row["created_at"])
    return created_at is None or (now - created_at) >= _RESPAWN_GUARD_EVENT_REPEAT_SECONDS


def check_respawn_guard(conn: sqlite3.Connection, task_id: str, *, lane: str = "ready") -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Same contract and priority order as upstream's guard (``rate_limit_cooldown``,
    ``blocker_auth``, then for the ready lane ``recent_success`` and
    ``active_pr``; the review lane skips the last two because a recent run and a
    fresh PR URL are the *inputs* to a review handoff). The fork differs on
    ``active_pr``:

    * it only arms after the task has run at least once — a never-run
      remediation card is not guarded by the PR it was created to fix;
    * PR comments count only from profiles that ran the task, so reviewer/cron
      context does not re-arm a guard a deliberate requeue released;
    * only *deliberate* re-queues release it (:func:`_is_deliberate_requeue`),
      keyed off the NEWEST matching comment, with the strict same-second rule
      of :func:`_deliberate_rerun_since`. Without any bypass the guard was
      permanent for 24 hours and an explicitly requeued card was refused on
      every tick (live incident ``t_b14df276``).
    """
    row = conn.execute(
        "SELECT last_failure_error, workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown (latest run only; checked BEFORE blocker_auth because
    #    the requeue stamps a quota-flavored last_failure_error).
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at, metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "resource_wait":
        wait = _kb._json_dict(latest_run["metadata"])
        path = wait.get("capacity_path")
        board_workspace = wait.get("board_workspace")
        board_unchanged = board_workspace is None or (
            isinstance(board_workspace, list) and len(board_workspace) == 2
            and (_kb.read_board_metadata(board_workspace[0]).get("default_workdir") or "").strip()
            == board_workspace[1]
        )
        if (
            isinstance(path, str) and path
            and board_unchanged
            and wait.get("workspace_kind") == row["workspace_kind"]
            and wait.get("workspace_path") == row["workspace_path"]
        ):
            try:
                _ensure_workspace_capacity(Path(path))
            except WorkspaceDiskSpaceError:
                return "workspace_disk_capacity"
            except OSError:
                return "workspace_capacity_unavailable"
    if latest_run is not None and latest_run["outcome"] == "spawn_failed":
        # An infrastructure spawn refusal (upstream #114720) shares the cooldown: the host
        # condition is not the card's, so it retries forever, spaced, and never reaches the breaker.
        if rl_cooldown > 0 and _kb._json_dict(latest_run["metadata"]).get("infrastructure"):
            ended_at = _kb._to_epoch(latest_run["ended_at"])
            if ended_at is not None and (now - ended_at) < rl_cooldown:
                return "infrastructure_cooldown"
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            return None
        ended_at = _kb._to_epoch(latest_run["ended_at"])
        if ended_at is not None and (now - ended_at) < rl_cooldown:
            return "rate_limit_cooldown"
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.
    # A plain crash is different: its persisted error includes the worker's last captured
    # output, which is context rather than a diagnosis and may contain benign commands such
    # as ``claude auth status`` (upstream #117097).
    err = _kb._lossy_text(row["last_failure_error"])
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    if err and latest_outcome != "crashed" and _kbd._RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    if lane == "review":
        return None

    # 3. Completed run within the guard window, unless deliberately re-queued after it.
    cutoff = now - _kbd._RESPAWN_GUARD_SUCCESS_WINDOW
    # SQLite sorts TEXT above every INTEGER, so the range also returns ISO stamps written
    # outside the mutators; each row is decided on its parsed epoch (unreadable → ignored).
    completed_at = max(
        (t for t in (_kb._to_epoch(r["ended_at"]) for r in conn.execute(
            "SELECT ended_at FROM task_runs WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ?",
            (task_id, cutoff),
        )) if t is not None and t >= cutoff),
        default=None,
    )
    if completed_at is not None and not _deliberate_rerun_since(conn, task_id, completed_at):
        return "recent_success"

    # 4. GitHub PR URL in a recent comment by a profile that ran the task.
    prior_run = conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? LIMIT 1", (task_id,)).fetchone()
    if prior_run is None:
        return None
    pr_cutoff = now - _kbd._RESPAWN_GUARD_PR_WINDOW
    newest_pr_comment_at: Optional[int] = None
    newest_pr_urls: tuple[str, ...] = ()
    for c in conn.execute(
        "SELECT body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? "
        "AND author IN (SELECT DISTINCT profile FROM task_runs WHERE task_id = ? AND profile IS NOT NULL)",
        (task_id, pr_cutoff, task_id),
    ).fetchall():
        body = _kb._lossy_text(c["body"]) or ""
        urls = _kbd._RESPAWN_GUARD_PR_URL_RE.findall(body)
        if not urls:
            continue
        # Duplicate-PR risk: an unreadable stamp fails closed as "just now".
        at = _kb._to_epoch(c["created_at"])
        at = now if at is None else at
        if at < pr_cutoff:
            continue
        if newest_pr_comment_at is None or at > newest_pr_comment_at:
            newest_pr_comment_at = at
            newest_pr_urls = tuple(u.lower() for u in urls)
    if newest_pr_comment_at is not None and not _deliberate_rerun_since(
        conn, task_id, newest_pr_comment_at, strict=True, ack_pr_urls=newest_pr_urls,
        include_handoffs=True,
    ):
        return "active_pr"

    return None


# ---------------------------------------------------------------------------
# Heartbeat renews the claim TTL; stale-claim hold on heartbeat freshness
# ---------------------------------------------------------------------------

# Renews a claim TTL from a heartbeat without ever *shortening* it and without
# inventing an expiry where there was none: ``MAX(claim_expires, ?)`` keeps a
# deliberately longer claim intact, and the ``IS NULL`` arm preserves the
# "runs without a TTL" state (``release_stale_claims`` only considers rows with
# ``claim_expires IS NOT NULL``).
_HEARTBEAT_CLAIM_RENEWAL_SQL = (
    "claim_expires = CASE WHEN claim_expires IS NULL THEN NULL ELSE MAX(claim_expires, ?) END"
)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    ttl_seconds: Optional[int] = None,
    worker_pid: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event, touch ``last_heartbeat_at`` and renew the
    active claim TTL — all in one run-fenced transaction.

    A heartbeat is the strongest evidence that a worker is alive, so a
    heartbeating worker must never be reclaimed for outliving the TTL it was
    claimed under (a worker heartbeating every 2 minutes was reclaimed the moment
    its original 15-minute window lapsed — run 1514). Doing the renewal here makes
    every heartbeat path (tool, CLI, supervisor) consistent and keeps
    ``last_heartbeat_at`` and ``claim_expires`` from ever disagreeing. The
    ``status = 'running'`` predicate and ``expected_run_id`` reject heartbeats from
    a superseded run, so a stale worker can never extend its replacement's claim.

    ``worker_pid`` lets the caller adopt ownership from the other side of the
    spawn→heartbeat race (see :func:`_record_worker_pid`). Adoption never steals a
    live foreign pid; pass it only from a process that genuinely is the worker
    or its supervisor.

    Returns True on success, False if the task is not running or the run has been
    superseded.
    """
    now = int(time.time())
    renewed = now + _kb._resolve_claim_ttl_seconds(ttl_seconds)
    with _kbc.write_txn(conn):
        sql = f"UPDATE tasks SET last_heartbeat_at = ?, {_HEARTBEAT_CLAIM_RENEWAL_SQL} WHERE id = ? AND status = 'running'"
        params: tuple = (now, renewed, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = int(expected_run_id) if expected_run_id is not None else _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                f"UPDATE task_runs SET last_heartbeat_at = ?, {_HEARTBEAT_CLAIM_RENEWAL_SQL} WHERE id = ?",
                (now, renewed, run_id),
            )
        adopted, replaced = _adopt_worker_pid_locked(
            conn, task_id, worker_pid, run_id=run_id, expected_run_id=expected_run_id,
        )
        _kb._append_event(conn, task_id, "heartbeat", {"note": note} if note else None, run_id=run_id)
        if adopted is not None:
            payload: dict[str, Any] = {"worker_pid": adopted, "via": "heartbeat"}
            if replaced is not None:
                payload["replaced_dead_pid"] = replaced
            _kb._append_event(conn, task_id, "ownership_adopted", payload, run_id=run_id)
    return True


@dataclass(frozen=True)
class StaleClaimHold:
    """Decision to extend (not reclaim) a TTL-expired claim."""

    new_expires: int
    reason: str  # "pid_alive" | "heartbeat_fresh"
    heartbeat_age: Optional[int]


def heartbeat_age_seconds(row: sqlite3.Row, now: int) -> Optional[int]:
    hb = _kb._to_epoch(row["last_heartbeat_at"])
    return None if hb is None else now - hb


def stale_claim_hold(
    row: sqlite3.Row, now: int, *, claim_ttl: int, host_local: bool, heartbeat_stale: bool,
    pid_alive: Optional[bool] = None,
) -> Optional[StaleClaimHold]:
    """Decide whether ``release_stale_claims`` should hold an expired claim.

    A live host-local pid earns a full window (upstream behaviour). The fork adds
    the pid-less case: a claim whose heartbeat is fresher than one claim window is
    held for ``RECLAIM_DEFER_GRACE_SECONDS`` and re-tested every tick — a heartbeat
    proves the worker was alive as of the last beat, not now. Launcher paths that
    supervise the worker out of process can leave ``worker_pid`` unset, and such a
    card was reclaimed 42 seconds after its last heartbeat, mid-flight, while its
    supervisor was still running (run 1514). The claim-lock host prefix, not the
    pid, is what makes a claim host-local. Returns None when the claim should go
    through the normal terminate/reclaim path.
    """
    if not host_local or heartbeat_stale:
        return None
    hb_age = heartbeat_age_seconds(row, now)
    if pid_alive is None:
        pid_alive = bool(row["worker_pid"]) and _kbd._pid_alive(row["worker_pid"])
    heartbeat_fresh = hb_age is not None and hb_age < claim_ttl
    if pid_alive:
        return StaleClaimHold(now + claim_ttl, "pid_alive", hb_age)
    if heartbeat_fresh:
        return StaleClaimHold(now + _kb.RECLAIM_DEFER_GRACE_SECONDS, "heartbeat_fresh", hb_age)
    return None


# ---------------------------------------------------------------------------
# Worker-pid ownership (compare-and-set)
# ---------------------------------------------------------------------------
#
# Ownership of a run is compare-and-set, not last-write-wins. Two paths race for
# it by construction — the dispatcher writes the pid its ``spawn_fn`` returned,
# and a long-lived supervisor adopts its own pid on its first heartbeat, which
# can land *before* that spawn call has returned. Both converge on ONE owner per
# run with exactly one ownership event: whichever arrives first wins and the
# loser is a no-op (plus, on the spawn path, a conflict diagnostic).
_OWNERSHIP_RECORDED = "recorded"            # we now own it; nobody did before
_OWNERSHIP_REPLACED_DEAD = "replaced_dead"  # took over from a dead owner
_OWNERSHIP_ALREADY_OURS = "already_ours"    # this pid is already the owner
_OWNERSHIP_CONFLICT = "conflict"            # a different owner holds the card
_OWNERSHIP_FENCED = "fenced"                # not running / run superseded
_OWNERSHIP_INVALID = "invalid"              # no usable pid offered


def _cas_worker_pid_locked(
    conn: sqlite3.Connection,
    task_id: str,
    pid: Optional[int],
    *,
    run_id: Optional[int],
    expected_run_id: Optional[int] = None,
    allow_dead_takeover: bool = False,
) -> tuple[str, Optional[int]]:
    """Compare-and-set ``tasks.worker_pid``; return ``(outcome, prior_pid)``.

    Must be called inside an open write transaction. ``allow_dead_takeover``
    distinguishes the callers: adoption by a live supervisor may take over a
    *dead* recorded pid (the readopt path), the dispatcher's spawn write may not
    (a spawn is no evidence about the recorded owner — a card owned only by a dead
    pid is ``detect_crashed_workers``' business). Neither may take over from a pid
    that is still alive: that is what let two workers run the same card (run 1469).
    """
    try:
        pid_i = int(pid) if pid else None
    except (TypeError, ValueError):
        pid_i = None
    if pid_i is None or pid_i <= 0:
        return (_OWNERSHIP_INVALID, None)
    row = conn.execute(
        "SELECT status, current_run_id, worker_pid FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or row["status"] != "running":
        return (_OWNERSHIP_FENCED, None)
    if expected_run_id is not None and (
        row["current_run_id"] is None or int(row["current_run_id"]) != int(expected_run_id)
    ):
        return (_OWNERSHIP_FENCED, None)
    current = None if row["worker_pid"] is None else int(row["worker_pid"])
    if current == pid_i:
        return (_OWNERSHIP_ALREADY_OURS, current)
    if current is not None and not (allow_dead_takeover and not _kbd._pid_alive(current)):
        return (_OWNERSHIP_CONFLICT, current)
    # Upstream's restart-stable fingerprint rides along with the pid so ``_worker_alive`` can tell
    # this worker from a process that recycled the number after a reboot.
    started_at = _kbd._process_fingerprint(pid_i) or _kbd.UNVERIFIED_WORKER_FINGERPRINT
    # ``IS`` rather than ``=`` so the NULL case (no prior owner) uses the same predicate.
    sql = ("UPDATE tasks SET worker_pid = ?, worker_started_at = ? "
           "WHERE id = ? AND status = 'running' AND worker_pid IS ?")
    params: list[Any] = [pid_i, started_at, task_id, current]
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params.append(int(expected_run_id))
    if conn.execute(sql, params).rowcount != 1:
        return (_OWNERSHIP_CONFLICT, current)
    if run_id is not None:
        conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                     (pid_i, started_at, run_id))
    return (_OWNERSHIP_REPLACED_DEAD if current is not None else _OWNERSHIP_RECORDED, current)


def _adopt_worker_pid_locked(
    conn: sqlite3.Connection,
    task_id: str,
    worker_pid: Optional[int],
    *,
    run_id: Optional[int],
    expected_run_id: Optional[int] = None,
) -> tuple[Optional[int], Optional[int]]:
    """Adopt ``worker_pid`` as this card's owner if nothing live claims it.

    Returns ``(adopted_pid, replaced_dead_pid)`` — both None when no write
    happened, so the caller emits exactly one event per actual change and a
    2-minute heartbeat cadence never becomes an event flood. A conflict is
    deliberately silent here: the spawn path reports it once, a heartbeat would
    report it forever.
    """
    outcome, prior = _cas_worker_pid_locked(
        conn, task_id, worker_pid, run_id=run_id, expected_run_id=expected_run_id, allow_dead_takeover=True,
    )
    if outcome == _OWNERSHIP_RECORDED:
        return (int(worker_pid), None)  # type: ignore[arg-type]
    if outcome == _OWNERSHIP_REPLACED_DEAD:
        return (int(worker_pid), prior)  # type: ignore[arg-type]
    return (None, None)


def adopt_worker_pid(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    worker_pid: int,
    expected_run_id: Optional[int] = None,
    via: str = "supervisor",
) -> str:
    """Public ownership-adoption entry point for out-of-process supervisors.

    A lane supervisor that takes over an already-live worker session has to name
    itself as the card's owner before its first ownership check, or the stale dead
    pid still on the card reads as a foreign owner and it kills the very session it
    was launched to keep. The write is fenced to ``expected_run_id`` so a
    supervisor left over from run N cannot stamp itself onto run N+1.

    Returns one of the ``_OWNERSHIP_*`` outcomes; only ``recorded`` /
    ``replaced_dead`` / ``already_ours`` mean "this pid owns the card".
    """
    with _kbc.write_txn(conn):
        run_id = int(expected_run_id) if expected_run_id is not None else _kb._current_run_id(conn, task_id)
        outcome, prior = _cas_worker_pid_locked(
            conn, task_id, worker_pid, run_id=run_id, expected_run_id=expected_run_id, allow_dead_takeover=True,
        )
        if outcome in (_OWNERSHIP_RECORDED, _OWNERSHIP_REPLACED_DEAD):
            payload: dict[str, Any] = {"worker_pid": int(worker_pid), "via": via}
            if prior is not None:
                payload["replaced_dead_pid"] = prior
            _kb._append_event(conn, task_id, "ownership_adopted", payload, run_id=run_id)
    return outcome


def _record_worker_pid(
    conn: sqlite3.Connection, task_id: str, pid: int, *, expected_run_id: Optional[int] = None,
) -> str:
    """Compare-and-set the spawned child's pid + emit a ``spawned`` event.

    The dispatcher's pid write necessarily lands *after* the child is alive; in
    that window the child can already have heartbeated (adopting its own pid), or
    — if the claim lapsed — been reclaimed and re-claimed under a new run. Writing
    unconditionally stamped the old child's pid onto the new run; ``expected_run_id``
    fences the write to the run actually spawned for, and the CAS refuses to
    replace an owner already recorded within that run. First valid owner wins.
    Unlike :func:`adopt_worker_pid` this never takes over from a *dead* recorded
    pid. Returns one of the ``_OWNERSHIP_*`` outcomes.
    """
    with _kbc.write_txn(conn):
        run_id = int(expected_run_id) if expected_run_id is not None else _kb._current_run_id(conn, task_id)
        outcome, prior = _cas_worker_pid_locked(
            conn, task_id, pid, run_id=run_id, expected_run_id=expected_run_id, allow_dead_takeover=False,
        )
        if outcome == _OWNERSHIP_CONFLICT:
            _kb._append_event(
                conn, task_id, "spawn_ownership_conflict",
                {
                    "pid": int(pid),
                    "recorded_pid": prior,
                    "recorded_alive": bool(prior is not None and _kbd._pid_alive(prior)),
                },
                run_id=run_id,
            )
        elif outcome in (_OWNERSHIP_RECORDED, _OWNERSHIP_ALREADY_OURS):
            # ``spawned`` records the spawn either way; the marker distinguishes
            # "we wrote the pid" from "the worker had already adopted it".
            payload: dict[str, Any] = {"pid": int(pid)}
            if outcome == _OWNERSHIP_ALREADY_OURS:
                payload["ownership"] = "already_recorded"
            _kb._append_event(conn, task_id, "spawned", payload, run_id=run_id)
    return outcome


def _set_worker_pid(
    conn: sqlite3.Connection, task_id: str, pid: int, *, expected_run_id: Optional[int] = None,
) -> bool:
    """Boolean façade over :func:`_record_worker_pid`: True iff this pid is the
    card's recorded owner (written now, or already adopted by the same process)."""
    return _record_worker_pid(conn, task_id, pid, expected_run_id=expected_run_id) in (
        _OWNERSHIP_RECORDED, _OWNERSHIP_ALREADY_OURS,
    )


def _record_spawn_ownership(
    conn: sqlite3.Connection, task_id: str, pid: Optional[int], *, expected_run_id: Optional[int] = None,
) -> bool:
    """Record spawn ownership, or record that we could not.

    A ``spawn_fn`` that returns no pid (an out-of-process supervisor, a launcher
    that forks and detaches) and a pid that arrives too late to belong to the run
    both used to leave the card with no ownership evidence and nothing in the event
    log saying so (23 of 44 supervisor-lane runs had no ``spawned`` event). Emitting
    ``spawn_ownership_unrecorded`` turns that silent gap into something
    ``hermes kanban tail`` shows. The task is deliberately *not* failed: the child is
    running and the claim is protected by heartbeat freshness (:func:`stale_claim_hold`).
    Returns True when ownership was recorded.
    """
    pid_int: Optional[int]
    try:
        pid_int = int(pid) if pid else None
    except (TypeError, ValueError):
        # Non-numeric but truthy (a Popen, a bare True) still spawned a child.
        pid_int = None
    if pid_int is not None and pid_int > 0:
        outcome = _record_worker_pid(conn, task_id, pid_int, expected_run_id=expected_run_id)
        if outcome in (_OWNERSHIP_RECORDED, _OWNERSHIP_ALREADY_OURS):
            return True
        if outcome == _OWNERSHIP_CONFLICT:
            return False  # the conflict event already describes the state
        reason = "run_superseded"
    else:
        reason = "spawn_returned_no_pid"
    with _kbc.write_txn(conn):
        _kb._append_event(
            conn, task_id, "spawn_ownership_unrecorded",
            {
                "reason": reason,
                "pid": pid_int,
                "expected_run_id": int(expected_run_id) if expected_run_id is not None else None,
            },
            run_id=int(expected_run_id) if expected_run_id is not None else _kb._current_run_id(conn, task_id),
        )
    return False


# Late-bound sibling namespaces (see module docstring); imported LAST so this
# module is fully populated before the siblings import from it.
from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc
from hermes_cli import kanban_db_dispatch as _kbd
from hermes_cli import kanban_db_workspace as _kbw
