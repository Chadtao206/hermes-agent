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
import pathlib

from hermes_cli.kanban_glue import run_dispatch_tick


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
