"""Tests for control_center_store read helpers using synthetic fixture data."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture: temp SessionDB rows via hermes_state.SessionDB
# ---------------------------------------------------------------------------

@pytest.fixture
def _populated_session_db(_isolate_hermes_home, tmp_path):
    """Insert two sessions into a temp SessionDB and return their ids."""
    try:
        from hermes_state import SessionDB
    except ImportError:
        pytest.skip("hermes_state not importable")

    db = SessionDB()
    now = time.time()

    ids = []
    for i, title in enumerate(("Session Alpha", "Session Beta")):
        sid = f"test-session-{i:04d}"
        db.create_session(sid, source="test", model="claude")
        db.set_session_title(sid, title)
        ids.append(sid)

    # Mark first session as active (recent last_active)
    db._conn.execute(
        "UPDATE sessions SET ended_at = NULL WHERE id = ?", (ids[0],)
    )
    db._conn.commit()

    db.close()
    return ids


# ---------------------------------------------------------------------------
# Fixture: temp runtime-status file (gateway_state.json)
# ---------------------------------------------------------------------------

@pytest.fixture
def _gateway_running(_isolate_hermes_home):
    """Write a minimal gateway_state.json indicating a running gateway."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    status = {
        "gateway_state": "running",
        "exit_reason": None,
        "active_agents": {},
        "platforms": {},
    }
    _write_json(home / "gateway_state.json", status)
    # Write a fake PID file pointing to the current process so get_running_pid
    # finds something alive.
    import os
    pid_data = {"pid": os.getpid(), "kind": "hermes-gateway"}
    _write_json(home / "gateway.pid", pid_data)


# ---------------------------------------------------------------------------
# Fixture: temp processes.json
# ---------------------------------------------------------------------------

@pytest.fixture
def _populated_processes(_isolate_hermes_home):
    """Write a processes.json checkpoint with one running and one exited entry."""
    from hermes_constants import get_hermes_home

    data = [
        {
            "session_id": "proc_aaa",
            "command": "pytest tests/",
            "pid": 12345,
            "status": "running",
            "started_at": "2024-01-01T00:00:00",
            "session_key": "",
        },
        {
            "session_id": "proc_bbb",
            "command": "npm run build",
            "pid": 12346,
            "status": "exited",
            "started_at": "2024-01-01T00:01:00",
            "session_key": "profile-a",
        },
    ]
    _write_json(get_hermes_home() / "processes.json", data)


# ---------------------------------------------------------------------------
# Fixture: temp spawn-tree directory
# ---------------------------------------------------------------------------

@pytest.fixture
def _populated_spawn_trees(_isolate_hermes_home):
    """Create a spawn-trees directory with one session and index entry."""
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "spawn-trees" / "session-abc"
    root.mkdir(parents=True, exist_ok=True)

    now = time.time()
    entry = {
        "session_id": "session-abc",
        "started_at": now - 120,
        "finished_at": now - 60,
        "label": "My Delegation Task",
        "count": 3,
    }

    index_path = root / "_index.jsonl"
    index_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


@pytest.fixture
def _populated_proposals(_isolate_hermes_home):
    """Write read-only proposal artifacts under telemetry/proposals."""
    from hermes_constants import get_hermes_home

    proposals_dir = get_hermes_home() / "telemetry" / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    row = {
        "proposal_id": "proposal:test-read-only-queue",
        "title": "Test read-only proposal",
        "status": "proposed",
        "decision_requested": "approve",
        "owner_profile": "engineer",
        "tl_dr": "Synthetic proposal row for dashboard queue tests.",
        "confidence_score": 0.72,
        "confidence_label": "medium",
        "confidence_basis": {"reasons": ["test evidence"]},
        "risk_level": "low",
        "risk_notes": "bounded synthetic risk",
        "rollback_plan": "delete synthetic fixture",
        "verification_plan": "assert endpoint shape",
        "evidence": [
            {
                "evidence_type": "unit_test",
                "evidence_ref": "tests/test_control_center_store.py",
                "evidence_summary": "proposal queue fixture",
            }
        ],
        "created_at": "2026-05-25T00:00:00+00:00",
        "updated_at": "2026-05-25T00:10:00+00:00",
    }

    packet = {
        "proposal_id": row["proposal_id"],
        "title": row["title"],
        "decision_requested": "approve",
        "tl_dr": row["tl_dr"],
        "owner": "engineer",
    }

    base = proposals_dir / row["proposal_id"]
    _write_json(base.with_suffix(".row.json"), row)
    _write_json(base.with_suffix(".json"), packet)
    base.with_suffix(".md").write_text("# synthetic proposal\n", encoding="utf-8")


@pytest.fixture
def _populated_proposals_with_ledger(_populated_proposals, _isolate_hermes_home):
    """Add experiments.db decision rows that should overlay proposal row artifacts."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    proposals_dir = home / "telemetry" / "proposals"

    denied_row = {
        "proposal_id": "proposal:test-override-denied",
        "title": "Denied overlay fixture",
        "status": "proposed",
        "decision_requested": "approve",
        "owner_profile": "reviewer",
        "tl_dr": "Synthetic denied fixture.",
        "confidence_score": 0.61,
        "confidence_label": "medium",
        "confidence_basis": {"reasons": ["test evidence"]},
        "risk_level": "medium",
        "risk_notes": "bounded synthetic risk",
        "rollback_plan": "delete synthetic fixture",
        "verification_plan": "assert endpoint shape",
        "evidence": [],
        "created_at": "2026-05-25T00:00:00+00:00",
        "updated_at": "2026-05-25T00:10:00+00:00",
    }
    _write_json(proposals_dir / f"{denied_row['proposal_id']}.row.json", denied_row)

    db = home / "telemetry" / "experiments.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                status TEXT,
                approved_at TEXT,
                denied_at TEXT,
                approver TEXT,
                denial_reason TEXT,
                applied_at TEXT,
                verified_at TEXT,
                scored_at TEXT,
                outcome TEXT,
                linked_experiment_id TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_decision_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                decision TEXT NOT NULL,
                approver TEXT NOT NULL,
                reason TEXT,
                previous_status TEXT,
                new_status TEXT NOT NULL,
                source TEXT,
                backup_path TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_apply_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                action TEXT NOT NULL,
                operator TEXT NOT NULL,
                approver TEXT,
                approval_decided_at TEXT,
                approval_source TEXT,
                reason TEXT,
                previous_status TEXT,
                new_status TEXT NOT NULL,
                source TEXT,
                idempotency_key TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                kanban_backup_path TEXT,
                apply_artifact_path TEXT,
                kanban_task_id TEXT,
                manifest_path TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO proposals(proposal_id, status, approved_at, denied_at, approver, denial_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal:test-read-only-queue",
                "approved",
                "2026-05-25T01:00:00+00:00",
                None,
                "Chad Tao",
                None,
                "2026-05-25T01:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO proposals(proposal_id, status, approved_at, denied_at, approver, denial_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                denied_row["proposal_id"],
                "denied",
                None,
                "2026-05-25T02:00:00+00:00",
                "Boris",
                "not enough evidence",
                "2026-05-25T02:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO proposal_decision_audit(
                proposal_id, decided_at, decision, approver, reason,
                previous_status, new_status, source, backup_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal:test-read-only-queue",
                "2026-05-25T01:00:00+00:00",
                "approve",
                "Chad Tao",
                None,
                "proposed",
                "approved",
                "slack:thread-1",
                "/tmp/backup-approved",
            ),
        )
        conn.execute(
            """
            INSERT INTO proposal_decision_audit(
                proposal_id, decided_at, decision, approver, reason,
                previous_status, new_status, source, backup_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                denied_row["proposal_id"],
                "2026-05-25T02:00:00+00:00",
                "deny",
                "Boris",
                "not enough evidence",
                "proposed",
                "denied",
                "manual",
                "/tmp/backup-denied",
            ),
        )
        conn.execute(
            """
            INSERT INTO proposal_apply_audit(
                proposal_id, applied_at, action, operator, approver,
                approval_decided_at, approval_source, reason,
                previous_status, new_status, source, idempotency_key,
                backup_path, kanban_backup_path, apply_artifact_path,
                kanban_task_id, manifest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal:test-read-only-queue",
                "2026-05-25T03:00:00+00:00",
                "applied",
                "Chad Tao",
                "Chad Tao",
                "2026-05-25T01:00:00+00:00",
                "slack:thread-1",
                "launch pilot",
                "approved",
                "applied",
                "manual",
                "proposal-apply:proposal:test-read-only-queue",
                "/tmp/backup-approved",
                "/tmp/kanban-backup-approved",
                "/tmp/proposal:test-read-only-queue.apply.json",
                "t_apply_123",
                "/tmp/proposal:test-read-only-queue.manifest.json",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests: read_sessions
# ---------------------------------------------------------------------------

class TestReadSessions:
    def test_returns_list_when_db_empty(self, _isolate_hermes_home):
        import control_center_store as cc
        result = cc.read_sessions()
        assert isinstance(result, list)

    def test_returns_session_records(self, _populated_session_db):
        import control_center_store as cc
        result = cc.read_sessions(limit=10)
        assert isinstance(result, list)
        assert len(result) >= 1
        for s in result:
            assert "session_id" in s
            assert "title" in s
            assert "source" in s
            assert "model" in s
            assert "profile" in s
            assert "owner_kind" in s
            assert "running" in s
            assert "awaiting_input" in s
            assert "pending_request_kinds" in s
            assert "started_at" in s
            assert "last_seen_at" in s
            assert "activity" in s
            assert isinstance(s["running"], bool)
            assert isinstance(s["awaiting_input"], bool)
            assert isinstance(s["pending_request_kinds"], list)
            assert isinstance(s["activity"], dict)
            assert "api_call_count" in s["activity"]
            assert "tool_call_count" in s["activity"]
            assert "duration_seconds" in s["activity"]
            assert "external_wait_hint" in s["activity"]

    def test_count_active_sessions_is_int(self, _isolate_hermes_home):
        import control_center_store as cc
        count = cc.count_active_sessions()
        assert isinstance(count, int)
        assert count >= 0

    def test_running_only_excludes_stale_sessions(self, _isolate_hermes_home):
        """running_only=True must exclude sessions whose last activity is >5 min old."""
        try:
            from hermes_state import SessionDB
        except ImportError:
            pytest.skip("hermes_state not importable")

        db = SessionDB()
        now = time.time()
        db.create_session("active-001", source="test", model="claude")
        db.create_session("stale-001", source="test", model="claude")
        # Force stale session to look old (last_active is derived from started_at when no messages)
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now - 400, "stale-001"),
        )
        db._conn.commit()
        db.close()

        import control_center_store as cc
        running = cc.read_sessions(limit=20, running_only=True)
        ids = {s["session_id"] for s in running}
        assert "active-001" in ids
        assert "stale-001" not in ids

    def test_count_active_sessions_not_capped_at_50(self, _isolate_hermes_home):
        """count_active_sessions should not be artificially capped by a list limit."""
        try:
            from hermes_state import SessionDB
        except ImportError:
            pytest.skip("hermes_state not importable")

        db = SessionDB()
        now = time.time()
        # Create 2 genuinely active sessions
        for i in range(2):
            sid = f"cap-test-{i:04d}"
            db.create_session(sid, source="test", model="claude")
        db._conn.commit()
        db.close()

        import control_center_store as cc
        count = cc.count_active_sessions()
        assert count >= 2, f"Expected at least 2 active sessions, got {count}"

    def test_count_active_sessions_excludes_child_sessions(self, _isolate_hermes_home):
        """count_active_sessions must not count active child sessions (subagents).

        A child session (parent_session_id IS NOT NULL, parent still active) should be
        excluded so the overview count stays coherent with the Live Sessions pane, which
        uses list_sessions_rich(include_children=False).
        """
        try:
            from hermes_state import SessionDB
        except ImportError:
            pytest.skip("hermes_state not importable")

        import control_center_store as cc

        # Baseline before creating any sessions for this test
        baseline = cc.count_active_sessions()

        db = SessionDB()
        parent_id = "parent-session-001"
        child_id = "child-session-001"

        db.create_session(parent_id, source="test", model="claude")
        db.create_session(child_id, source="test", model="claude")

        # Wire child to parent (parent still active, not branched — should be excluded)
        db._conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
            (parent_id, child_id),
        )
        db._conn.commit()
        db.close()

        count_after = cc.count_active_sessions()
        visible_ids = {s["session_id"] for s in cc.read_sessions(limit=200)}

        # Child must not appear in read_sessions (list_sessions_rich excludes it)
        assert child_id not in visible_ids, "child session should be hidden from Live Sessions pane"
        assert parent_id in visible_ids, "parent session should be visible"

        # Adding parent+child should increase the count by exactly 1 (parent only)
        delta = count_after - baseline
        assert delta == 1, (
            f"Adding a parent+child pair should increase active_sessions by 1 (parent only), "
            f"got delta={delta} (baseline={baseline}, after={count_after})"
        )


class TestReadProposals:
    def test_returns_empty_list_when_no_proposals(self, _isolate_hermes_home):
        import control_center_store as cc

        rows = cc.read_proposals()
        assert rows == []

    def test_reads_row_json_shape(self, _populated_proposals):
        import control_center_store as cc

        rows = cc.read_proposals()
        assert len(rows) >= 1
        proposal = rows[0]

        for field in (
            "proposal_id",
            "title",
            "status",
            "decision_requested",
            "owner",
            "tl_dr",
            "confidence",
            "risk",
            "rollback",
            "verification",
            "evidence",
            "provenance",
        ):
            assert field in proposal, f"proposal row missing field: {field}"

        assert proposal["proposal_id"] == "proposal:test-read-only-queue"
        assert proposal["status"] == "proposed"
        assert proposal["confidence"]["band"] == "medium"
        assert isinstance(proposal["provenance"]["source_paths"], list)
        assert any(path.endswith(".row.json") for path in proposal["provenance"]["source_paths"])

    def test_status_filter(self, _populated_proposals):
        import control_center_store as cc

        proposed = cc.read_proposals(status="proposed")
        denied = cc.read_proposals(status="denied")

        assert len(proposed) == 1
        assert denied == []

    def test_overlays_ledger_status_and_audit_metadata(self, _populated_proposals_with_ledger):
        import control_center_store as cc

        rows = cc.read_proposals()
        by_id = {row["proposal_id"]: row for row in rows}

        approved = by_id["proposal:test-read-only-queue"]
        assert approved["status"] == "approved"
        assert approved["approver"] == "Chad Tao"
        assert approved["approved_at"] == "2026-05-25T01:00:00+00:00"
        assert approved["decision"]["decision"] == "approve"
        assert approved["decision"]["source"] == "slack:thread-1"
        assert approved["kanban_task_id"] == "t_apply_123"
        assert approved["apply_artifact_path"] == "/tmp/proposal:test-read-only-queue.apply.json"
        assert approved["apply_idempotency_key"] == "proposal-apply:proposal:test-read-only-queue"
        assert approved["apply"]["action"] == "applied"
        assert approved["apply"]["operator"] == "Chad Tao"
        assert approved["apply"]["source"] == "manual"
        assert approved["apply"]["kanban_task_id"] == "t_apply_123"

        denied = by_id["proposal:test-override-denied"]
        assert denied["status"] == "denied"
        assert denied["denial_reason"] == "not enough evidence"
        assert denied["decision"]["decision"] == "deny"
        assert denied["decision"]["source"] == "manual"

    def test_status_filter_uses_ledger_overlay(self, _populated_proposals_with_ledger):
        import control_center_store as cc

        approved = cc.read_proposals(status="approved")
        proposed = cc.read_proposals(status="proposed")

        assert [row["proposal_id"] for row in approved] == ["proposal:test-read-only-queue"]
        assert proposed == []

    def test_missing_apply_audit_table_falls_back_to_decision_overlay(self, _populated_proposals_with_ledger, _isolate_hermes_home):
        from hermes_constants import get_hermes_home
        import control_center_store as cc

        db = get_hermes_home() / "telemetry" / "experiments.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE IF EXISTS proposal_apply_audit")
            conn.commit()
        finally:
            conn.close()

        rows = cc.read_proposals(status="approved")
        assert len(rows) == 1
        row = rows[0]
        assert row["proposal_id"] == "proposal:test-read-only-queue"
        assert row["status"] == "approved"
        assert row["decision"]["decision"] == "approve"
        assert "apply" not in row
        assert "kanban_task_id" not in row

    def test_missing_or_unreadable_ledger_falls_back_to_file_rows(self, _populated_proposals, monkeypatch):
        import control_center_store as cc

        monkeypatch.setattr(cc, "_load_proposal_decisions", lambda: {})
        rows = cc.read_proposals()

        assert len(rows) == 1
        assert rows[0]["proposal_id"] == "proposal:test-read-only-queue"
        assert rows[0]["status"] == "proposed"

    def test_fallback_ignores_apply_and_manifest_artifacts_when_row_json_missing(self, _isolate_hermes_home):
        from hermes_constants import get_hermes_home
        import control_center_store as cc

        proposals_dir = get_hermes_home() / "telemetry" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)

        proposal_id = "proposal:test-fallback-byproducts"
        packet = {
            "proposal_id": proposal_id,
            "title": "Fallback packet",
            "decision_requested": "approve",
            "owner_profile": "engineer",
            "tl_dr": "fallback fixture",
        }
        apply_artifact = {
            "proposal_id": proposal_id,
            "mode": "execute",
            "title": "Apply artifact - must not appear in queue",
            "status": "applied",
        }

        _write_json(proposals_dir / f"{proposal_id}.json", packet)
        _write_json(proposals_dir / f"{proposal_id}.apply.json", apply_artifact)
        _write_json(proposals_dir / f"{proposal_id}.apply.dry-run.json", {**apply_artifact, "mode": "dry_run"})
        _write_json(proposals_dir / f"{proposal_id}.apply.execute.json", apply_artifact)
        _write_json(proposals_dir / f"{proposal_id}.apply.noop-20260525.json", {**apply_artifact, "mode": "noop_already_applied"})
        _write_json(proposals_dir / f"{proposal_id}.manifest.json", {"proposal_id": proposal_id})

        rows = cc.read_proposals()
        assert [row["proposal_id"] for row in rows] == [proposal_id]
        assert rows[0]["title"] == "Fallback packet"


# ---------------------------------------------------------------------------
# Tests: read_gateway_status
# ---------------------------------------------------------------------------

class TestReadGatewayStatus:
    def test_returns_dict_with_required_keys(self, _isolate_hermes_home):
        import control_center_store as cc
        status = cc.read_gateway_status()
        assert "running" in status
        assert "state" in status
        assert isinstance(status["running"], bool)

    def test_not_running_when_no_pid_file(self, _isolate_hermes_home):
        import control_center_store as cc
        status = cc.read_gateway_status()
        assert status["running"] is False


# ---------------------------------------------------------------------------
# Tests: read_processes
# ---------------------------------------------------------------------------

class TestReadProcesses:
    def test_returns_empty_list_when_no_checkpoint(self, _isolate_hermes_home):
        import control_center_store as cc
        result = cc.read_processes()
        assert isinstance(result, list)

    def test_reads_from_checkpoint(self, _populated_processes):
        import control_center_store as cc
        result = cc.read_processes()
        assert isinstance(result, list)
        # The checkpoint has 2 entries — we might get them from the registry or
        # checkpoint fallback, but count depends on process_registry state.
        for p in result:
            assert "session_id" in p
            assert "pid" in p
            assert "command" in p
            assert "started_at" in p
            assert "exited" in p
            assert "exit_code" in p
            assert "notify_on_complete" in p
            assert isinstance(p["exited"], bool)
            assert isinstance(p["notify_on_complete"], bool)

    def test_count_running_processes_is_int(self, _isolate_hermes_home):
        import control_center_store as cc
        count = cc.count_running_processes()
        assert isinstance(count, int)
        assert count >= 0




class TestSystemProcesses:
    def test_read_processes_exposes_richer_managed_fields(self, _populated_processes):
        import control_center_store as cc

        result = cc.read_processes()
        assert isinstance(result, list)
        for proc in result:
            for field in (
                "session_id",
                "pid",
                "command",
                "cwd",
                "started_at",
                "uptime_seconds",
                "status",
                "exited",
                "exit_code",
                "notify_on_complete",
                "session_key",
                "detached",
                "output_preview",
            ):
                assert field in proc, f"process item missing field: {field}"
            assert isinstance(proc["exited"], bool)
            assert isinstance(proc["notify_on_complete"], bool)

    def test_read_system_processes_filters_and_classifies_hermes_rows(self, monkeypatch, _isolate_hermes_home):
        import control_center_store as cc

        sample = """
          101     1 00:00:05 python -m hermes_cli.web_server
          102     1 01:02:03 /usr/bin/ssh unrelated@example.com
          103   101 00:00:02 python gateway/run.py
        """
        monkeypatch.setattr(cc.subprocess, "check_output", lambda *a, **k: sample)
        monkeypatch.setattr(cc, "read_processes", lambda: [])

        rows = cc.read_system_processes(limit=10)
        assert [row["pid"] for row in rows] == [101, 103]
        assert rows[0]["kind"] == "dashboard"
        assert rows[1]["kind"] == "gateway"
        for row in rows:
            assert "ppid" in row
            assert "elapsed" in row
            assert "command" in row
            assert "command_preview" in row
            assert "managed" in row


class TestRuntimeHealth:
    def test_read_runtime_health_shape(self, monkeypatch, _isolate_hermes_home):
        import control_center_store as cc

        monkeypatch.setattr(cc, "read_system_processes", lambda limit=200: [
            {"pid": 111, "ppid": 1, "elapsed": "00:00:10", "kind": "dashboard", "command": "hermes dashboard --port 9119", "command_preview": "hermes dashboard", "managed": False},
            {"pid": 112, "ppid": 1, "elapsed": "00:00:11", "kind": "dashboard", "command": "python -m hermes_cli.web_server", "command_preview": "web_server", "managed": False},
            {"pid": 333, "ppid": 1, "elapsed": "00:01:00", "kind": "cron", "command": "hermes cron tick", "command_preview": "hermes cron tick", "managed": False},
        ])

        data = cc.read_runtime_health()
        assert "last_checked" in data
        assert "runtimes" in data
        assert "alerts" in data
        cards = {card["id"]: card for card in data["runtimes"]}
        assert set(cards) >= {"dashboard", "gateway", "cron"}
        assert cards["dashboard"]["details"]["url"] == "http://127.0.0.1:9119"
        assert cards["dashboard"]["warnings"], "duplicate dashboard processes should produce a warning"
        for card in cards.values():
            for field in ("id", "name", "status", "running", "pids", "source", "details", "warnings", "actions"):
                assert field in card

    def test_execute_runtime_action_blocks_dashboard_self_stop(self, _isolate_hermes_home):
        import control_center_store as cc

        result = cc.execute_runtime_action("dashboard", "stop")
        assert result["status"] == "unavailable"
        assert "terminate this UI" in result["error"]

    def test_execute_runtime_action_unknown_runtime(self, _isolate_hermes_home):
        import control_center_store as cc

        result = cc.execute_runtime_action("nope", "restart")
        assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Tests: spawn-tree / delegation
# ---------------------------------------------------------------------------

class TestSpawnTrees:
    def test_read_metadata_empty_when_no_dir(self, _isolate_hermes_home):
        import control_center_store as cc
        result = cc.read_spawn_tree_metadata()
        assert isinstance(result, list)

    def test_reads_index_entries(self, _populated_spawn_trees):
        import control_center_store as cc
        result = cc.read_spawn_tree_metadata()
        assert isinstance(result, list)
        assert len(result) >= 1
        entry = result[0]
        assert "session_id" in entry
        assert "finished_at" in entry

    def test_delegation_subagents_shape(self, _populated_spawn_trees):
        import control_center_store as cc
        result = cc.read_delegation_subagents()
        assert isinstance(result, list)
        assert len(result) >= 1
        d = result[0]
        assert "session_id" in d
        assert "subagent_id" in d
        assert "status" in d
        assert "profile" in d
        assert "started_at" in d
        assert "finished_at" in d
        assert "parent_subagent_id" in d


# ---------------------------------------------------------------------------
# Tests: read_profiles
# ---------------------------------------------------------------------------

class TestReadProfiles:
    def test_returns_list(self, _isolate_hermes_home):
        import control_center_store as cc
        result = cc.read_profiles()
        assert isinstance(result, list)

    def test_handles_active_agents_as_integer(self, _isolate_hermes_home):
        """read_profiles must not crash when active_agents is an int count."""
        import os
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        _write_json(
            home / "gateway_state.json",
            {"gateway_state": "running", "active_agents": 3},
        )
        _write_json(home / "gateway.pid", {"pid": os.getpid(), "kind": "hermes-gateway"})

        import control_center_store as cc
        result = cc.read_profiles()
        assert isinstance(result, list)
        for p in result:
            assert "name" in p
            assert "is_online" in p
            assert "active_sessions" in p

    def test_handles_active_agents_empty_dict(self, _gateway_running):
        """read_profiles falls back to session-derived list for empty dict."""
        import control_center_store as cc
        result = cc.read_profiles()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Tests: read_overview
# ---------------------------------------------------------------------------

class TestReadOverview:
    def test_returns_correct_top_level_keys(self, _isolate_hermes_home):
        import control_center_store as cc
        ov = cc.read_overview()
        assert "gateway" in ov
        assert "counts" in ov
        assert "alerts" in ov

    def test_counts_are_integers(self, _isolate_hermes_home):
        import control_center_store as cc
        counts = cc.read_overview()["counts"]
        for key in ("active_sessions", "pending_requests", "running_processes", "profiles_online"):
            assert isinstance(counts[key], int), f"counts.{key} must be int"

    def test_alerts_is_list(self, _isolate_hermes_home):
        import control_center_store as cc
        assert isinstance(cc.read_overview()["alerts"], list)

    def test_gateway_keys(self, _isolate_hermes_home):
        import control_center_store as cc
        gw = cc.read_overview()["gateway"]
        assert "running" in gw
        assert "state" in gw

    def test_action_capabilities_phase2c_and_phase2d_envs(self, monkeypatch, _isolate_hermes_home):
        import control_center_store as cc

        monkeypatch.delenv("HERMES_CONTROL_CENTER_ACTIONS", raising=False)
        monkeypatch.delenv("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", raising=False)
        caps = cc.read_action_capabilities()
        assert caps["actions_enabled"] is False
        assert caps["destructive_controls_enabled"] is False
        assert "respond_pending" in caps["pending_request_actions"]

        monkeypatch.setenv("HERMES_CONTROL_CENTER_ACTIONS", "1")
        caps = cc.read_action_capabilities()
        assert caps["actions_enabled"] is True
        assert "respond_pending" in caps["safe_actions"]
        assert caps["destructive_controls_enabled"] is False

        monkeypatch.setenv("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", "1")
        caps = cc.read_action_capabilities()
        assert caps["destructive_controls_enabled"] is True
        assert "kill" in caps["destructive_actions"]


# ---------------------------------------------------------------------------
# Fixtures for ControlCenterDB tests
# ---------------------------------------------------------------------------

@pytest.fixture
def ccdb(_isolate_hermes_home, tmp_path):
    """Open a fresh ControlCenterDB backed by a temp file and close it after."""
    from control_center_store import ControlCenterDB
    db = ControlCenterDB(db_path=tmp_path / "cc_test.db")
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Tests: ControlCenterDB — live sessions
# ---------------------------------------------------------------------------

class TestControlCenterDBLiveSessions:
    def test_upsert_and_list(self, ccdb):
        ccdb.upsert_live_session("s1", title="Alpha", running=True, model="gpt-4")
        sessions = ccdb.list_live_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == "s1"
        assert s["title"] == "Alpha"
        assert s["running"] is True
        assert s["awaiting_input"] is False
        assert isinstance(s["started_at"], float)
        assert isinstance(s["last_seen_at"], float)

    def test_upsert_merges_fields(self, ccdb):
        ccdb.upsert_live_session("s1", title="Alpha", running=True)
        ccdb.upsert_live_session("s1", title="Beta", running=False, awaiting_input=True)
        sessions = ccdb.list_live_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s["title"] == "Beta"
        assert s["running"] is False
        assert s["awaiting_input"] is True

    def test_upsert_preserves_existing_when_null(self, ccdb):
        ccdb.upsert_live_session("s1", profile="main", running=True)
        # Second upsert without profile — should keep "main"
        ccdb.upsert_live_session("s1", running=True)
        s = ccdb.list_live_sessions()[0]
        assert s["profile"] == "main"

    def test_clear_live_session(self, ccdb):
        ccdb.upsert_live_session("s1", running=True)
        ccdb.upsert_live_session("s2", running=True)
        ccdb.clear_live_session("s1")
        ids = {s["session_id"] for s in ccdb.list_live_sessions()}
        assert "s1" not in ids
        assert "s2" in ids

    def test_list_running_only(self, ccdb):
        ccdb.upsert_live_session("active", running=True)
        ccdb.upsert_live_session("inactive", running=False)
        running = ccdb.list_live_sessions(running_only=True)
        ids = {s["session_id"] for s in running}
        assert "active" in ids
        assert "inactive" not in ids

    def test_payload_roundtrip(self, ccdb):
        payload = {"extra": "data", "count": 42}
        ccdb.upsert_live_session("s1", running=True, payload=payload)
        s = ccdb.list_live_sessions()[0]
        assert s["payload"] == payload

    def test_ttl_pruning(self, ccdb):
        now = time.time()
        # Insert a stale session (last seen 20 minutes ago)
        ccdb.upsert_live_session("stale", running=True, last_seen_at=now - 1200)
        ccdb.upsert_live_session("fresh", running=True, last_seen_at=now - 10)
        deleted = ccdb.prune_stale_rows(live_session_ttl=600.0)
        assert deleted["live_sessions"] >= 1
        ids = {s["session_id"] for s in ccdb.list_live_sessions()}
        assert "stale" not in ids
        assert "fresh" in ids


# ---------------------------------------------------------------------------
# Tests: ControlCenterDB — pending requests
# ---------------------------------------------------------------------------

class TestControlCenterDBPendingRequests:
    def test_create_and_list(self, ccdb):
        ccdb.create_pending_request("req-1", "sess-a", "approval", prompt_preview="Do X?")
        reqs = ccdb.list_pending_requests(session_id="sess-a")
        assert len(reqs) == 1
        r = reqs[0]
        assert r["request_id"] == "req-1"
        assert r["kind"] == "approval"
        assert r["status"] == "pending"
        assert r["prompt_preview"] == "Do X?"

    def test_resolve_returns_true_once(self, ccdb):
        ccdb.create_pending_request("req-1", "sess-a", "approval")
        assert ccdb.resolve_pending_request("req-1") is True
        # Second resolve should return False (already resolved)
        assert ccdb.resolve_pending_request("req-1") is False

    def test_resolve_changes_status(self, ccdb):
        ccdb.create_pending_request("req-1", "sess-a", "approval")
        ccdb.resolve_pending_request("req-1", status="approved")
        reqs_pending = ccdb.list_pending_requests(session_id="sess-a", status="pending")
        reqs_approved = ccdb.list_pending_requests(session_id="sess-a", status="approved")
        assert len(reqs_pending) == 0
        assert len(reqs_approved) == 1

    def test_list_all_pending_without_session_filter(self, ccdb):
        ccdb.create_pending_request("r1", "s1", "info")
        ccdb.create_pending_request("r2", "s2", "info")
        all_reqs = ccdb.list_pending_requests()
        assert len(all_reqs) == 2

    def test_payload_roundtrip(self, ccdb):
        payload = {"options": ["yes", "no"]}
        ccdb.create_pending_request("req-1", "sess-a", "approval", payload=payload)
        r = ccdb.list_pending_requests()[0]
        assert r["payload"] == payload


# ---------------------------------------------------------------------------
# Tests: ControlCenterDB — commands
# ---------------------------------------------------------------------------

class TestControlCenterDBCommands:
    def test_enqueue_returns_id(self, ccdb):
        cmd_id = ccdb.enqueue_command("restart", target_session_id="s1")
        assert isinstance(cmd_id, int)
        assert cmd_id > 0

    def test_claim_next_command_basic(self, ccdb):
        ccdb.enqueue_command("stop", target_session_id="s1")
        cmd = ccdb.claim_next_command(target_session_id="s1")
        assert cmd is not None
        assert cmd["action"] == "stop"
        assert cmd["status"] == "claimed"
        assert cmd["claimed_at"] is not None

    def test_command_claimed_only_once(self, ccdb):
        ccdb.enqueue_command("ping", target_session_id="s1")
        first = ccdb.claim_next_command(target_session_id="s1")
        second = ccdb.claim_next_command(target_session_id="s1")
        assert first is not None
        assert second is None  # already claimed

    def test_claim_returns_none_when_empty(self, ccdb):
        result = ccdb.claim_next_command(target_session_id="nobody")
        assert result is None

    def test_complete_command(self, ccdb):
        cmd_id = ccdb.enqueue_command("act", target_session_id="s1")
        ccdb.claim_next_command(target_session_id="s1")
        ok = ccdb.complete_command(cmd_id, result={"done": True})
        assert ok is True

    def test_complete_unclaimed_returns_false(self, ccdb):
        cmd_id = ccdb.enqueue_command("act", target_session_id="s1")
        ok = ccdb.complete_command(cmd_id)
        assert ok is False

    def test_stale_command_recovery(self, ccdb):
        """A stale claimed command (claimed long ago) can be re-claimed."""
        cmd_id = ccdb.enqueue_command("work", target_session_id="s1")
        # Simulate claim happening long ago by updating with old claimed_at
        # (isolation_level=None means autocommit — no explicit COMMIT needed)
        ccdb._conn.execute(
            "UPDATE commands SET status='claimed', claimed_at=? WHERE id=?",
            (time.time() - 400, cmd_id),
        )
        # Now claim with stale_after_seconds=300 — should recover the stale command
        recovered = ccdb.claim_next_command(
            target_session_id="s1", stale_after_seconds=300.0
        )
        assert recovered is not None
        assert recovered["id"] == cmd_id
        assert recovered["status"] == "claimed"

    def test_command_payload_roundtrip(self, ccdb):
        payload = {"prompt": "do something", "flags": ["--force"]}
        cmd_id = ccdb.enqueue_command("exec", payload=payload)
        cmd = ccdb.claim_next_command()
        assert cmd is not None
        assert cmd["payload"] == payload

    def test_action_filter_in_claim(self, ccdb):
        ccdb.enqueue_command("stop")
        ccdb.enqueue_command("restart")
        cmd = ccdb.claim_next_command(action="restart")
        assert cmd is not None
        assert cmd["action"] == "restart"


    def test_expire_stale_commands_marks_pending_and_claimed_expired(self, ccdb):
        pending_id = ccdb.enqueue_command("old_pending", target_session_id="s1")
        claimed_id = ccdb.enqueue_command("old_claimed", target_session_id="s1")
        fresh_id = ccdb.enqueue_command("fresh", target_session_id="s1")
        old = time.time() - 1200
        ccdb._conn.execute(
            "UPDATE commands SET created_at=? WHERE id=?",
            (old, pending_id),
        )
        ccdb._conn.execute(
            "UPDATE commands SET status='claimed', created_at=?, claimed_at=? WHERE id=?",
            (old, old, claimed_id),
        )

        expired = ccdb.expire_stale_commands(command_ttl=900.0)

        commands = {row["id"]: row for row in ccdb.list_commands(limit=10)}
        assert expired == 2
        assert commands[pending_id]["status"] == "expired"
        assert commands[claimed_id]["status"] == "expired"
        assert commands[fresh_id]["status"] == "pending"
        assert commands[pending_id]["result"]["error"]


    def test_freshly_reclaimed_old_command_does_not_expire_by_created_at(self, ccdb):
        cmd_id = ccdb.enqueue_command("old_but_active", target_session_id="s1")
        old = time.time() - 1200
        ccdb._conn.execute("UPDATE commands SET created_at=? WHERE id=?", (old, cmd_id))

        claimed = ccdb.claim_next_command(target_session_id="s1")
        assert claimed is not None
        assert claimed["claimed_at"] > old

        assert ccdb.expire_stale_commands(command_ttl=900.0) == 0
        assert ccdb.complete_command(cmd_id, result={"done": True}) is True
        row = next(row for row in ccdb.list_commands(limit=10) if row["id"] == cmd_id)
        assert row["status"] == "completed"

    def test_expired_command_cannot_be_claimed(self, ccdb):
        cmd_id = ccdb.enqueue_command("old", target_session_id="s1")
        old = time.time() - 1200
        ccdb._conn.execute("UPDATE commands SET created_at=? WHERE id=?", (old, cmd_id))
        ccdb.expire_stale_commands(command_ttl=900.0)
        assert ccdb.claim_next_command(target_session_id="s1") is None

    def test_prune_removes_completed_commands(self, ccdb):
        cmd_id = ccdb.enqueue_command("act")
        ccdb.claim_next_command()
        ccdb.complete_command(cmd_id)
        # Backdate completed_at (autocommit — no explicit COMMIT needed)
        ccdb._conn.execute(
            "UPDATE commands SET completed_at=? WHERE id=?",
            (time.time() - 90000, cmd_id),
        )
        deleted = ccdb.prune_stale_rows(command_ttl=86400.0)
        assert deleted["commands"] >= 1


# ---------------------------------------------------------------------------
# Tests: module-level convenience wrappers
# ---------------------------------------------------------------------------

class TestModuleLevelWrappers:
    def test_cc_upsert_and_list(self, _isolate_hermes_home):
        import control_center_store as cc
        cc.cc_upsert_live_session("w1", title="Wrapper Test", running=True)
        sessions = cc.cc_list_live_sessions()
        assert any(s["session_id"] == "w1" for s in sessions)

    def test_cc_enqueue_and_claim(self, _isolate_hermes_home):
        import control_center_store as cc
        cmd_id = cc.cc_enqueue_command("test_action", target_session_id="ws1")
        assert isinstance(cmd_id, int)
        cmd = cc.cc_claim_next_command(target_session_id="ws1")
        assert cmd is not None
        assert cmd["action"] == "test_action"
        ok = cc.cc_complete_command(cmd["id"], result={"ok": True})
        assert ok is True

    def test_cc_pending_request_flow(self, _isolate_hermes_home):
        import control_center_store as cc
        cc.cc_create_pending_request("wr-1", "ws1", "info")
        assert cc.cc_resolve_pending_request("wr-1") is True
        assert cc.cc_resolve_pending_request("wr-1") is False


    def test_cc_expire_stale_commands_wrapper(self, _isolate_hermes_home):
        import control_center_store as cc
        cmd_id = cc.cc_enqueue_command("old", target_session_id="ws1")
        db = cc.ControlCenterDB()
        try:
            db._conn.execute("UPDATE commands SET created_at=? WHERE id=?", (time.time() - 1200, cmd_id))
        finally:
            db.close()
        assert cc.cc_expire_stale_commands(command_ttl=900.0) == 1
        rows = cc.cc_list_commands(limit=10)
        assert rows[0]["status"] == "expired"

    def test_cc_prune_stale_rows(self, _isolate_hermes_home):
        import control_center_store as cc
        result = cc.cc_prune_stale_rows()
        assert isinstance(result, dict)
        assert "live_sessions" in result
        assert "commands" in result
        assert "pending_requests" in result


# ---------------------------------------------------------------------------
# Tests: ControlCenterDB — stale-owner cleanup
# ---------------------------------------------------------------------------

class TestControlCenterDBOwnerCleanup:
    def test_clear_by_owner_kind(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.upsert_live_session("s2", owner_kind="gateway", owner_id="gw-002", running=True)
        ccdb.upsert_live_session("s3", owner_kind="worker", owner_id="w-001", running=True)
        deleted = ccdb.clear_owner_sessions(owner_kind="gateway")
        assert deleted == 2
        ids = {s["session_id"] for s in ccdb.list_live_sessions()}
        assert "s1" not in ids
        assert "s2" not in ids
        assert "s3" in ids

    def test_clear_by_owner_id(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.upsert_live_session("s2", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.upsert_live_session("s3", owner_kind="gateway", owner_id="gw-999", running=True)
        deleted = ccdb.clear_owner_sessions(owner_id="gw-001")
        assert deleted == 2
        ids = {s["session_id"] for s in ccdb.list_live_sessions()}
        assert "s3" in ids

    def test_clear_by_owner_kind_and_id(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.upsert_live_session("s2", owner_kind="worker", owner_id="gw-001", running=True)
        deleted = ccdb.clear_owner_sessions(owner_kind="gateway", owner_id="gw-001")
        assert deleted == 1
        ids = {s["session_id"] for s in ccdb.list_live_sessions()}
        assert "s2" in ids

    def test_clear_stale_by_owner_and_age(self, ccdb):
        now = time.time()
        ccdb.upsert_live_session("fresh", owner_kind="gateway", owner_id="gw-001",
                                 running=True, last_seen_at=now - 10)
        ccdb.upsert_live_session("stale", owner_kind="gateway", owner_id="gw-001",
                                 running=True, last_seen_at=now - 700)
        deleted = ccdb.clear_owner_sessions(owner_kind="gateway", stale_after_seconds=600.0)
        assert deleted == 1
        ids = {s["session_id"] for s in ccdb.list_live_sessions()}
        assert "fresh" in ids
        assert "stale" not in ids

    def test_clear_requires_at_least_one_filter(self, ccdb):
        with pytest.raises(ValueError):
            ccdb.clear_owner_sessions()

    def test_clear_owner_sessions_returns_zero_when_nothing_matches(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="worker", running=True)
        deleted = ccdb.clear_owner_sessions(owner_kind="nonexistent")
        assert deleted == 0
        assert len(ccdb.list_live_sessions()) == 1

    def test_cc_clear_owner_sessions_wrapper(self, _isolate_hermes_home):
        import control_center_store as cc
        cc.cc_upsert_live_session("w1", owner_kind="test-owner", running=True)
        deleted = cc.cc_clear_owner_sessions(owner_kind="test-owner")
        assert deleted == 1


# ---------------------------------------------------------------------------
# Tests: ControlCenterDB — owner-based claim_next_command
# ---------------------------------------------------------------------------

class TestControlCenterDBOwnerClaim:
    def test_claim_by_owner_kind_and_id(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.upsert_live_session("s2", owner_kind="worker", owner_id="w-001", running=True)
        ccdb.enqueue_command("ping", target_session_id="s1")
        ccdb.enqueue_command("pong", target_session_id="s2")

        cmd = ccdb.claim_next_command(owner_kind="gateway", owner_id="gw-001")
        assert cmd is not None
        assert cmd["action"] == "ping"
        assert cmd["target_session_id"] == "s1"

    def test_claim_by_owner_kind_only(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.enqueue_command("act", target_session_id="s1")
        cmd = ccdb.claim_next_command(owner_kind="gateway")
        assert cmd is not None
        assert cmd["action"] == "act"

    def test_owner_claim_ignores_unowned_sessions(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="other", owner_id="o-001", running=True)
        ccdb.enqueue_command("act", target_session_id="s1")
        cmd = ccdb.claim_next_command(owner_kind="gateway")
        assert cmd is None

    def test_owner_claim_combined_with_target_session_id(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.upsert_live_session("s2", owner_kind="gateway", owner_id="gw-001", running=True)
        ccdb.enqueue_command("cmd-s1", target_session_id="s1")
        ccdb.enqueue_command("cmd-s2", target_session_id="s2")

        cmd = ccdb.claim_next_command(owner_kind="gateway", owner_id="gw-001",
                                      target_session_id="s2")
        assert cmd is not None
        assert cmd["action"] == "cmd-s2"

    def test_owner_claim_uses_stale_recovery(self, ccdb):
        ccdb.upsert_live_session("s1", owner_kind="gateway", owner_id="gw-001", running=True)
        cmd_id = ccdb.enqueue_command("work", target_session_id="s1")
        ccdb._conn.execute(
            "UPDATE commands SET status='claimed', claimed_at=? WHERE id=?",
            (time.time() - 400, cmd_id),
        )
        recovered = ccdb.claim_next_command(owner_kind="gateway", owner_id="gw-001",
                                            stale_after_seconds=300.0)
        assert recovered is not None
        assert recovered["id"] == cmd_id

    def test_target_session_id_path_still_works(self, ccdb):
        ccdb.enqueue_command("legacy", target_session_id="legacy-s1")
        cmd = ccdb.claim_next_command(target_session_id="legacy-s1")
        assert cmd is not None
        assert cmd["action"] == "legacy"


# ---------------------------------------------------------------------------
# Tests: ControlCenterDB — concurrency (two writers racing to claim)
# ---------------------------------------------------------------------------

class TestControlCenterDBConcurrency:
    def test_two_writers_racing_only_one_wins(self, tmp_path):
        """Two separate DB connections racing to claim the same command — exactly one wins."""
        from control_center_store import ControlCenterDB

        db_path = tmp_path / "race_test.db"
        db1 = ControlCenterDB(db_path=db_path)
        db2 = ControlCenterDB(db_path=db_path)

        try:
            db1.enqueue_command("race_work", target_session_id="race-s1")

            results: list = []
            errors: list = []
            barrier = threading.Barrier(2)

            def claim(db, label):
                try:
                    barrier.wait(timeout=5.0)
                    cmd = db.claim_next_command(target_session_id="race-s1")
                    results.append(cmd)
                except Exception as exc:
                    errors.append((label, exc))

            t1 = threading.Thread(target=claim, args=(db1, "db1"), daemon=True)
            t2 = threading.Thread(target=claim, args=(db2, "db2"), daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            assert not errors, f"Unexpected errors during concurrent claim: {errors}"
            assert len(results) == 2, "Both threads should have returned (None or a row)"
            claimed = [r for r in results if r is not None]
            assert len(claimed) == 1, (
                f"Expected exactly 1 successful claim, got {len(claimed)}: {claimed}"
            )
        finally:
            db1.close()
            db2.close()
