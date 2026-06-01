"""Tests that _cmd_dispatch live tick routes through kanban_glue.run_dispatch_tick."""
from __future__ import annotations
from pathlib import Path
import argparse
import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "sqlite")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
    kb.init_db()
    return home


def test_dispatch_live_tick_spawns_via_glue(kanban_home, monkeypatch, all_assignees_spawnable):
    # one ready, assigned task
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do it", assignee="engineer")
    # capture spawns instead of launching a real worker
    spawned = []
    monkeypatch.setattr(kb, "_default_spawn", lambda task, ws, **kw: spawned.append(task.id) or 4321)
    monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: str(kanban_home))
    # Verify the live tick routes through kanban_glue.run_dispatch_tick.
    # We confirm this by capturing calls to run_dispatch_tick and asserting it
    # was invoked (the SQLite store.dispatch_plan legitimately uses dispatch_once
    # internally, so patching dispatch_once to raise would be too aggressive).
    import hermes_cli.kanban_glue as _glue
    glue_calls = []
    real_run_dispatch_tick = _glue.run_dispatch_tick

    def _capturing_run_dispatch_tick(store, **kwargs):
        glue_calls.append(kwargs)
        return real_run_dispatch_tick(store, **kwargs)

    monkeypatch.setattr(_glue, "run_dispatch_tick", _capturing_run_dispatch_tick)
    from hermes_cli.kanban.cli import _cmd_dispatch
    rc = _cmd_dispatch(argparse.Namespace(dry_run=False, json=True, max=None,
                                          failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert rc == 0
    # The glue was called
    assert len(glue_calls) == 1
    # The task was spawned via the glue path
    assert spawned == [tid]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
