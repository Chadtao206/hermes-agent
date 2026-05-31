"""Tests for the backend-agnostic dispatch glue (Phase 3, Part B / B1).

Reuse the conformance ``store`` fixture (parametrized sqlite + postgres) from
``tests/hermes_cli/kanban/conftest.py`` so every test runs on BOTH backends.

For the SQLite path, ``dispatch_plan`` runs ``kanban_db.dispatch_once``, which
uses the module-level ``kanban_db.resolve_workspace`` /
``hermes_cli.profiles.profile_exists`` / ``kanban_db.check_respawn_guard``
rather than the injected callbacks — so we monkeypatch those (same pattern as
the A5 ``test_dispatch_plan_claims_ready`` test). For the PG path the injected
``resolve_workspace`` / ``profile_exists`` callbacks are honored directly.
"""
import asyncio
import enum
import pathlib

from hermes_cli.kanban_glue import run_dispatch_tick, run_notifier_tick

_TERMINAL_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out", "archived",
)


def _field(row, key):
    """Backend-agnostic accessor for dict-or-attr task rows."""
    return row[key] if isinstance(row, dict) else getattr(row, key)


def _patch_sqlite_dispatch_path(monkeypatch, tmp_path):
    """Make the SQLite dispatch_once path treat a fresh ready task as
    spawnable + workspace-resolvable + respawn-unguarded. Harmless for PG."""
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda a: True,
                        raising=False)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.resolve_workspace",
        lambda task, board=None: pathlib.Path(str(tmp_path)))
    monkeypatch.setattr("hermes_cli.kanban_db.check_respawn_guard",
                        lambda conn, task_id: None)


def test_dispatch_tick_spawns_ready(store, monkeypatch, tmp_path):
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    tid = store.create_task(title="dispatch me", assignee="engineer")
    assert _field(store.get_task(tid), "status") == "ready"

    calls = []

    def fake_spawn(task, workspace, board=None):
        calls.append((task.id, workspace, board))
        return 4242

    summary = run_dispatch_tick(
        store,
        board=store.board,
        spawn_fn=fake_spawn,
        resolve_workspace=lambda t, board=None: str(tmp_path),
        profile_exists=lambda a: True,
        max_spawn=5,
    )

    # spawn_fn was called exactly for our task.
    assert [c[0] for c in calls] == [tid]
    # The board pin was threaded through (signature accepts board=).
    assert calls[0][2] == store.board

    task = store.get_task(tid)
    assert _field(task, "status") == "running"
    assert _field(task, "worker_pid") == 4242

    # The summary lists the actually-spawned id and counts it.
    assert tid in summary["spawned_ids"]
    assert summary["spawned"] == 1
    assert summary["spawn_failures"] == 0


def test_dispatch_tick_spawn_failure_breaker(store, monkeypatch, tmp_path):
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    tid = store.create_task(title="boom task", assignee="engineer")
    assert _field(store.get_task(tid), "status") == "ready"

    def boom_spawn(task, workspace, board=None):
        raise RuntimeError("boom")

    summary = run_dispatch_tick(
        store,
        board=store.board,
        spawn_fn=boom_spawn,
        resolve_workspace=lambda t, board=None: str(tmp_path),
        profile_exists=lambda a: True,
        max_spawn=5,
        failure_limit=1,  # first failure trips the breaker -> blocked
    )

    task = store.get_task(tid)
    assert _field(task, "status") == "blocked"

    # The summary reflects a spawn failure + the auto-block.
    assert tid not in summary["spawned_ids"]
    assert summary["spawn_failures"] >= 1
    assert summary["auto_blocked"] >= 1
    assert tid in summary["auto_blocked_ids"]

    # The breaker emitted a gave_up event.
    kinds = [_field(e, "kind") for e in store.list_events(tid)]
    assert "gave_up" in kinds


def test_dispatch_tick_spawn_failure_retries_below_limit(store, monkeypatch, tmp_path):
    """With failure_limit > 1, a single spawn failure must NOT block the task;
    it returns to ready for a later tick (per-task breaker not yet tripped)."""
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    tid = store.create_task(title="retry task", assignee="engineer")

    def boom_spawn(task, workspace, board=None):
        raise RuntimeError("transient boom")

    summary = run_dispatch_tick(
        store,
        board=store.board,
        spawn_fn=boom_spawn,
        resolve_workspace=lambda t, board=None: str(tmp_path),
        profile_exists=lambda a: True,
        max_spawn=5,
        failure_limit=2,
    )

    task = store.get_task(tid)
    assert _field(task, "status") == "ready"
    assert tid not in summary["spawned_ids"]
    assert summary["spawn_failures"] >= 1
    assert tid not in summary["auto_blocked_ids"]


def test_dispatch_tick_board_kwarg_optional(store, monkeypatch, tmp_path):
    """A spawn_fn that takes only (task, workspace) must still be callable —
    the glue introspects the signature like dispatch_once does."""
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    tid = store.create_task(title="two-arg spawn", assignee="engineer")

    calls = []

    def two_arg_spawn(task, workspace):  # no board kwarg
        calls.append(task.id)
        return 7777

    summary = run_dispatch_tick(
        store,
        board=store.board,
        spawn_fn=two_arg_spawn,
        resolve_workspace=lambda t, board=None: str(tmp_path),
        profile_exists=lambda a: True,
        max_spawn=5,
    )

    assert calls == [tid]
    assert _field(store.get_task(tid), "worker_pid") == 7777
    assert tid in summary["spawned_ids"]


def test_dispatch_tick_returns_diagnostics(store, monkeypatch, tmp_path):
    """The summary dict carries the DispatchResult diagnostics keys the
    gateway logs, plus the glue's spawned/auto_blocked surface."""
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    store.create_task(title="diag task", assignee="engineer")

    summary = run_dispatch_tick(
        store,
        board=store.board,
        spawn_fn=lambda task, workspace, board=None: 1234,
        resolve_workspace=lambda t, board=None: str(tmp_path),
        profile_exists=lambda a: True,
        max_spawn=5,
    )

    for key in (
        "ready_count", "spawnable_ready", "spawn_attempts", "spawn_failures",
        "spawned", "reclaimed", "promoted", "crashed", "timed_out", "stale",
        "auto_blocked", "spawned_ids", "auto_blocked_ids",
    ):
        assert key in summary, f"missing summary key: {key}"
    # spawned count is an int (gateway does arithmetic on it); ids is a list.
    assert isinstance(summary["spawned"], int)
    assert isinstance(summary["spawned_ids"], list)


# ---------------------------------------------------------------------------
# run_notifier_tick (B2)
#
# Backend-agnostic: the store routes daemon-vs-direct internally, so these
# tests never monkeypatch profile/workspace (the notifier path doesn't spawn
# workers). The fake adapter is keyed by the lowercased platform string because
# we drive the glue with ``platform_enum=None`` (string-match resolution).
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Records send() calls; an async send like the real chat adapters."""

    def __init__(self, raise_on_send=False):
        self.sent = []
        self._raise = raise_on_send

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "metadata": metadata or {}})
        if self._raise:
            raise RuntimeError("send boom")


def _render(ev, ev_task, sub, board):
    return f"msg:{ev.kind}"


def _wake_noop(psub, events, task, event_tasks, board):
    return True


def test_notifier_tick_delivers_and_advances(store):
    tid = store.create_task(title="notify me", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    # Terminal event: completing the task emits a ``completed`` event.
    store.complete_task(tid, summary="done")

    adapter = _FakeAdapter()
    summary = asyncio.run(run_notifier_tick(
        store,
        {"telegram": adapter},
        notifier_profile=None,
        active_platforms={"telegram"},
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=_wake_noop,
        platform_enum=None,
    ))

    # Delivered exactly once, to the subscribed chat.
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "c1"
    assert summary["delivered"] == 1
    assert tid in summary["delivered_subs"]

    # The sub completing on a done task is removed (should_unsub).
    assert not store.list_notify_subs(tid)

    # A second tick delivers nothing (cursor advanced + sub gone).
    adapter2 = _FakeAdapter()
    summary2 = asyncio.run(run_notifier_tick(
        store,
        {"telegram": adapter2},
        notifier_profile=None,
        active_platforms={"telegram"},
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=_wake_noop,
        platform_enum=None,
    ))
    assert adapter2.sent == []
    assert summary2["delivered"] == 0


def test_notifier_tick_send_failure_rewinds_then_unsubs(store):
    """max_send_failures=2: first tick rewinds (cursor restored, sub stays),
    second tick removes the sub after the failure counter trips."""
    tid = store.create_task(title="dead chat", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    # Use a non-terminal task state so should_unsub never fires on its own —
    # blocking keeps the task alive while still emitting a terminal event kind.
    store.block_task(tid, reason="halt")

    adapter = _FakeAdapter(raise_on_send=True)
    fail_counts: dict = {}

    # Tick 1: send raises -> rewind (below the limit). Sub survives.
    summary1 = asyncio.run(run_notifier_tick(
        store,
        {"telegram": adapter},
        notifier_profile=None,
        active_platforms={"telegram"},
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=_wake_noop,
        sub_fail_counts=fail_counts,
        max_send_failures=2,
        platform_enum=None,
    ))
    assert summary1["send_failures"] == 1
    assert summary1["unsubbed"] == 0
    assert store.list_notify_subs(tid), "sub should survive the first failure"

    # Tick 2: the rewind let the same event be re-claimed; the second failure
    # trips max_send_failures -> the sub is removed.
    summary2 = asyncio.run(run_notifier_tick(
        store,
        {"telegram": adapter},
        notifier_profile=None,
        active_platforms={"telegram"},
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=_wake_noop,
        sub_fail_counts=fail_counts,
        max_send_failures=2,
        platform_enum=None,
    ))
    assert summary2["send_failures"] == 1
    assert summary2["unsubbed"] == 1
    assert not store.list_notify_subs(tid), "sub removed after the 2nd failure"


def test_notifier_tick_profile_wake_advances(store):
    tid = store.create_task(title="wake me", assignee="engineer")
    # wake_agent defaults on.
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    store.complete_task(tid, summary="done")

    woke = []

    def wake_fn(psub, events, task, event_tasks, board):
        woke.append((_field(psub, "task_id"), _field(psub, "profile")))
        return True

    summary = asyncio.run(run_notifier_tick(
        store,
        {},  # no chat adapters needed for a profile wake
        notifier_profile=None,
        active_platforms=set(),
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=wake_fn,
        platform_enum=None,
    ))

    # The wake callback fired for our sub, and the tick recorded the wake.
    assert woke == [(tid, "engineer")]
    assert summary["woke"] == 1
    assert tid in summary["woke_profiles"]

    # record_profile_wake_success appended a 'success' wake-event row + advanced
    # the cursor.
    wake_events = store.list_profile_wake_events(task_id=tid)
    statuses = [_field(e, "status") for e in wake_events]
    assert "success" in statuses

    # A second tick wakes nothing (cursor advanced past the claimed event).
    woke.clear()
    summary2 = asyncio.run(run_notifier_tick(
        store,
        {},
        notifier_profile=None,
        active_platforms=set(),
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=wake_fn,
        platform_enum=None,
    ))
    assert woke == []
    assert summary2["woke"] == 0


def test_notifier_tick_platform_enum_resolution(store):
    """Exercise the ``platform_enum``-based adapter resolution — the path B4
    actually wires (the gateway keys ``self.adapters`` by its ``Platform``
    enum). Prefer the real ``gateway.config.Platform`` when it imports cleanly,
    else fall back to a tiny enum-like."""
    try:
        from gateway.config import Platform as _Platform  # real gateway type
        tg_key = _Platform.TELEGRAM
        platform_enum = _Platform
    except Exception:
        _Platform = enum.Enum("P", {"TELEGRAM": "telegram"})
        tg_key = _Platform.TELEGRAM
        platform_enum = _Platform

    tid = store.create_task(title="enum notify", assignee="engineer")
    store.add_notify_sub(task_id=tid, platform="telegram", chat_id="c1")
    store.complete_task(tid, summary="done")

    adapter = _FakeAdapter()
    summary = asyncio.run(run_notifier_tick(
        store,
        {tg_key: adapter},  # adapters keyed by the enum member (gateway style)
        notifier_profile=None,
        active_platforms={"telegram"},
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=_wake_noop,
        platform_enum=platform_enum,
    ))

    # The enum-keyed adapter was resolved and the event delivered.
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "c1"
    assert summary["delivered"] == 1
    assert tid in summary["delivered_subs"]


def test_notifier_tick_async_wake_profile_fn(store):
    """An ``async def`` wake_profile_fn returning True must take the
    ``inspect.isawaitable`` path: wake recorded as success, cursor advanced."""
    tid = store.create_task(title="async wake", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")
    store.complete_task(tid, summary="done")

    calls = []

    async def async_wake(psub, events, task, event_tasks, board):
        calls.append(_field(psub, "task_id"))
        return True

    summary = asyncio.run(run_notifier_tick(
        store,
        {},
        notifier_profile=None,
        active_platforms=set(),
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=async_wake,
        platform_enum=None,
    ))

    assert calls == [tid]
    assert summary["woke"] == 1
    assert tid in summary["woke_profiles"]

    statuses = [_field(e, "status")
                for e in store.list_profile_wake_events(task_id=tid)]
    assert "success" in statuses

    # The cursor advanced past the claimed event — a second tick wakes nothing.
    calls.clear()
    summary2 = asyncio.run(run_notifier_tick(
        store,
        {},
        notifier_profile=None,
        active_platforms=set(),
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=async_wake,
        platform_enum=None,
    ))
    assert calls == []
    assert summary2["woke"] == 0


def test_notifier_tick_profile_wake_failure_rewinds(store):
    """A wake_profile_fn that returns ``(False, "spawn boom")`` must trigger
    record_profile_wake_failure: the sub's cursor is rewound to the old value,
    a 'failed' wake-event row appears, and wake_failure_count bumps."""
    tid = store.create_task(title="wake fail", assignee="engineer")
    store.add_profile_event_sub(task_id=tid, profile="engineer")

    # Capture the cursor + failure count BEFORE the failing event is emitted.
    before = store.list_profile_event_subs(task_id=tid, enabled_only=True)
    assert len(before) == 1
    old_cursor = int(_field(before[0], "last_event_id"))
    old_fail_count = int(_field(before[0], "wake_failure_count"))

    store.complete_task(tid, summary="done")  # emits a terminal event to claim

    def failing_wake(psub, events, task, event_tasks, board):
        return (False, "spawn boom")

    summary = asyncio.run(run_notifier_tick(
        store,
        {},
        notifier_profile=None,
        active_platforms=set(),
        terminal_kinds=_TERMINAL_KINDS,
        render_chat_event=_render,
        wake_profile_fn=failing_wake,
        platform_enum=None,
    ))

    assert summary["woke"] == 0
    assert summary["profile_failed"] == 1
    assert tid not in summary["woke_profiles"]

    # The failure rewound the cursor back to its old value (so a later tick can
    # retry the same event) and bumped the failure counter.
    after = store.list_profile_event_subs(task_id=tid, enabled_only=True)
    assert len(after) == 1
    assert int(_field(after[0], "last_event_id")) == old_cursor
    assert int(_field(after[0], "wake_failure_count")) == old_fail_count + 1

    # A 'failed' wake-event row was appended carrying the error.
    wake_events = store.list_profile_wake_events(task_id=tid)
    statuses = [_field(e, "status") for e in wake_events]
    assert "failed" in statuses
    failed_row = [e for e in wake_events if _field(e, "status") == "failed"][-1]
    assert "spawn boom" in str(_field(failed_row, "error"))
