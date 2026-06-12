#!/usr/bin/env python3
"""Tests for readiness_doctor consolidated cron requirements + gate waivers."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {path} for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rd = _load_module("readiness_doctor_under_test", THIS_DIR / "readiness_doctor.py")
gp = _load_module("generate_proposals_under_test", THIS_DIR / "generate_proposals.py")

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _ok_job(job_id: str) -> dict:
    return {
        "id": job_id,
        "last_run_at": "2026-06-12T07:32:31+02:00",
        "last_status": "ok",
        "last_error": None,
    }


def _scheduler_ctx(cron_jobs):
    return SimpleNamespace(
        cron_jobs=cron_jobs,
        cron_state_error=None,
        cron_state_path=Path("/tmp/jobs.json"),
    )


def _failing_gate(gate_id: str = "kanban_telemetry_drift_state") -> dict:
    return rd.gate_result(
        gate_id,
        "fail",
        "Kanban/telemetry drift cannot pass before the sync job succeeds at least once.",
        {"latest_successful_sync_at": None},
        ["daily-telemetry-kanban-sync has never recorded a successful scheduler run."],
    )


def _waiver(gate_id: str = "kanban_telemetry_drift_state", *, expires_at: str = "2026-07-15T00:00:00+00:00", **overrides) -> dict:
    waiver = {
        "gate_id": gate_id,
        "reason": "kanban sync paused for Postgres migration",
        "waived_by": "Chad Tao",
        "expires_at": expires_at,
    }
    waiver.update(overrides)
    return waiver


def case_required_cron_jobs_match_consolidated_schedulers() -> None:
    assert rd.REQUIRED_CRON_JOBS == {"daily-ops-digest", "weekly-ops-digest"}, rd.REQUIRED_CRON_JOBS


def case_scheduler_proof_passes_with_consolidated_receipts() -> None:
    ctx = _scheduler_ctx(
        {
            "daily-ops-digest": _ok_job("374314bc3008"),
            "weekly-ops-digest": _ok_job("d443324f8d69"),
        }
    )
    gate = rd.gate_scheduler_proof_state(ctx)
    assert gate["status"] == "pass", gate

    missing_ctx = _scheduler_ctx({"daily-ops-digest": _ok_job("374314bc3008")})
    missing_gate = rd.gate_scheduler_proof_state(missing_ctx)
    assert missing_gate["status"] == "fail", missing_gate
    assert any("weekly-ops-digest" in reason for reason in missing_gate["reasons"]), missing_gate["reasons"]


def case_active_waiver_marks_failing_gate_waived() -> None:
    gates = [_failing_gate()]
    rd.apply_waivers(gates, [_waiver()], now=NOW)
    gate = gates[0]
    assert gate["status"] == "waived", gate
    assert gate["evidence"]["waiver"]["waived_by"] == "Chad Tao", gate["evidence"]
    assert gate["evidence"]["waiver"]["expires_at"] == "2026-07-15T00:00:00+00:00", gate["evidence"]
    # Original failure detail must remain auditable.
    assert gate["reasons"], gate


def case_expired_or_malformed_waiver_keeps_gate_failing() -> None:
    expired = [_failing_gate()]
    rd.apply_waivers(expired, [_waiver(expires_at="2026-06-01T00:00:00+00:00")], now=NOW)
    assert expired[0]["status"] == "fail", expired[0]

    missing_field = [_failing_gate()]
    rd.apply_waivers(missing_field, [_waiver(waived_by="")], now=NOW)
    assert missing_field[0]["status"] == "fail", missing_field[0]

    other_gate = [_failing_gate("scheduler_proof_state")]
    rd.apply_waivers(other_gate, [_waiver()], now=NOW)
    assert other_gate[0]["status"] == "fail", other_gate[0]


def case_waiver_never_touches_passing_gate() -> None:
    gates = [
        rd.gate_result("kanban_telemetry_drift_state", "pass", "ok", {}, []),
    ]
    rd.apply_waivers(gates, [_waiver()], now=NOW)
    assert gates[0]["status"] == "pass", gates[0]


def case_overall_verdict_counts_waived_as_complete() -> None:
    passing = rd.gate_result("a", "pass", "ok", {}, [])
    waived = rd.gate_result("b", "waived", "waived", {}, ["original reason"])
    failing = rd.gate_result("c", "fail", "bad", {}, ["broken"])
    assert rd.overall_verdict([passing, waived]) == "COMPLETE"
    assert rd.overall_verdict([passing, waived, failing]) == "NOT_COMPLETE"


def case_load_waivers_handles_missing_and_malformed_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "readiness_waivers.json"
        assert rd.load_waivers(missing) == []

        malformed = Path(tmp) / "bad.json"
        malformed.write_text("{not json", encoding="utf-8")
        assert rd.load_waivers(malformed) == []

        good = Path(tmp) / "good.json"
        good.write_text(json.dumps({"waivers": [_waiver()]}), encoding="utf-8")
        loaded = rd.load_waivers(good)
        assert len(loaded) == 1 and loaded[0]["gate_id"] == "kanban_telemetry_drift_state", loaded


def case_waived_gate_produces_no_repair_proposal() -> None:
    readiness = {
        "gates": [
            {
                "gate_id": "kanban_telemetry_drift_state",
                "status": "waived",
                "summary": "waived for Postgres migration",
                "reasons": ["sync paused"],
            },
            {
                "gate_id": "scheduler_proof_state",
                "status": "fail",
                "summary": "missing receipts",
                "reasons": ["weekly-ops-digest non-ok"],
            },
        ]
    }
    proposals = gp.readiness_gate_proposals(readiness, gate_blocked=True)
    gate_ids = {p["confidence_basis"]["gate_id"] for p in proposals}
    assert gate_ids == {"scheduler_proof_state"}, gate_ids


def main() -> int:
    case_required_cron_jobs_match_consolidated_schedulers()
    case_scheduler_proof_passes_with_consolidated_receipts()
    case_active_waiver_marks_failing_gate_waived()
    case_expired_or_malformed_waiver_keeps_gate_failing()
    case_waiver_never_touches_passing_gate()
    case_overall_verdict_counts_waived_as_complete()
    case_load_waivers_handles_missing_and_malformed_file()
    case_waived_gate_produces_no_repair_proposal()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
