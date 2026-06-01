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


def _archived_task() -> str:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="purge me", assignee="engineer")
        kb.archive_task(conn, tid)
    return tid


def test_archive_rm_dry_run_does_not_delete(kanban_home, capsys):
    from hermes_cli.kanban.cli import _cmd_archive
    tid = _archived_task()
    rc = _cmd_archive(argparse.Namespace(task_ids=None, purge_ids=[tid], confirm=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    with kb.connect() as conn:
        assert kb.get_task(conn, tid) is not None  # NOT deleted


def test_archive_rm_confirm_deletes(kanban_home):
    from hermes_cli.kanban.cli import _cmd_archive
    tid = _archived_task()
    rc = _cmd_archive(argparse.Namespace(task_ids=None, purge_ids=[tid], confirm=True))
    assert rc == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid) is None  # deleted
