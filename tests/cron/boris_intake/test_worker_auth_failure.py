from types import SimpleNamespace

import pytest


def _shell(monkeypatch, relogin):
    from hermes_cli.auth import AuthError
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    def unavailable(**kwargs):
        raise AuthError("No Codex credentials stored", provider="openai-codex",
                        code="codex_auth_missing", relogin_required=relogin)

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", unavailable)
    shell = CLIAgentSetupMixin.__new__(CLIAgentSetupMixin)
    shell.model = "fixture"
    shell.requested_provider = "openai-codex"
    shell._explicit_api_key = shell._explicit_base_url = None
    shell._fallback_model = []
    shell.tool_progress_mode = "off"
    return shell


def _kanban(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = kb._claimer_id().split(":", 1)[0]
    return db, host


@pytest.mark.parametrize("relogin", [True, False])
def test_credential_failure_parks_the_card_only_when_reauthentication_is_required(
    tmp_path, monkeypatch, relogin
):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
    from hermes_cli.turn_exit import turn_exit_code
    from boris_intake.dispatch import KanbanIO

    db, host = _kanban(tmp_path, monkeypatch)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    shell = _shell(monkeypatch, relogin)
    assert shell._ensure_runtime_credentials() is False
    code = turn_exit_code(None, kanban_worker=True,
                          credentials_rate_limited=shell._credentials_rate_limited,
                          credentials_terminal=shell._credentials_terminal)
    assert code == (kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE if relogin else 1)

    with kbc.connect_closing(db) as conn:
        tid = kb.create_task(conn, title="review", assignee="boris_on_demand")
        assert kb.claim_task(conn, tid, claimer=f"{host}:w1") is not None
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (71000, tid))
        conn.commit()
        kbd._record_worker_exit(71000, code << 8)
        kbd.detect_crashed_workers(conn)
    adapter = KanbanIO(db)
    if relogin:
        assert adapter.lane_status(tid) == "blocked"
        assert adapter.lane_authentication_failed(tid) is True
        with kbc.connect_closing(db) as conn:
            assert not kb.claim_task(conn, tid, claimer="test:retry")
    else:
        assert adapter.lane_status(tid) != "blocked"
        assert adapter.lane_authentication_failed(tid) is False


def test_a_superseded_workers_terminal_exit_leaves_the_live_run_alone(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
    from boris_intake.dispatch import KanbanIO

    db, host = _kanban(tmp_path, monkeypatch)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 71001)
    with kbc.connect_closing(db) as conn:
        tid = kb.create_task(conn, title="review", assignee="boris_on_demand")
        assert kb.claim_task(conn, tid, claimer=f"{host}:w1") is not None
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (71001, tid))
        conn.commit()
        kbd._record_worker_exit(71000, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE << 8)
        assert kbd.detect_crashed_workers(conn) == []
    adapter = KanbanIO(db)
    assert adapter.lane_status(tid) == "running"
    assert adapter.lane_authentication_failed(tid) is False


def test_recovered_run_does_not_inherit_an_old_authentication_failure(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from boris_intake.dispatch import KanbanIO

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "kanban.db"
    with kbc.connect_closing(db) as conn:
        tid = kb.create_task(conn, title="review", assignee="boris_on_demand")
        kb.claim_task(conn, tid, claimer="test:1")
        kb.block_task(conn, tid, reason="PROVIDER_AUTHENTICATION_REQUIRED: missing credentials")
        assert KanbanIO(db).lane_authentication_failed(tid)
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid, claimer="test:2")
    assert not KanbanIO(db).lane_authentication_failed(tid)
