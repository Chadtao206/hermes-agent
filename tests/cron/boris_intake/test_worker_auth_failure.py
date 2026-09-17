from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("relogin,stale_run", [(True, False), (False, False), (True, True)])
def test_credential_failure_blocks_only_the_bound_run_needing_reauthentication(
    tmp_path, monkeypatch, relogin, stale_run
):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.auth import AuthError
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from boris_intake.dispatch import KanbanIO

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    with kbc.connect_closing(db) as conn:
        tid = kb.create_task(conn, title="review", assignee="boris_on_demand")
        kb.claim_task(conn, tid, claimer="test:1")
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 1 if stale_run else run_id))

    def unavailable(**kwargs):
        raise AuthError("No Codex credentials stored", provider="openai-codex",
                        code="codex_auth_missing", relogin_required=relogin)

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", unavailable)
    shell = SimpleNamespace(requested_provider="openai-codex", _explicit_api_key=None,
                            _explicit_base_url=None, _resolve_fallback_runtime=lambda exc: None)
    assert CLIAgentSetupMixin._ensure_runtime_credentials(shell) is False
    adapter = KanbanIO(db)
    if relogin and not stale_run:
        assert adapter.lane_status(tid) == "blocked"
        assert adapter.lane_authentication_failed(tid) is True
        with kbc.connect_closing(db) as conn:
            assert not kb.claim_task(conn, tid, claimer="test:retry")
    else:
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
