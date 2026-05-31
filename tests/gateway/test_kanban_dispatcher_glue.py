"""Gateway dispatcher → kanban_glue wiring (Phase 3, Part B / B3).

These tests prove that the gateway's embedded dispatcher, when single-writer is
DISABLED (the non-single-writer SQLite path), routes one dispatch tick through
``hermes_cli.kanban_glue.run_dispatch_tick`` — and that the consumer
normalization (which now accepts a summary *dict* from the glue OR a
``DispatchResult`` from the single-writer daemon path) reads the dict cleanly.

The single-writer daemon path (the live deployment) is intentionally NOT changed
by B3 and stays covered by the existing writer-lifecycle/notifier tests.

IMPORTANT (test isolation): some sibling kanban tests (e.g.
``test_kanban_default_assignee``) purge every ``hermes_cli*`` entry from
``sys.modules`` to force a fresh re-import under a throwaway ``HERMES_HOME``.
After that runs, the module object imported at THIS file's top would be stale,
and the gateway watcher's LAZY ``from hermes_cli import kanban_db`` would re-bind
to a different module than the one we patched — bypassing our stubs. So we
import ``kanban_db`` lazily *inside* each test and patch by dotted-string path
(``monkeypatch.setattr("hermes_cli.kanban_db....", ...)`` resolves against the
live ``sys.modules`` entry at patch time), keeping the test pollution-safe.
"""
import asyncio
import importlib
import pathlib

import pytest


def _kb():
    """The currently-live kanban_db module (pollution-safe)."""
    return importlib.import_module("hermes_cli.kanban_db")


def _seed_ready_task(db_path: pathlib.Path, *, assignee: str = "engineer") -> str:
    """Create one ready+assigned task on the tmp sqlite board and return its id."""
    kb = _kb()
    conn = kb.connect(db_path=db_path, readonly=False, _bootstrap=True)
    try:
        tid = kb.create_task(conn, title="dispatch me", assignee=assignee)
        kb.recompute_ready(conn)
    finally:
        conn.close()
    return tid


def _patch_sqlite_dispatch_path(monkeypatch, tmp_path):
    """Make dispatch_once treat a fresh ready task as spawnable +
    workspace-resolvable + respawn-unguarded (same pattern as the B1 glue test).
    The sqlite glue path uses these module-level functions, not the injected
    callbacks, so we patch them directly (by dotted string -> live module)."""
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda a: True,
                        raising=False)
    monkeypatch.setattr(
        "hermes_cli.kanban_db.resolve_workspace",
        lambda task, board=None: pathlib.Path(str(tmp_path)))
    monkeypatch.setattr("hermes_cli.kanban_db.check_respawn_guard",
                        lambda conn, task_id: None)


@pytest.mark.asyncio
async def test_gateway_dispatch_tick_uses_glue_non_single_writer(monkeypatch, tmp_path):
    """Drive ONE real watcher tick with single-writer OFF + a tmp sqlite board
    holding a ready task. Assert the glue claimed+spawned it (worker_pid stamped
    via the recorded spawn) and that the consumer-normalization path ran without
    error on the glue's summary dict."""
    from gateway.run import GatewayRunner

    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    # Make the dispatcher config explicit + fast, and disable the writer daemon
    # so the watcher takes the non-single-writer (glue) branch.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **k: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,  # keep the tick focused on dispatch
            }
        },
    )
    # resolve_backend() also reads load_config(); the stub returns no
    # kanban.backend key -> defaults to sqlite, which is what we want here.
    monkeypatch.setattr("hermes_cli.kanban_db.single_writer_enabled",
                        lambda board=None: False)
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    tid = _seed_ready_task(db_path)

    runner = object.__new__(GatewayRunner)
    runner._running = True

    spawned: list = []

    def fake_spawn(task, workspace, board=None):
        spawned.append((task.id, str(workspace), board))
        # Stop the loop the instant we've spawned: the `while self._running`
        # guard fails before a SECOND dispatch tick can run. A second tick
        # would let dispatch_once's reclaim/crash-detection phase observe our
        # (fake) pid 4242 as a dead worker and auto-block the task, flipping it
        # out of `running` — exactly the flakiness we must avoid. Stopping here
        # makes the test observe precisely one tick's outcome.
        runner._running = False
        return 4242

    monkeypatch.setattr("hermes_cli.kanban_db._default_spawn", fake_spawn)

    # Neutralize the watcher's sleeps: the 5s boot delay and the 1s shutdown
    # slices would make this test slow. A no-op sleep keeps the loop tight.
    real_sleep = asyncio.sleep

    async def _fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr("gateway.run.asyncio.sleep", _fast_sleep)

    await asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10)

    # The glue path spawned our task exactly once, threading the board pin.
    assert [s[0] for s in spawned] == [tid]

    # The task was claimed (running) and the recorded pid was stamped by the
    # glue's store.record_spawn_success — proving the glue branch ran the full
    # claim → spawn → record cycle, not the daemon route.
    kb = _kb()
    conn = kb.connect(db_path=db_path, readonly=True)
    try:
        task = kb.get_task(conn, tid)
    finally:
        conn.close()
    assert task.status == "running"
    assert task.worker_pid == 4242


@pytest.mark.asyncio
async def test_tick_for_board_returns_dict_consumer_can_get_on(monkeypatch, tmp_path):
    """The glue branch returns a summary *dict*; the watcher's consumer
    normalizes ``dispatch_res.summary() if hasattr(...,'summary') else dict(...)``
    and then calls ``.get(...)`` on it. Reproduce that wiring exactly against the
    real SqliteKanbanStore + glue and assert the dict carries the int keys the
    consumer reads."""
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    from hermes_cli import kanban_glue

    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    _patch_sqlite_dispatch_path(monkeypatch, tmp_path)

    tid = _seed_ready_task(db_path)

    # Mirror exactly what gateway/run.py's non-single-writer branch builds.
    store = SqliteKanbanStore(board=None)
    dispatch_res = kanban_glue.run_dispatch_tick(
        store,
        board=None,
        spawn_fn=lambda task, workspace, board=None: 1234,
        resolve_workspace=None,
        profile_exists=None,
        signal_fn=None,
        pid_alive_fn=None,
        max_spawn=5,
    )

    # The consumer normalization expression from gateway/run.py:
    summary = (
        dispatch_res.summary()
        if hasattr(dispatch_res, "summary")
        else dict(dispatch_res)
    )

    # The glue returns a plain dict (no .summary()), so the else-branch ran.
    assert isinstance(summary, dict)
    # Every int key the consumer's `.get(...)` reads must be present (it does
    # arithmetic / truthiness on these).
    for key in (
        "spawned", "spawn_failures", "auto_blocked", "reclaimed", "promoted",
        "crashed", "timed_out", "stale", "respawn_guarded",
        "max_in_progress_blocked", "ready_count",
    ):
        assert key in summary, f"missing consumer key: {key}"
    assert summary["spawned"] == 1
    assert tid in summary["spawned_ids"]
