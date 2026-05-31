"""Backend-agnostic dispatch + notifier glue for the kanban (Phase 3, Part B).

This module exposes two public tick functions plus a pair of shared helpers:

  * ``run_dispatch_tick`` — one dispatcher tick (claim ready tasks + spawn
    workers + record spawn success/failure) against ONE board's store.
  * ``run_notifier_tick`` — one notifier tick (claim unseen events + send chat
    deliveries via the injected adapters + orchestrate profile wakes) against
    ONE board's store. It is the board-agnostic delivery core extracted from
    the gateway's ``_kanban_notifier_watcher``; the gateway keeps board
    enumeration, heartbeats, and the corruption/disk-io quarantine.
  * ``_resolve_adapter`` — map a subscription ``platform`` string to a chat
    adapter (by ``platform_enum`` member or lowercased string key).
  * ``_sub_field`` — dict-or-attr accessor for a subscription row.

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

import asyncio
import inspect
import logging
import time
from typing import Any, Optional

from hermes_cli import kanban_db as _kb

logger = logging.getLogger(__name__)


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


def _sub_field(sub, key, default=None):
    """Accessor for a subscription row (dict-or-attr, backend-agnostic).

    Both stores return notify/profile subs as plain dicts today, but mirror
    ``run_dispatch_tick``'s tolerance for attr-style rows so the glue does not
    care which backend produced the row.
    """
    if isinstance(sub, dict):
        return sub.get(key, default)
    return getattr(sub, key, default)


def _resolve_adapter(platform_str: str, adapters, platform_enum):
    """Resolve a subscription ``platform`` string to an adapter.

    The gateway keys ``self.adapters`` by its ``Platform`` enum, so when the
    caller passes ``platform_enum`` (B4 wires the gateway's ``Platform``) we
    coerce the string to that enum and look it up — matching the gateway's
    ``adapter = self.adapters.get(_Platform(platform_str))`` exactly, including
    the ``ValueError`` (unknown platform string) escape hatch.

    Returns ``(status, adapter)`` where ``status`` is one of:
      * ``"ok"``     — adapter resolved (``adapter`` is the adapter).
      * ``"unknown"``— the platform string isn't a valid enum member; the
        gateway advances the cursor so a bad sub doesn't replay forever.
      * ``"missing"``— a valid platform but no connected adapter; the gateway
        rewinds the claim so a later tick can retry once it reconnects.

    With ``platform_enum=None`` we resolve against the ``adapters`` dict in two
    steps: first a direct ``platform_str in adapters`` lookup (the key is NOT
    lowercased — a dict keyed by the already-lowercased ``"telegram"`` hits
    here), then a fallback loop that compares each adapter key's value
    *lowercased* against ``platform_str``, so callers/tests can key the dict by
    ``"telegram"`` or by an enum-ish object whose ``.value`` lowercases to it.
    """
    if platform_enum is not None:
        try:
            key = platform_enum(platform_str)
        except ValueError:
            return "unknown", None
        adapter = adapters.get(key)
        return ("ok", adapter) if adapter is not None else ("missing", None)
    # String-key resolution: lowercased platform vs lowercased adapter keys.
    if platform_str in adapters:
        return "ok", adapters[platform_str]
    for k, v in adapters.items():
        if str(getattr(k, "value", k)).lower() == platform_str:
            return "ok", v
    # No matching key at all — treat as a disconnected/missing adapter so the
    # claim rewinds (a later tick can retry) rather than being dropped.
    return "missing", None


async def run_notifier_tick(
    store,
    adapters,
    *,
    notifier_profile,
    active_platforms,
    terminal_kinds,
    render_chat_event,
    wake_profile_fn,
    deliver_artifacts_fn=None,
    sub_fail_counts=None,
    max_send_failures=3,
    platform_enum=None,
    board=None,
) -> dict:
    """One backend-agnostic notifier tick for ONE board's ``store``.

    Reads subs + claims unseen events via the store, sends chat deliveries via
    ``adapters`` (rendered by ``render_chat_event``), advances/rewinds cursors
    via the store, orchestrates profile wakes via ``wake_profile_fn``, and
    records wake success/failure via the store. NO asyncio loop, NO board
    enumeration, NO heartbeat/quarantine — those stay in the gateway watcher.

    This is the board-agnostic delivery core extracted from the gateway's
    ``_kanban_notifier_watcher._collect`` (the read side) + its chat-delivery
    and profile-wake loops. The gateway KEEPS board enumeration, heartbeats,
    daemon lookup, identity/config, the corruption/disk-io quarantine maps, and
    the asyncio while-loop. The ``store`` routes daemon-vs-direct internally, so
    there is NO ``board_daemon.execute(...)`` branching and NO ``conn`` handling
    here — only ``store.<method>`` calls.

    Args:
        store: a :class:`~hermes_cli.kanban.store.KanbanStore` for ONE board.
        adapters: chat adapters. When ``platform_enum`` is given, this is keyed
            by ``platform_enum`` members (the gateway's ``self.adapters``);
            otherwise keyed by lowercased platform string.
        notifier_profile: this notifier's owning profile. Subs whose
            ``notifier_profile`` is set and differs are skipped (another
            notifier owns them).
        active_platforms: set of lowercased connected-platform strings. Chat
            subs on a platform not in this set are skipped. Profile-event subs
            are independent of connected adapters and always processed.
        terminal_kinds: default kinds claimed for a sub that does not pin its
            own ``event_kinds`` (the gateway's ``TERMINAL_KINDS``).
        render_chat_event: ``render_chat_event(ev, ev_task, sub, board) -> str``
            INJECTED message formatter (the gateway keeps its per-kind
            formatter and passes it in). The glue does NOT format messages.
        wake_profile_fn: ``wake_profile_fn(psub, events, task, event_tasks,
            board) -> Any`` INJECTED profile-wake. May return ``bool`` (legacy)
            or ``(bool, error)``; on a falsy/exception result the glue records a
            wake failure via the store, otherwise a wake success. The callback
            is invoked DIRECTLY on the event loop (the glue awaits the result
            only if it is awaitable), so B4 MUST pass either a coroutine
            function or a callable that internally wraps blocking work (e.g.
            ``subprocess.Popen``) in ``asyncio.to_thread`` — otherwise it blocks
            the loop. (The gateway calls its blocking ``_kanban_profile_wake``
            via ``await asyncio.to_thread(...)``; B4's injected wrapper must
            preserve that.)
        deliver_artifacts_fn: optional async
            ``deliver_artifacts_fn(adapter, chat_id, metadata, event_payload,
            task)`` called once per ``completed`` event after the text send.
            ``None`` (default) skips artifact delivery.
        sub_fail_counts: caller-owned ``{sub_key: int}`` dict so the per-sub
            consecutive-send-failure counter persists across ticks. A fresh
            dict is used when ``None`` (failures won't accumulate across ticks).
        max_send_failures: drop a chat sub after this many consecutive send
            failures (the gateway's ``MAX_SEND_FAILURES``).
        platform_enum: the gateway's ``Platform`` enum used to resolve a
            platform string → adapter key. ``None`` → resolve by lowercased
            string match against ``adapters`` keys.
        board: board slug pinned for this tick; forwarded to the injected
            ``render_chat_event`` / ``wake_profile_fn`` callbacks for context.

    Returns a plain summary dict:
        ``delivered`` (chat events sent), ``woke`` (profile wakes spawned),
        ``unsubbed`` (chat subs removed), ``send_failures`` (failed sends),
        ``profile_advanced`` (wake-disabled profile subs acked),
        ``profile_failed`` (profile wakes that failed), plus the id lists
        ``delivered_subs`` / ``woke_profiles`` / ``unsubbed_subs``.
    """
    if sub_fail_counts is None:
        sub_fail_counts = {}
    active = {str(p).lower() for p in (active_platforms or set())}

    # ------------------------------------------------------------------
    # Read side (ported from _collect). One sync collection, off-thread so
    # the loop never blocks on the store's IO — the gateway does the same.
    # ------------------------------------------------------------------
    def _collect():
        chat_deliveries: list[dict] = []
        profile_deliveries: list[dict] = []

        # Chat subscriptions only matter when an adapter is connected.
        subs = store.list_notify_subs() if active else []
        for sub in subs:
            # Per-sub isolation (symmetry with the profile loop below): one
            # malformed/raising chat sub is SKIPPED so the rest of the tick
            # proceeds. The gateway's outer board try/except covered this, but
            # per-sub isolation is strictly more robust and matches intent.
            try:
                owner_profile = _sub_field(sub, "notifier_profile") or None
                if owner_profile and owner_profile != notifier_profile:
                    continue
                platform = (_sub_field(sub, "platform") or "").lower()
                if platform not in active:
                    continue
                # Per-sub event_kinds override the default terminal-only list;
                # NULL/empty falls back to the injected terminal kinds.
                sub_kinds = _kb._parse_event_kinds_column(
                    _sub_field(sub, "event_kinds")
                )
                effective_kinds = (
                    tuple(sub_kinds) if sub_kinds else tuple(terminal_kinds)
                )
                include_children = bool(_sub_field(sub, "include_children"))
                old_cursor, cursor, events = store.claim_unseen_events_for_sub(
                    task_id=_sub_field(sub, "task_id"),
                    platform=_sub_field(sub, "platform"),
                    chat_id=_sub_field(sub, "chat_id"),
                    thread_id=_sub_field(sub, "thread_id") or "",
                    kinds=effective_kinds,
                    include_children=include_children,
                )
                if not events:
                    continue
                task = store.get_task(_sub_field(sub, "task_id"))
                event_tasks: dict[str, Any] = {}
                if include_children:
                    for ev in events:
                        if ev.task_id in event_tasks:
                            continue
                        if ev.task_id == _sub_field(sub, "task_id"):
                            event_tasks[ev.task_id] = task
                        else:
                            try:
                                event_tasks[ev.task_id] = store.get_task(ev.task_id)
                            except Exception:
                                event_tasks[ev.task_id] = None
            except Exception as exc:
                logger.warning(
                    "kanban notifier: chat claim failed for %s/%s on board %s: %s",
                    _sub_field(sub, "task_id"), _sub_field(sub, "platform"),
                    board, exc,
                )
                continue
            chat_deliveries.append({
                "sub": sub,
                "old_cursor": old_cursor,
                "cursor": cursor,
                "events": events,
                "task": task,
                "event_tasks": event_tasks,
                "board": board,
            })

        # Profile-level event subs (adapter-independent) — always processed.
        try:
            pro_subs = store.list_profile_event_subs(enabled_only=True)
        except Exception:
            pro_subs = []
        for psub in pro_subs:
            # Per-sub isolation: one malformed/raising profile sub must be
            # SKIPPED, not abort the whole _collect (which would discard the
            # already-collected chat deliveries too). Mirrors the gateway's
            # generic per-sub ``try/except: logger.warning; continue``
            # (gateway/run.py:6207, 6291-6297). The corruption/disk-io
            # quarantine stays in the gateway — this is just the generic skip.
            try:
                old_pc, pc, p_events = store.claim_unseen_events_for_profile_sub(
                    task_id=_sub_field(psub, "task_id"),
                    profile=_sub_field(psub, "profile"),
                    name=_sub_field(psub, "name") or "",
                )
                if not p_events:
                    continue
                p_task = store.get_task(_sub_field(psub, "task_id"))
                p_event_tasks: dict[str, Any] = {}
                if bool(_sub_field(psub, "include_children")):
                    for ev in p_events:
                        if ev.task_id in p_event_tasks:
                            continue
                        if ev.task_id == _sub_field(psub, "task_id"):
                            p_event_tasks[ev.task_id] = p_task
                        else:
                            try:
                                p_event_tasks[ev.task_id] = store.get_task(ev.task_id)
                            except Exception:
                                p_event_tasks[ev.task_id] = None
            except Exception as exc:
                logger.warning(
                    "kanban notifier: profile-event claim failed for %s/%s on "
                    "board %s: %s",
                    _sub_field(psub, "task_id"), _sub_field(psub, "profile"),
                    board, exc,
                )
                continue
            profile_deliveries.append({
                "sub": psub,
                "old_cursor": old_pc,
                "cursor": pc,
                "events": p_events,
                "task": p_task,
                "event_tasks": p_event_tasks,
                "board": board,
            })
        return chat_deliveries, profile_deliveries

    deliveries, profile_deliveries = await asyncio.to_thread(_collect)

    delivered = 0
    send_failures = 0
    unsubbed_subs: list[str] = []
    delivered_subs: list[str] = []

    # ------------------------------------------------------------------
    # Chat delivery loop (ported from gateway:6399-6624).
    # ------------------------------------------------------------------
    for d in deliveries:
        sub = d["sub"]
        task = d["task"]
        platform_str = (_sub_field(sub, "platform") or "").lower()
        status, adapter = _resolve_adapter(platform_str, adapters, platform_enum)
        if status == "unknown":
            # Unknown platform string; advance so we don't replay forever.
            await asyncio.to_thread(
                store.advance_notify_cursor,
                task_id=_sub_field(sub, "task_id"),
                platform=_sub_field(sub, "platform"),
                chat_id=_sub_field(sub, "chat_id"),
                thread_id=_sub_field(sub, "thread_id") or "",
                new_cursor=d["cursor"],
            )
            continue
        if status == "missing":
            # Valid platform but adapter disconnected before delivery; rewind
            # the claim so a later tick retries once it reconnects.
            await asyncio.to_thread(
                store.rewind_notify_cursor,
                task_id=_sub_field(sub, "task_id"),
                platform=_sub_field(sub, "platform"),
                chat_id=_sub_field(sub, "chat_id"),
                thread_id=_sub_field(sub, "thread_id") or "",
                claimed_cursor=d["cursor"],
                old_cursor=int(d.get("old_cursor", 0) or 0),
            )
            continue

        event_tasks = d.get("event_tasks") or {}
        # The claim advanced the cursor to the batch max before delivery,
        # giving this tick exclusive ownership of the range. If a later send
        # fails after earlier sends succeeded, rewind only to the last
        # successfully delivered event id so retry doesn't duplicate pings.
        last_success_cursor = int(d.get("old_cursor", 0) or 0)
        sub_key = (
            _sub_field(sub, "task_id"),
            _sub_field(sub, "platform"),
            _sub_field(sub, "chat_id"),
            _sub_field(sub, "thread_id") or "",
        )
        all_delivered = True
        for ev in d["events"]:
            ev_task = event_tasks.get(ev.task_id) or task
            msg = render_chat_event(ev, ev_task, sub, d.get("board"))
            metadata: dict[str, Any] = {}
            if _sub_field(sub, "thread_id"):
                metadata["thread_id"] = _sub_field(sub, "thread_id")
            try:
                await adapter.send(
                    _sub_field(sub, "chat_id"), msg, metadata=metadata,
                )
                # Surface artifacts on the completed event only (never on
                # retries) when the caller injected a delivery callback.
                if ev.kind == "completed" and deliver_artifacts_fn is not None:
                    try:
                        await deliver_artifacts_fn(
                            adapter=adapter,
                            chat_id=_sub_field(sub, "chat_id"),
                            metadata=metadata,
                            event_payload=getattr(ev, "payload", None),
                            task=ev_task,
                        )
                    except Exception as art_exc:
                        # Artifact delivery is best-effort; never fail the
                        # send / rewind the cursor over an upload error.
                        # Matches the gateway's logger.debug (run.py:6554).
                        logger.debug(
                            "kanban notifier: artifact delivery for %s failed: %s",
                            _sub_field(sub, "task_id"), art_exc,
                        )
                delivered += 1
                last_success_cursor = max(
                    last_success_cursor, int(getattr(ev, "id", 0) or 0),
                )
                sub_fail_counts.pop(sub_key, None)
            except Exception:
                all_delivered = False
                fails = sub_fail_counts.get(sub_key, 0) + 1
                sub_fail_counts[sub_key] = fails
                send_failures += 1
                if fails >= max_send_failures:
                    await asyncio.to_thread(
                        store.remove_notify_sub,
                        task_id=_sub_field(sub, "task_id"),
                        platform=_sub_field(sub, "platform"),
                        chat_id=_sub_field(sub, "chat_id"),
                        thread_id=_sub_field(sub, "thread_id") or "",
                    )
                    sub_fail_counts.pop(sub_key, None)
                    unsubbed_subs.append(_sub_field(sub, "task_id"))
                else:
                    await asyncio.to_thread(
                        store.rewind_notify_cursor,
                        task_id=_sub_field(sub, "task_id"),
                        platform=_sub_field(sub, "platform"),
                        chat_id=_sub_field(sub, "chat_id"),
                        thread_id=_sub_field(sub, "thread_id") or "",
                        claimed_cursor=d["cursor"],
                        old_cursor=last_success_cursor,
                    )
                # Transient or terminal, stop processing this batch.
                break

        if all_delivered:
            # All events delivered; advance cursor (the dedup mechanism).
            await asyncio.to_thread(
                store.advance_notify_cursor,
                task_id=_sub_field(sub, "task_id"),
                platform=_sub_field(sub, "platform"),
                chat_id=_sub_field(sub, "chat_id"),
                thread_id=_sub_field(sub, "thread_id") or "",
                new_cursor=d["cursor"],
            )
            delivered_subs.append(_sub_field(sub, "task_id"))
            # Unsub only when the task reached a truly final status
            # (done / archived). Subtree subs stay alive after the root
            # completes (downstream lane events still matter); archive is the
            # explicit terminal cleanup. Mirrors gateway:6614-6623.
            include_children = bool(_sub_field(sub, "include_children"))
            task_status = _sub_field(task, "status") if task is not None else None
            task_terminal = task is not None and task_status in {"done", "archived"}
            should_unsub = bool(
                task_terminal
                and (not include_children or task_status == "archived")
            )
            if should_unsub:
                await asyncio.to_thread(
                    store.remove_notify_sub,
                    task_id=_sub_field(sub, "task_id"),
                    platform=_sub_field(sub, "platform"),
                    chat_id=_sub_field(sub, "chat_id"),
                    thread_id=_sub_field(sub, "thread_id") or "",
                )
                unsubbed_subs.append(_sub_field(sub, "task_id"))

    # ------------------------------------------------------------------
    # Profile wake loop (ported from gateway:6625-6686).
    # ------------------------------------------------------------------
    woke = 0
    profile_advanced = 0
    profile_failed = 0
    woke_profiles: list[str] = []
    for pd in profile_deliveries:
        psub = pd["sub"]
        if not int(_sub_field(psub, "wake_agent") or 0):
            # Sub recorded events but is configured not to wake an agent —
            # just advance the cursor to ack the range. No wake happened, so
            # we do NOT record a wake-events row.
            await asyncio.to_thread(
                store.advance_profile_event_cursor,
                task_id=_sub_field(psub, "task_id"),
                profile=_sub_field(psub, "profile"),
                name=_sub_field(psub, "name") or "",
                new_cursor=pd["cursor"],
                last_wake_at=None,
            )
            profile_advanced += 1
            continue
        wake_error: Any = None
        try:
            result = wake_profile_fn(
                psub, pd["events"], pd["task"], pd["event_tasks"], pd.get("board"),
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            result = False
            wake_error = exc
        # wake_profile_fn returns bool (legacy) or (bool, error).
        if isinstance(result, tuple) and len(result) == 2:
            ok = bool(result[0])
            if not ok and wake_error is None:
                wake_error = result[1]
        else:
            ok = bool(result)
        if ok:
            await asyncio.to_thread(
                store.record_profile_wake_success,
                task_id=_sub_field(psub, "task_id"),
                profile=_sub_field(psub, "profile"),
                name=_sub_field(psub, "name") or "",
                new_cursor=pd["cursor"],
                last_wake_at=int(time.time()),
            )
            woke += 1
            woke_profiles.append(_sub_field(psub, "task_id"))
        else:
            await asyncio.to_thread(
                store.record_profile_wake_failure,
                task_id=_sub_field(psub, "task_id"),
                profile=_sub_field(psub, "profile"),
                name=_sub_field(psub, "name") or "",
                claimed_cursor=pd["cursor"],
                old_cursor=int(pd.get("old_cursor", 0) or 0),
                error=wake_error,
            )
            profile_failed += 1

    return {
        "delivered": delivered,
        "woke": woke,
        "unsubbed": len(unsubbed_subs),
        "send_failures": send_failures,
        "profile_advanced": profile_advanced,
        "profile_failed": profile_failed,
        "delivered_subs": delivered_subs,
        "woke_profiles": woke_profiles,
        "unsubbed_subs": unsubbed_subs,
    }
