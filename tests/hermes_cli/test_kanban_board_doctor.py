from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_board_doctor as doctor


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_readonly_connect_does_not_create_missing_db(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(Exception):
        kb.connect(missing, readonly=True)
    assert not missing.exists()




def test_doctor_reports_unreadable_db_without_deleting_sidecars(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db = home / "kanban.db"
    wal = home / "kanban.db-wal"
    shm = home / "kanban.db-shm"
    db.write_bytes(b"not a sqlite database")
    wal.write_bytes(b"stale wal marker")
    shm.write_bytes(b"stale shm marker")

    result = doctor.run_board_doctor()

    assert result["ok"] is False
    assert result["issues"][0]["kind"] == "db_invalid_header"
    assert "recover DB" in result["issues"][0]["action"]
    assert wal.read_bytes() == b"stale wal marker"
    assert shm.read_bytes() == b"stale shm marker"


def test_readonly_connect_does_not_initialize_schema_or_create_sidecars(tmp_path):
    db = tmp_path / "plain.db"
    raw = __import__("sqlite3").connect(db)
    raw.execute("CREATE TABLE external_table(id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    with kb.connect(db, readonly=True) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert tables == {"external_table"}
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()


def test_doctor_reports_orphan_links_and_stale_running_runs(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        orphan_child = kb.create_task(conn, title="orphan child", assignee="reviewer")
        blocked_child = kb.create_task(conn, title="blocked child", assignee="reviewer")
        kb.complete_task(conn, parent, summary="done")
        kb.block_task(conn, orphan_child, reason="waiting on missing parent")
        kb.block_task(conn, blocked_child, reason="waiting on remediation")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            ("t_missing", orphan_child),
        )
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            (parent, blocked_child),
        )
        run_id = conn.execute(
            """
            INSERT INTO task_runs(task_id, profile, status, worker_pid, started_at)
            VALUES (?, 'engineer', 'running', 999999, ?)
            """,
            (parent, 1),
        ).lastrowid
        assert run_id

    result = doctor.run_board_doctor()
    kinds = {i["kind"] for i in result["issues"]}
    assert "orphan_task_link" in kinds
    assert "blocked_with_completed_parents" in kinds
    assert "stale_running_run" in kinds


def test_doctor_cli_json(kanban_home):
    out = kc.run_slash("doctor --json")
    assert '"issues"' in out
    assert '"ok"' in out


def test_doctor_continues_for_noncritical_notifier_heartbeat_issue(kanban_home, monkeypatch):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="engineer")
        child = kb.create_task(conn, title="child", assignee="reviewer")
        kb.complete_task(conn, parent, summary="done")
        kb.block_task(conn, child, reason="waiting")
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, 'dependency')",
            (parent, child),
        )

    monkeypatch.setattr(
        doctor,
        "_quick_check",
        lambda path: {
            "severity": "warning",
            "kind": "notifier_heartbeat_integrity",
            "message": "non-critical",
            "action": "reset heartbeat telemetry",
        },
    )

    result = doctor.run_board_doctor()
    kinds = {issue["kind"] for issue in result["issues"]}
    assert result["ok"] is False
    assert "notifier_heartbeat_integrity" in kinds
    assert "blocked_with_completed_parents" in kinds

