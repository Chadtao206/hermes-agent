from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "kanban_reliability_delta_watch.py"


@pytest.fixture()
def watch_module(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    spec = importlib.util.spec_from_file_location("kanban_reliability_delta_watch", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "KANBAN_DB", home / "kanban.db")
    setattr(module, "STATE_PATH", home / "kanban_reliability_delta_watch_state.json")
    return module


def _create_task(*, title: str, assignee: str, status: str = "ready") -> str:
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title=title, assignee=assignee)
        if status != "ready":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        return task_id
    finally:
        conn.close()


def _insert_event(task_id: str, kind: str, created_at: int) -> None:
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES(?, ?, ?, ?, ?)",
                (task_id, None, kind, "{}", created_at),
            )
    finally:
        conn.close()


def test_watcher_baseline_then_silent_when_unchanged(watch_module, monkeypatch, capsys):
    now = int(time.time())
    monkeypatch.setattr(watch_module.time, "time", lambda: float(now))

    rc1 = watch_module.main()
    out1 = capsys.readouterr().out
    assert rc1 == 0
    assert "baseline established" in out1

    rc2 = watch_module.main()
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out2 == ""


def test_watcher_emits_real_events_and_ignores_synthetic(watch_module, monkeypatch, capsys):
    now = int(time.time())
    real_task = _create_task(title="real reliability task", assignee="engineer", status="running")
    synthetic_task = _create_task(title="repro stale-dispatch", assignee="testbot", status="running")
    _insert_event(real_task, "crashed", now)
    _insert_event(synthetic_task, "crashed", now)

    monkeypatch.setattr(watch_module.time, "time", lambda: float(now))
    rc = watch_module.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "real=1" in out
    assert "synthetic=1" in out
    assert real_task in out
    assert synthetic_task not in out


def test_watcher_uses_snapshot_connect(watch_module, monkeypatch, capsys):
    calls: list[Path] = []
    real_snapshot_connect = watch_module.kanban_db.snapshot_connect

    def _recording_snapshot_connect(*, board=None, db_path=None):
        calls.append(Path(db_path) if db_path is not None else Path(""))
        return real_snapshot_connect(board=board, db_path=db_path)

    monkeypatch.setattr(watch_module.kanban_db, "snapshot_connect", _recording_snapshot_connect)
    real_time = time.time
    monkeypatch.setattr(watch_module.time, "time", lambda: float(int(real_time())))

    rc = watch_module.main()
    _ = capsys.readouterr().out

    assert rc == 0
    assert calls == [watch_module.KANBAN_DB]
    assert not watch_module.KANBAN_DB.with_name(watch_module.KANBAN_DB.name + "-wal").exists()
    assert not watch_module.KANBAN_DB.with_name(watch_module.KANBAN_DB.name + "-shm").exists()
