from __future__ import annotations

from pathlib import Path

import hashlib
import sqlite3

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_board_doctor as doctor
from hermes_cli import kanban_db_repair as krepair


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_SOURCE",
        "_HERMES_GATEWAY",
        "HERMES_GATEWAY_SESSION",
    ):
        monkeypatch.delenv(name, raising=False)
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
    assert "repair-db" in result["issues"][0]["action"]
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


def test_doctor_ignores_corrupt_notifier_sidecar_when_board_db_is_clean(kanban_home):
    """Heartbeat sidecar corruption must not be classified as board DB corruption."""
    sidecar = kanban_home / "kanban_notifier_heartbeats.db"
    sidecar.write_bytes(b"not sqlite")

    result = doctor.run_board_doctor()

    assert result["ok"] is True
    assert result["issues"] == []
    assert result["reconcile_summary"]["action_count"] == 0



def test_snapshot_connect_tolerates_vanishing_wal_sidecars(kanban_home, monkeypatch):
    """WAL/SHM can disappear during snapshot copy after a live checkpoint."""
    db = kanban_home / "kanban.db"
    wal = db.with_name(db.name + "-wal")
    wal.write_bytes(b"transient wal marker")
    real_copy2 = kb.shutil.copy2

    def race_copy2(src, dst, *args, **kwargs):
        if Path(src) == wal:
            wal.unlink()
            raise FileNotFoundError(wal)
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(kb.shutil, "copy2", race_copy2)

    with kb.snapshot_connect(db) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    assert not wal.exists()



def test_doctor_reconcile_summary_surfaces_decision_actions_without_failing_health(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="implementation", assignee="engineer")
        assert kb.complete_task(conn, parent, summary="done")
        child = kb.create_task(conn, title="parked review", assignee="reviewer", parents=[parent])
        assert kb.schedule_task(conn, child, reason="park until operator reviews")
        conn.execute("UPDATE tasks SET created_at = created_at - 10 WHERE id = ?", (child,))

    result = doctor.run_board_doctor(ready_age_seconds=1)

    assert result["ok"] is True
    assert result["issues"] == []
    assert result["reconcile_summary"]["action_count"] >= 1
    assert result["reconcile_summary"]["wake_mode"] == "jensen_decision_required"
    assert result["reconcile_summary"]["kinds"]["scheduled_with_completed_parents_decision"] == 1


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_observability_slash_commands_do_not_init_or_mutate_main_db(kanban_home, monkeypatch):
    """Doctor/reconcile/metrics are observational; they must not call init_db."""
    db = kanban_home / "kanban.db"
    # Ensure any prior writable setup sidecars are gone before the observation smoke.
    for suffix in ("-wal", "-shm"):
        sidecar = db.with_name(db.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    before = _sha256(db)

    def fail_init(*args, **kwargs):
        raise AssertionError("observability command called init_db")

    monkeypatch.setattr(kb, "init_db", fail_init)

    for command in ("doctor --json", "reconcile --json", "metrics --json"):
        out = kc.run_slash(command)
        assert "observability command called init_db" not in out
        assert _sha256(db) == before
        assert not db.with_name(db.name + "-wal").exists()
        assert not db.with_name(db.name + "-shm").exists()


def test_repair_db_guard_requires_explicit_live_replacement_gates(kanban_home, tmp_path, monkeypatch):
    db = kanban_home / "kanban.db"
    candidate = tmp_path / "candidate.db"
    with sqlite3.connect(db) as src, sqlite3.connect(candidate) as dst:
        src.backup(dst)

    for suffix in ("-wal", "-shm"):
        sidecar = db.with_name(db.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    before = _sha256(db)

    def fail_init(*args, **kwargs):
        raise AssertionError("repair-db guard called init_db")

    monkeypatch.setattr(kb, "init_db", fail_init)

    out = kc.run_slash(f"repair-db --candidate {candidate} --install --json")

    assert "missing_confirmation" in out
    assert "--confirm-quiesced" in out
    assert "--confirm-freshness-checked" in out
    assert "repair-db guard called init_db" not in out
    assert _sha256(db) == before
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()


def test_repair_db_guard_runbook_only_is_safe_without_existing_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_SESSION_PLATFORM",
        "HERMES_SESSION_CHAT_ID",
        "HERMES_SESSION_SOURCE",
        "_HERMES_GATEWAY",
        "HERMES_GATEWAY_SESSION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    out = kc.run_slash("repair-db --json")

    assert "runbook_only" in out
    assert "kanban.db" in out
    assert not (home / "kanban.db").exists()


def test_repair_db_install_refuses_gateway_slack_context(kanban_home, tmp_path, monkeypatch):
    db = kanban_home / "kanban.db"
    candidate = tmp_path / "candidate.db"
    with sqlite3.connect(db) as src, sqlite3.connect(candidate) as dst:
        src.backup(dst)
    before = _sha256(db)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "C123")

    out = kc.run_slash(
        f"repair-db --candidate {candidate} --install "
        "--confirm-quiesced --confirm-freshness-checked --json"
    )

    assert "active_gateway_context_refused" in out
    assert "slack" in out
    assert _sha256(db) == before


def test_repair_db_install_blocks_writer_processes_even_after_confirmations(kanban_home, tmp_path, monkeypatch):
    db = kanban_home / "kanban.db"
    candidate = tmp_path / "candidate.db"
    with sqlite3.connect(db) as src, sqlite3.connect(candidate) as dst:
        src.backup(dst)
    before = _sha256(db)

    monkeypatch.setattr(krepair, "_writer_process_check", lambda: {
        "ok": False,
        "running_writers": [{"pid": 12345, "kind": "gateway", "command": "hermes gateway run"}],
        "errors": [],
    })

    out = kc.run_slash(
        f"repair-db --candidate {candidate} --install "
        "--confirm-quiesced --confirm-freshness-checked --json"
    )

    assert "writer_processes_or_unverified" in out
    assert "hermes gateway run" in out
    assert _sha256(db) == before


def test_repair_db_install_blocks_freshness_regression_without_override(kanban_home, tmp_path, monkeypatch):
    db = kanban_home / "kanban.db"
    conn = kb.connect()
    try:
        kb.create_task(conn, title="freshness baseline", assignee="engineer")
    finally:
        conn.close()

    candidate = tmp_path / "candidate-stale.db"
    with sqlite3.connect(db) as src, sqlite3.connect(candidate) as dst:
        src.backup(dst)
    with sqlite3.connect(candidate) as stale:
        stale.execute("DELETE FROM tasks")
        stale.commit()

    before = _sha256(db)
    monkeypatch.setattr(krepair, "_writer_process_check", lambda: {"ok": True, "running_writers": [], "errors": []})
    monkeypatch.setattr(
        krepair,
        "_open_handle_check",
        lambda paths: {"ok": True, "tool": "lsof", "checked_paths": [str(p) for p in paths], "open_handles": ""},
    )

    out = kc.run_slash(
        f"repair-db --candidate {candidate} --install "
        "--confirm-quiesced --confirm-freshness-checked --json"
    )

    assert "freshness_regression" in out
    assert "staler_count" in out
    assert _sha256(db) == before


def test_repair_db_install_allows_freshness_regression_with_override(kanban_home, tmp_path, monkeypatch):
    db = kanban_home / "kanban.db"
    conn = kb.connect()
    try:
        kb.create_task(conn, title="freshness baseline", assignee="engineer")
    finally:
        conn.close()

    candidate = tmp_path / "candidate-stale.db"
    with sqlite3.connect(db) as src, sqlite3.connect(candidate) as dst:
        src.backup(dst)
    with sqlite3.connect(candidate) as stale:
        stale.execute("DELETE FROM tasks")
        stale.commit()

    monkeypatch.setattr(krepair, "_writer_process_check", lambda: {"ok": True, "running_writers": [], "errors": []})
    monkeypatch.setattr(
        krepair,
        "_open_handle_check",
        lambda paths: {"ok": True, "tool": "lsof", "checked_paths": [str(p) for p in paths], "open_handles": ""},
    )

    out = kc.run_slash(
        f"repair-db --candidate {candidate} --install "
        "--confirm-quiesced --confirm-freshness-checked --allow-data-loss --json"
    )

    assert '"installed": true' in out
    assert "allow_data_loss_override" in out

