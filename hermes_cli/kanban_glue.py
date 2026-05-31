"""Backend-agnostic dispatch glue for the kanban dispatcher (Phase 3, Part B).

``run_dispatch_tick`` runs ONE dispatcher tick against ONE board's
:class:`~hermes_cli.kanban.store.KanbanStore`:

  1. ``store.dispatch_plan(...)`` claims ready tasks + does the DB-side reclaim
     (using the glue-provided fs/profile/OS callbacks) and returns a
     :class:`~hermes_cli.kanban.store.DispatchPlan` whose ``to_spawn`` is the
     list of ``(claimed_task, workspace_path)`` tuples that are claimed,
     workspace-resolved, and ready to spawn.
  2. The glue spawns each planned task via the injected ``spawn_fn`` and records
     success (``store.record_spawn_success``) or failure
     (``store.record_spawn_failure`` / ``store.record_task_failure``) back into
     the store.
  3. It returns a summary dict (the :class:`DispatchResult` diagnostics plus the
     actually-spawned / auto-blocked task ids).

This module is deliberately backend-agnostic: it makes only ``store.<method>``
calls and invokes injected callbacks. It does NOT branch on the backend type,
does NOT touch SQLite/Postgres internals directly, does NOT do asyncio, and does
NOT enumerate boards (the caller passes one store / one board).

The spawn-failure handling here REPLICATES ``kanban_db.dispatch_once``'s
per-task circuit breaker + systemic-signature grouping
(``_record_dispatch_spawn_failure`` / ``_block_systemic_spawn_failure_signature``
in ``kanban_db``). On the SQLite path the real spawn now happens HERE (the
store's ``dispatch_plan`` passes ``dispatch_once`` a capturing stub that never
raises), so ``dispatch_once``'s INTERNAL spawn-failure path is no longer
exercised — replicating it here preserves the legacy SQLite behavior.

The glue runs IN the gateway process (host-local), so it MAY import host-local
``kanban_db`` helpers (``_error_fingerprint``, the systemic constants).
"""
from __future__ import annotations

import inspect
from typing import Any, Optional

from hermes_cli import kanban_db as _kb


def _spawn_accepts_board(spawn_fn) -> bool:
    """True when ``spawn_fn`` accepts a ``board`` kwarg.

    Mirrors ``kanban_db.dispatch_once`` (kanban_db.py:8030-8038): older /
    test ``spawn_fn`` signatures accept only ``(task, workspace)``. Introspect
    and pass ``board`` only when supported so those stubs keep working.
    """
    try:
        sig = inspect.signature(spawn_fn)
    except (TypeError, ValueError):
        return False
    params = sig.parameters
    if "board" in params:
        return True
    # A bare ``**kwargs`` callable can also take board=...
    return any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def run_dispatch_tick(
    store,
    *,
    board: Optional[str] = None,
    spawn_fn,
    resolve_workspace=None,
    profile_exists=None,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = 2,
    stale_timeout_seconds: int = 0,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    ttl_seconds: Optional[int] = None,
    signal_fn=None,
    pid_alive_fn=None,
) -> dict:
    """One backend-agnostic dispatch tick for ONE board's ``store``.

    Calls ``store.dispatch_plan(...)`` (which claims ready tasks + does the
    DB-side reclaim, using the glue-provided fs/profile/OS callbacks), spawns
    each planned task via ``spawn_fn``, and records spawn success/failure back
    into the store — replicating ``dispatch_once``'s per-task breaker +
    systemic-signature grouping so SQLite behavior is preserved.

    Returns a plain summary dict: the :class:`DispatchResult` diagnostics
    (``plan.result.summary()``) merged with the *post-spawn* truth — the
    ``spawned`` / ``spawn_failures`` / ``auto_blocked`` integer counts are
    updated to reflect what actually happened in this glue, and the
    ``spawned_ids`` / ``auto_blocked_ids`` lists carry the concrete task ids.

    NO asyncio, NO SQLite-specifics, NO board enumeration.

    Args:
        store: a :class:`~hermes_cli.kanban.store.KanbanStore`.
        board: board slug pinned for this tick (passed to ``spawn_fn`` when its
            signature accepts a ``board`` kwarg).
        spawn_fn: ``spawn_fn(task, workspace[, board=...]) -> Optional[int]``.
            A truthy return is recorded as the worker pid. Raising signals a
            spawn failure (counted + breaker-eligible).
        resolve_workspace / profile_exists: injected callbacks forwarded to
            ``store.dispatch_plan`` (the PG store uses them directly; the SQLite
            store relies on the module-level ``kanban_db`` functions, which
            tests monkeypatch).
        signal_fn / pid_alive_fn: injected OS callbacks forwarded straight to
            ``store.dispatch_plan`` for its DB-side reclaim. B1 passes the
            caller-provided callbacks through unchanged. (B3 wires the gateway's
            real ``os.kill``-based ``signal_fn`` / ``os.kill(pid, 0)``-style
            ``pid_alive_fn``. The full SIGTERM->SIGKILL kill ladder is a tracked
            phase-3-tail on the PG store; SQLite's ``dispatch_once`` runs its own
            ladder internally.)
        Remaining args are dispatch config forwarded to ``store.dispatch_plan``.
    """
    plan = store.dispatch_plan(
        resolve_workspace=resolve_workspace,
        profile_exists=profile_exists,
        signal_fn=signal_fn,
        pid_alive_fn=pid_alive_fn,
        max_spawn=max_spawn,
        max_in_progress=max_in_progress,
        failure_limit=failure_limit,
        stale_timeout_seconds=stale_timeout_seconds,
        default_assignee=default_assignee,
        max_in_progress_per_profile=max_in_progress_per_profile,
        ttl_seconds=ttl_seconds,
    )

    # NOTE: zombie reaping is NOT done here. On the SQLite path
    # ``dispatch_once`` reaps internally (preserved, runs inside dispatch_plan);
    # on the PG path crash-detection-via-reap is a tracked phase-3-tail. The
    # glue never owns a reap loop because it must stay backend-agnostic.

    spawned_ids: list[str] = []
    glue_spawn_failures = 0
    # Auto-blocks the glue's spawn-failure handling produced this tick (kept
    # separate from any auto-blocks already recorded by ``dispatch_plan`` /
    # ``dispatch_once``'s reclaim phase, which are already in
    # ``plan.result.auto_blocked``).
    glue_auto_blocked: list[str] = []

    accepts_board = _spawn_accepts_board(spawn_fn)

    # Per-signature spawn-failure groups, mirroring dispatch_once's
    # ``spawn_failure_groups`` local (kanban_db.py:7803). Once a group crosses
    # SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD the failure is "systemic": the
    # offending task is auto-blocked via a capped failure_limit rather than the
    # normal consecutive-failure counter.
    spawn_failure_groups: dict[str, list[str]] = {}

    def _record_dispatch_spawn_failure(task_id: str, error: str) -> None:
        """Replicate ``dispatch_once._record_dispatch_spawn_failure``.

        Fingerprints the error, groups same-signature failures, and either
        escalates to a systemic auto-block (capped failure_limit) or records a
        normal spawn failure subject to the per-task breaker.
        """
        nonlocal glue_spawn_failures
        glue_spawn_failures += 1
        signature = _kb._error_fingerprint(error)
        group = spawn_failure_groups.setdefault(signature, [])
        if task_id not in group:
            group.append(task_id)
        systemic = len(group) >= _kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD
        if systemic:
            event_extra = {
                "failure_class": _kb.FAILURE_CLASS_SYSTEMIC_SPAWN_FAILURE,
                "failure_signature": signature,
                "signature_count": len(group),
                "signature_threshold": _kb.SYSTEMIC_SPAWN_FAILURE_SIGNATURE_THRESHOLD,
                "limit_source": "systemic_failure_signature",
                "guidance": _kb._SYSTEMIC_SPAWN_FAILURE_GUIDANCE,
            }
            auto = store.record_task_failure(
                task_id,
                error,
                outcome="spawn_failed",
                failure_limit=1,
                failure_limit_is_cap=True,
                release_claim=True,
                end_run=True,
                event_payload_extra=event_extra,
            )
            # phase-3-tail: the systemic-SIBLING pre-emptive block
            # (kanban_db._block_systemic_spawn_failure_signature) — which blocks
            # the OTHER already-failed tasks in the same signature group without
            # re-incrementing their counters — is NOT replicated here. Doing so
            # cross-backend needs a new store method (block a set of ids by
            # shared signature) that does not exist yet; adding store surface is
            # out of scope for B1. The PRIMARY systemic behavior — escalating the
            # current task via failure_limit_is_cap — IS preserved above, so a
            # systemic spawn failure still trips the breaker on the offending
            # task at the threshold. Siblings will block via their own next
            # spawn failure rather than pre-emptively. Tracked for the
            # phase-3-tail (B-series follow-up).
        else:
            auto = store.record_spawn_failure(
                task_id, error, failure_limit=failure_limit
            )
        if auto and task_id not in glue_auto_blocked:
            glue_auto_blocked.append(task_id)

    for task, workspace in plan.to_spawn:
        task_id = task.id
        try:
            if accepts_board:
                pid = spawn_fn(task, workspace, board=board)
            else:
                pid = spawn_fn(task, workspace)
        except Exception as exc:  # spawn raised -> failure path
            _record_dispatch_spawn_failure(task_id, str(exc))
            continue
        if pid:
            store.record_spawn_success(task_id, int(pid))
            spawned_ids.append(task_id)
        # A falsy (None / 0) pid is NOT a failure — it mirrors dispatch_once,
        # which records the spawn but skips the worker-pid stamp (e.g. a
        # detached spawner that doesn't surface a pid). The claim already
        # flipped the task to running; leave it there.

    # Build the summary. Start from the DispatchResult diagnostics (the same
    # dict the gateway logs today), then overlay the post-spawn truth.
    summary: dict[str, Any] = dict(plan.result.summary())

    # ``plan.result.summary()`` counts every task that dispatch_plan *placed in
    # to_spawn* as "spawned" (optimistic — the real spawn happens here) and only
    # counts spawn failures / auto-blocks that happened DURING planning
    # (workspace resolution, reclaim phase). Overlay the actual glue outcomes so
    # the integer counts reflect what truly happened this tick. Keep them ints
    # (the gateway does arithmetic on summary["spawned"] etc.); expose the
    # concrete ids under explicit *_ids keys.
    summary["spawned"] = len(spawned_ids)
    summary["spawn_failures"] = int(summary.get("spawn_failures", 0)) + glue_spawn_failures
    summary["auto_blocked"] = int(summary.get("auto_blocked", 0)) + len(glue_auto_blocked)
    summary["spawned_ids"] = spawned_ids
    summary["auto_blocked_ids"] = list(plan.result.auto_blocked) + glue_auto_blocked
    return summary
