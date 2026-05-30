"""WS1 Task 8: dispatcher-spawned workers are pinned writer *clients*.

Under the single-writer flag the dispatcher injects ``HERMES_KANBAN_WRITER_SOCK``
into the worker env, and ``writer_socket_path`` honours it (mirroring how
``kanban_db_path`` honours ``HERMES_KANBAN_DB``) — defense-in-depth so the
worker's ``write_session`` → ``RemoteWriter`` always finds the daemon the
dispatcher knows serves this board. Workers are never daemon owners: that's
enforced structurally by the writer-thread-local guard, not an env var.
"""
from pathlib import Path

from hermes_cli import kanban_db as kb


def test_writer_socket_path_honors_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_WRITER_SOCK", str(tmp_path / "pinned.sock"))
    # Even with a different board arg, the explicit pin wins.
    assert kb.writer_socket_path(board="other") == tmp_path / "pinned.sock"


def test_writer_socket_path_defaults_to_db_sibling(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_WRITER_SOCK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    assert kb.writer_socket_path() == tmp_path / ".kanban-writer.sock"


def test_writer_client_env_includes_socket_under_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_WRITER_SOCK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    env = kb._writer_client_env(board="default")
    assert env["HERMES_KANBAN_WRITER_SOCK"] == str(tmp_path / ".kanban-writer.sock")
    # Workers must never be owners — there is no owner env to set.
    assert "HERMES_KANBAN_WRITER_OWNER" not in env


def test_writer_client_env_empty_when_flag_off(monkeypatch):
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
    assert kb._writer_client_env(board="default") == {}
