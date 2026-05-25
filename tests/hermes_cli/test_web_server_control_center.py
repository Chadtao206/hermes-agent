"""Tests for Control Center REST endpoints in hermes_cli.web_server."""

import json
import sqlite3
import time
from pathlib import Path

import pytest


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestControlCenterEndpoints:
    """Assert 200 status and correct envelope shape for every Control Center route."""

    @pytest.fixture(autouse=True)
    def _setup_client(self, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_overview_status_and_keys(self):
        resp = self.client.get("/api/control-center/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "gateway" in data
        assert "counts" in data
        assert "alerts" in data

    def test_overview_nested_gateway_keys(self):
        data = self.client.get("/api/control-center/overview").json()
        gateway = data["gateway"]
        assert "running" in gateway
        assert "state" in gateway

    def test_overview_counts_values_are_integers(self):
        data = self.client.get("/api/control-center/overview").json()
        counts = data["counts"]
        for key in ("active_sessions", "pending_requests", "running_processes", "profiles_online"):
            assert isinstance(counts[key], int), f"counts.{key} should be int"

    def test_overview_alerts_is_list(self):
        data = self.client.get("/api/control-center/overview").json()
        assert isinstance(data["alerts"], list)

    def test_overview_includes_phase1_status_domains(self):
        data = self.client.get("/api/control-center/overview").json()
        for key in ("kanban", "memory", "repos"):
            assert key in data, f"overview missing {key} status domain"
            assert "status" in data[key]
        assert "available" in data["kanban"]
        assert "available" in data["memory"]
        assert "hermes_source" in data["repos"]
        assert "control_plane" in data["repos"]


    def test_overview_includes_control_center_action_mode(self, monkeypatch):
        monkeypatch.delenv("HERMES_CONTROL_CENTER_ACTIONS", raising=False)
        data = self.client.get("/api/control-center/overview").json()
        mode = data["control_center"]
        assert mode["actions_enabled"] is False
        assert mode["mode"] == "read_only"
        assert "safe_session_actions" in mode
        assert "safe_process_actions" in mode
        assert mode["destructive_controls_enabled"] is False

    def test_overview_action_mode_reflects_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_CONTROL_CENTER_ACTIONS", "1")
        data = self.client.get("/api/control-center/overview").json()
        mode = data["control_center"]
        assert mode["actions_enabled"] is True
        assert mode["mode"] == "operator_actions_enabled"

    def test_overview_action_mode_includes_phase2c_and_phase2d_gates(self, monkeypatch):
        monkeypatch.setenv("HERMES_CONTROL_CENTER_ACTIONS", "1")
        monkeypatch.delenv("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", raising=False)

        data = self.client.get("/api/control-center/overview").json()
        mode = data["control_center"]
        assert "respond_pending" in mode["pending_request_actions"]
        assert "respond_pending" in mode["safe_actions"]
        assert mode["destructive_controls_enabled"] is False
        assert mode["destructive_controls_env_var"] == "HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS"

        monkeypatch.setenv("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", "1")
        data = self.client.get("/api/control-center/overview").json()
        assert data["control_center"]["destructive_controls_enabled"] is True

    def test_sessions_status_and_keys(self):
        resp = self.client.get("/api/control-center/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)

    def test_sessions_item_shape(self, _isolate_hermes_home):
        """Sessions items must have the ControlCenterLiveSession contract fields."""
        import control_center_store as cc

        cc.cc_upsert_live_session(
            "ws-test-001",
            owner_kind="tui",
            owner_id="pid-1",
            title="WS Test Session",
            source="test",
            model="claude-test",
            running=True,
            payload={"pending_request_kinds": []},
        )

        data = self.client.get("/api/control-center/sessions").json()
        sessions = data["sessions"]
        assert len(sessions) >= 1
        s = sessions[0]
        for field in ("session_id", "title", "source", "model", "profile",
                      "owner_kind", "running", "awaiting_input",
                      "pending_request_kinds", "started_at", "last_seen_at"):
            assert field in s, f"session item missing field: {field}"
        assert isinstance(s["running"], bool)
        assert isinstance(s["awaiting_input"], bool)
        assert isinstance(s["pending_request_kinds"], list)

    def test_pending_status_and_keys(self):
        resp = self.client.get("/api/control-center/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert "requests" in data
        assert isinstance(data["requests"], list)

    def test_pending_items_contract(self):
        """Pending endpoint returns [] but the envelope key must be 'requests'."""
        data = self.client.get("/api/control-center/pending").json()
        assert data["requests"] == []

    def test_proposals_status_and_keys(self, _isolate_hermes_home):
        from hermes_constants import get_hermes_home

        proposals_dir = get_hermes_home() / "telemetry" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "proposal_id": "proposal:test-web-endpoint",
            "title": "Test endpoint proposal",
            "status": "proposed",
            "decision_requested": "approve",
            "owner_profile": "ops",
            "tl_dr": "Endpoint fixture",
            "confidence_score": 0.55,
            "confidence_label": "medium",
            "confidence_basis": {"reasons": ["fixture"]},
            "risk_level": "low",
            "risk_notes": "test",
            "rollback_plan": "none",
            "verification_plan": "endpoint returns row",
            "evidence": [],
            "created_at": "2026-05-25T01:00:00+00:00",
            "updated_at": "2026-05-25T01:00:00+00:00",
        }
        _write_json(proposals_dir / "proposal:test-web-endpoint.row.json", row)

        resp = self.client.get("/api/control-center/proposals")
        assert resp.status_code == 200
        data = resp.json()
        assert "proposals" in data
        assert isinstance(data["proposals"], list)
        assert len(data["proposals"]) == 1

        proposal = data["proposals"][0]
        assert proposal["proposal_id"] == "proposal:test-web-endpoint"
        assert proposal["status"] == "proposed"
        assert proposal["owner"] == "ops"
        assert "source_paths" in proposal["provenance"]

    def test_proposals_status_filter(self, _isolate_hermes_home):
        from hermes_constants import get_hermes_home

        proposals_dir = get_hermes_home() / "telemetry" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            proposals_dir / "proposal:test-status-filter.row.json",
            {
                "proposal_id": "proposal:test-status-filter",
                "title": "Status filter fixture",
                "status": "approved",
                "decision_requested": "approve",
                "owner_profile": "reviewer",
                "tl_dr": "Status filter test",
                "confidence_score": 0.9,
                "confidence_label": "high",
                "confidence_basis": {},
                "risk_level": "low",
                "risk_notes": "",
                "rollback_plan": "",
                "verification_plan": "",
                "evidence": [],
                "created_at": "2026-05-25T02:00:00+00:00",
                "updated_at": "2026-05-25T02:00:00+00:00",
            },
        )

        approved = self.client.get("/api/control-center/proposals?status=approved")
        proposed = self.client.get("/api/control-center/proposals?status=proposed")

        assert approved.status_code == 200
        assert len(approved.json()["proposals"]) == 1
        assert proposed.status_code == 200
        assert proposed.json()["proposals"] == []

    def test_proposals_endpoint_overlays_ledger_decisions(self, _isolate_hermes_home):
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        proposals_dir = home / "telemetry" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            proposals_dir / "proposal:test-ledger-overlay.row.json",
            {
                "proposal_id": "proposal:test-ledger-overlay",
                "title": "Ledger overlay fixture",
                "status": "proposed",
                "decision_requested": "approve",
                "owner_profile": "ops",
                "tl_dr": "ledger overlay test",
                "confidence_score": 0.4,
                "confidence_label": "low",
                "confidence_basis": {},
                "risk_level": "medium",
                "risk_notes": "",
                "rollback_plan": "",
                "verification_plan": "",
                "evidence": [],
                "created_at": "2026-05-25T02:00:00+00:00",
                "updated_at": "2026-05-25T02:00:00+00:00",
            },
        )

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
                INSERT INTO proposals(proposal_id, status, approved_at, denied_at, approver, denial_reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "proposal:test-ledger-overlay",
                    "approved",
                    "2026-05-25T03:00:00+00:00",
                    None,
                    "Chad Tao",
                    None,
                    "2026-05-25T03:00:00+00:00",
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
                    "proposal:test-ledger-overlay",
                    "2026-05-25T03:00:00+00:00",
                    "approve",
                    "Chad Tao",
                    None,
                    "proposed",
                    "approved",
                    "slack:thread-22",
                    "/tmp/backup",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        resp = self.client.get("/api/control-center/proposals?status=approved")
        assert resp.status_code == 200
        rows = resp.json()["proposals"]
        assert len(rows) == 1
        assert rows[0]["proposal_id"] == "proposal:test-ledger-overlay"
        assert rows[0]["status"] == "approved"
        assert rows[0]["approver"] == "Chad Tao"
        assert rows[0]["decision"]["source"] == "slack:thread-22"

        proposed = self.client.get("/api/control-center/proposals?status=proposed")
        assert proposed.status_code == 200
        assert proposed.json()["proposals"] == []

    def test_pending_respond_enqueues_phase2c_command(self, monkeypatch, _isolate_hermes_home):
        monkeypatch.setenv("HERMES_CONTROL_CENTER_ACTIONS", "1")
        import control_center_store as cc

        cc.cc_upsert_live_session(
            "pending-session-001",
            owner_kind="gateway",
            owner_id="pid-1",
            running=True,
        )
        cc.cc_create_pending_request(
            "pending-req-001",
            "pending-session-001",
            "clarify",
            prompt_preview="Need answer?",
        )

        resp = self.client.post(
            "/api/control-center/pending/pending-req-001/respond",
            json={"text": "yes"},
        )
        assert resp.status_code == 200
        command = resp.json()["command"]
        assert command["action"] == "respond_pending"
        assert command["target_session_id"] == "pending-session-001"
        assert command["payload"]["request_id"] == "pending-req-001"
        assert command["payload"]["text"] == "yes"

    def test_processes_status_and_keys(self):
        resp = self.client.get("/api/control-center/processes")
        assert resp.status_code == 200
        data = resp.json()
        assert "processes" in data
        assert isinstance(data["processes"], list)

    def test_processes_item_shape(self, _isolate_hermes_home):
        """Process items must have the ControlCenterProcess contract fields."""
        from hermes_constants import get_hermes_home

        checkpoint = [
            {
                "session_id": "proc-ws-001",
                "command": "pytest tests/",
                "pid": 9999,
                "status": "running",
                "started_at": "2024-06-01T00:00:00",
                "session_key": "",
            }
        ]
        _write_json(get_hermes_home() / "processes.json", checkpoint)

        data = self.client.get("/api/control-center/processes").json()
        procs = data["processes"]
        assert len(procs) >= 1
        p = procs[0]
        for field in ("session_id", "pid", "command", "started_at",
                      "exited", "exit_code", "notify_on_complete"):
            assert field in p, f"process item missing field: {field}"
        assert isinstance(p["exited"], bool)
        assert isinstance(p["notify_on_complete"], bool)



    def test_process_poll_not_found_returns_404(self):
        resp = self.client.get("/api/control-center/processes/proc-does-not-exist/poll")
        assert resp.status_code == 404

    def test_process_log_not_found_returns_404(self):
        resp = self.client.get("/api/control-center/processes/proc-does-not-exist/log")
        assert resp.status_code == 404

    def test_process_wait_not_found_returns_404(self):
        resp = self.client.post("/api/control-center/processes/proc-does-not-exist/wait?timeout=1")
        assert resp.status_code == 404

    def test_system_processes_status_and_keys(self, monkeypatch):
        import control_center_store as cc

        monkeypatch.setattr(
            cc,
            "read_system_processes",
            lambda limit=100: [
                {
                    "pid": 123,
                    "ppid": 1,
                    "elapsed": "00:00:01",
                    "kind": "dashboard",
                    "command": "python -m hermes_cli.web_server",
                    "command_preview": "python -m hermes_cli.web_server",
                    "managed": False,
                }
            ],
        )

        resp = self.client.get("/api/control-center/system-processes")
        assert resp.status_code == 200
        data = resp.json()
        assert "processes" in data
        assert data["processes"][0]["kind"] == "dashboard"
        assert data["processes"][0]["pid"] == 123

    def test_kill_managed_process_not_found_returns_404(self):
        resp = self.client.post("/api/control-center/processes/proc-does-not-exist/kill")
        assert resp.status_code == 404

    def test_runtimes_status_and_keys(self, monkeypatch):
        import control_center_store as cc

        monkeypatch.setattr(
            cc,
            "read_runtime_health",
            lambda: {
                "last_checked": 123.0,
                "runtimes": [{
                    "id": "dashboard",
                    "name": "Dashboard",
                    "status": "running",
                    "running": True,
                    "state": "serving",
                    "primary_pid": 1,
                    "pids": [1],
                    "source": "test",
                    "details": {"responsive": True},
                    "warnings": [],
                    "actions": [],
                }],
                "alerts": [],
            },
        )

        resp = self.client.get("/api/control-center/runtimes")
        assert resp.status_code == 200
        data = resp.json()
        assert "last_checked" in data
        assert data["runtimes"][0]["id"] == "dashboard"


    def test_process_wait_disabled_by_default_for_read_only_mode(self, monkeypatch):
        monkeypatch.delenv("HERMES_CONTROL_CENTER_ACTIONS", raising=False)

        resp = self.client.post("/api/control-center/processes/proc-test/wait")

        assert resp.status_code == 404
        assert "disabled" in resp.json()["detail"]

    def test_runtime_actions_disabled_by_default_for_phase1(self, monkeypatch):
        monkeypatch.delenv("HERMES_CONTROL_CENTER_ACTIONS", raising=False)

        resp = self.client.post("/api/control-center/runtimes/dashboard/actions/stop")
        assert resp.status_code == 404
        assert "disabled" in resp.json()["detail"]

    def test_runtime_action_not_found_returns_404(self, monkeypatch):
        import control_center_store as cc

        monkeypatch.setattr(cc, "execute_runtime_action", lambda runtime_id, action: {
            "status": "not_found",
            "runtime_id": runtime_id,
            "action": action,
            "error": "missing",
        })

        resp = self.client.post("/api/control-center/runtimes/nope/actions/restart")
        assert resp.status_code == 404

    def test_interrupt_requires_phase2d_destructive_gate(self, monkeypatch, _isolate_hermes_home):
        monkeypatch.setenv("HERMES_CONTROL_CENTER_ACTIONS", "1")
        monkeypatch.delenv("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", raising=False)
        import control_center_store as cc

        cc.cc_upsert_live_session(
            "destructive-session-001",
            owner_kind="gateway",
            owner_id="pid-1",
            running=True,
        )
        resp = self.client.post("/api/control-center/sessions/destructive-session-001/interrupt")
        assert resp.status_code == 404
        assert "destructive" in resp.json()["detail"]

        monkeypatch.setenv("HERMES_CONTROL_CENTER_DESTRUCTIVE_ACTIONS", "1")
        resp = self.client.post("/api/control-center/sessions/destructive-session-001/interrupt")
        assert resp.status_code == 200
        assert resp.json()["command"]["action"] == "interrupt"

    def test_delegation_status_and_keys(self):
        resp = self.client.get("/api/control-center/delegation")
        assert resp.status_code == 200
        data = resp.json()
        assert "subagents" in data
        assert isinstance(data["subagents"], list)

    def test_delegation_item_shape(self, _isolate_hermes_home):
        """Delegation items must have the ControlCenterDelegationSummary contract fields."""
        from hermes_constants import get_hermes_home

        now = time.time()
        root = get_hermes_home() / "spawn-trees" / "ws-session-abc"
        root.mkdir(parents=True, exist_ok=True)
        entry = {
            "session_id": "ws-session-abc",
            "started_at": now - 120,
            "finished_at": now - 60,
            "label": "WS Delegation",
            "count": 2,
        }
        (root / "_index.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

        data = self.client.get("/api/control-center/delegation").json()
        subs = data["subagents"]
        assert len(subs) >= 1
        d = subs[0]
        for field in ("session_id", "subagent_id", "status"):
            assert field in d, f"delegation item missing field: {field}"
        assert "parent_subagent_id" in d

    def test_specialist_lanes_from_kanban(self, _isolate_hermes_home):
        """Specialist lanes must report durable kanban/profile-lane work separately."""
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "kanban.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    assignee TEXT,
                    status TEXT,
                    created_at REAL,
                    started_at REAL,
                    completed_at REAL,
                    session_id TEXT
                )
                """
            )
            now = time.time()
            conn.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("t_impl", "implement lane", "engineer", "running", now - 120, now - 60, None, "s-eng"),
                    ("t_review", "review lane", "reviewer", "todo", now - 90, None, None, None),
                    ("t_old", "old lane", "ops", "archived", now - 240, now - 200, now - 100, None),
                ],
            )
            conn.commit()

        resp = self.client.get("/api/control-center/specialist-lanes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        lanes = {lane["assignee"]: lane for lane in data["lanes"]}
        assert lanes["engineer"]["running_tasks"] == 1
        assert lanes["reviewer"]["todo_tasks"] == 1
        assert "ops" not in lanes
        assert data["recent_tasks"][0]["id"] in {"t_impl", "t_review", "t_old"}

    def test_profiles_status_and_keys(self):
        resp = self.client.get("/api/control-center/profiles")
        assert resp.status_code == 200
        data = resp.json()
        assert "profiles" in data
        assert isinstance(data["profiles"], list)

    def test_profiles_fallback_to_live_sessions_when_gateway_down(self, _isolate_hermes_home, monkeypatch):
        import control_center_store as cc

        monkeypatch.setattr("gateway.status.get_running_pid", lambda cleanup_stale=False: None)
        cc.cc_upsert_live_session(
            "phase1-profile-fallback",
            owner_kind="tui",
            owner_id="pid-1",
            profile="reviewer",
            title="Fallback Session",
            model="claude-test",
            running=True,
        )

        data = self.client.get("/api/control-center/profiles").json()
        profiles = data["profiles"]
        reviewer = next((p for p in profiles if p["name"] == "reviewer"), None)
        assert reviewer is not None
        assert reviewer["active_sessions"] == 1
        assert reviewer["model"] == "claude-test"

    def test_sessions_endpoint_excludes_inactive_sessions(self, _isolate_hermes_home):
        """Sessions endpoint must not list sessions whose last activity is >5 min old."""
        try:
            from hermes_state import SessionDB
        except ImportError:
            pytest.skip("hermes_state not importable")

        db = SessionDB()
        now = time.time()
        db.create_session("live-ws-001", source="test", model="claude")
        db.create_session("stale-ws-001", source="test", model="claude")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (now - 400, "stale-ws-001"),
        )
        db._conn.commit()
        db.close()

        data = self.client.get("/api/control-center/sessions").json()
        ids = {s["session_id"] for s in data["sessions"]}
        assert "live-ws-001" in ids, "recently-created session must appear"
        assert "stale-ws-001" not in ids, "stale session must be excluded"

    def test_sessions_all_items_have_running_true(self, _isolate_hermes_home):
        """Every session returned by the sessions endpoint must have running=True."""
        try:
            from hermes_state import SessionDB
        except ImportError:
            pytest.skip("hermes_state not importable")

        db = SessionDB()
        db.create_session("running-ws-001", source="test", model="claude")
        db.close()

        data = self.client.get("/api/control-center/sessions").json()
        for s in data["sessions"]:
            assert s["running"] is True, f"session {s['session_id']} has running=False in live endpoint"
