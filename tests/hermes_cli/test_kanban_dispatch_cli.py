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
    monkeypatch.setattr(kb, "_default_spawn", lambda task, ws, **kw: 4321)
    monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: str(kanban_home))
    # Verify the live tick routes through kanban_glue.run_dispatch_tick.
    # We confirm this by capturing the return value of run_dispatch_tick and
    # asserting it was invoked and recorded the spawn in its summary dict
    # (more robust than a module-level list: avoids cross-module patch identity
    # fragility across test-file boundaries).
    import hermes_cli.kanban_glue as _glue
    calls = []
    real = _glue.run_dispatch_tick

    def _capture(*a, **k):
        summary = real(*a, **k)
        calls.append(summary)
        return summary

    monkeypatch.setattr(_glue, "run_dispatch_tick", _capture)
    from hermes_cli.kanban.cli import _cmd_dispatch
    rc = _cmd_dispatch(argparse.Namespace(dry_run=False, json=True, max=None,
                                          failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert rc == 0
    # The glue was called exactly once
    assert len(calls) == 1
    # The spawn was recorded in the glue's own accounting (spawned_ids)
    assert tid in (calls[0].get("spawned_ids") or [])
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"


def test_dispatch_dry_run_is_read_only(kanban_home, monkeypatch, capsys):
    # The dry-run branch returns before the gateway-warning block, so
    # find_gateway_pids is not exercised and needn't be patched here.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ready one", assignee="engineer")
    # spawn must NEVER be called in dry-run
    monkeypatch.setattr(kb, "_default_spawn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned in dry-run")))
    from hermes_cli.kanban.cli import _cmd_dispatch
    rc = _cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                          failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert rc == 0
    out = capsys.readouterr().out
    assert tid in out
    assert "preview" in out.lower()
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"  # NOT mutated


def test_dispatch_dry_run_json_lists_assigned_candidate(kanban_home, monkeypatch, capsys):
    import json as _json
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ready assigned", assignee="engineer")
    # dry-run must never spawn
    monkeypatch.setattr(kb, "_default_spawn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned in dry-run")))
    from hermes_cli.kanban.cli import _cmd_dispatch
    rc = _cmd_dispatch(argparse.Namespace(dry_run=True, json=True, max=None,
                                          failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["preview"] is True
    assert {"task_id": tid, "assignee": "engineer"} in payload["candidates"]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"  # not mutated


def test_dispatch_warns_when_gateway_running(kanban_home, monkeypatch, capsys, all_assignees_spawnable):
    with kb.connect() as conn:
        kb.create_task(conn, title="x", assignee="engineer")
    monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: None)
    monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: str(kanban_home))
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [9999])
    from hermes_cli.kanban.cli import _cmd_dispatch
    _cmd_dispatch(argparse.Namespace(dry_run=False, json=True, max=None,
                                     failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT))
    assert "double-spawn" in capsys.readouterr().err
