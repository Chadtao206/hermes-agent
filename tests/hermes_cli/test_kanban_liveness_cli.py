"""WS6 Task 2: `hermes kanban liveness --json` reports board-liveness signals."""
import argparse
import json

from hermes_cli import kanban


def _run(argv):
    parent = argparse.ArgumentParser()
    psub = parent.add_subparsers(dest="top")
    kanban.build_parser(psub)
    args = parent.parse_args(argv)
    return kanban.kanban_command(args)


def test_liveness_cli_emits_json_on_fresh_board(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    rc = _run(["kanban", "liveness", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "oldest_ready_age_seconds" in data
    assert data["oldest_ready_age_seconds"] == 0  # empty/fresh board


def test_liveness_cli_reports_ready_age(tmp_path, monkeypatch, capsys):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    from hermes_cli import kanban_db as kb
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    tid = kb.create_task(conn, title="x", assignee="engineer")
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    rc = _run(["kanban", "liveness", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    # A ready task exists → non-negative age reported (>=0, typically small).
    assert data["oldest_ready_age_seconds"] >= 0
    assert "oldest_stale_running_age_seconds" in data
