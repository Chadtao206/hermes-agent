"""Regression tests for kanban-worker live DB mutation guard."""

import json

from tools.terminal_tool import _kanban_worker_db_mutation_error, terminal_tool


def test_kanban_worker_blocks_live_db_copy(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/hermes/kanban.db")

    err = _kanban_worker_db_mutation_error(
        "cp /tmp/recovered_candidate.db /tmp/hermes/kanban.db"
    )

    assert "may not mutate or replace" in err
    assert "quiesced repair path" in err


def test_kanban_worker_blocks_repair_install(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/hermes/kanban.db")

    err = _kanban_worker_db_mutation_error(
        "hermes kanban repair-db /tmp/hermes/kanban.db --install"
    )

    assert "may not mutate or replace" in err


def test_kanban_worker_allows_readonly_quick_check(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/hermes/kanban.db")

    err = _kanban_worker_db_mutation_error(
        "sqlite3 /tmp/hermes/kanban.db 'PRAGMA quick_check;'"
    )

    assert err == ""


def test_non_kanban_session_not_blocked(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/hermes/kanban.db")

    err = _kanban_worker_db_mutation_error(
        "cp /tmp/recovered_candidate.db /tmp/hermes/kanban.db"
    )

    assert err == ""


def test_terminal_tool_returns_blocked_without_execution(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/hermes/kanban.db")

    result = json.loads(terminal_tool("cp /tmp/recovered_candidate.db /tmp/hermes/kanban.db"))

    assert result["status"] == "blocked"
    assert result["exit_code"] == -1
    assert "quiesced repair path" in result["error"]
