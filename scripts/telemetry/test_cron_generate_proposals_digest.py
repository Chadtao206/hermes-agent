#!/usr/bin/env python3
"""Tests for the proposals digest formatting (dry-run vs live mode)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cron_generate_proposals_digest_under_test",
        THIS_DIR / "cron_generate_proposals_digest.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


digest_mod = _load_module()


def _payload(**overrides) -> dict:
    payload = {
        "overall_verdict": "NOT_COMPLETE",
        "dry_run": True,
        "proposal_count": 1,
        "min_ledger_confidence": "medium",
        "ledger_persisted_count": 0,
        "file_only_count": 1,
        "skipped_stale_gap_tasks": [],
        "archived_stale_packets": [],
        "suppressed_count": 0,
        "proposals": [
            {
                "proposal_id": "proposal:readiness_gate_fix-x-ops",
                "proposal_type": "readiness_gate_fix",
                "title": "Repair readiness gate: x",
                "decision_requested": "approve",
                "owner_profile": "ops",
                "confidence_label": "not_ready",
            }
        ],
        "suppressed": [],
    }
    payload.update(overrides)
    return payload


def case_dry_run_digest_reports_floor_ttl_and_archive() -> None:
    payload = _payload(
        skipped_stale_gap_tasks=["jira:OLD-1"],
        archived_stale_packets=["proposal:old.json", "proposal:old.md"],
    )
    counts = {"proposals": 1, "proposal_evidence_links": 1}
    text = digest_mod.build_digest(payload, counts, before_counts=counts)
    assert "dry-run" in text and "LIVE" not in text, text
    assert "Ledger floor (medium): 0 persisted, 1 file-only" in text, text
    assert "Gap-repair TTL skipped (1): jira:OLD-1" in text, text
    assert "Archived stale packets: 2" in text, text


def case_live_digest_reports_ledger_delta() -> None:
    payload = _payload(dry_run=False, ledger_persisted_count=2, file_only_count=1, proposal_count=3)
    before = {"proposals": 1, "proposal_evidence_links": 1}
    after = {"proposals": 3, "proposal_evidence_links": 4}
    text = digest_mod.build_digest(payload, after, before_counts=before)
    assert "Mode: LIVE" in text, text
    assert "remains unchanged" not in text, text
    assert "ledger delta this run: proposals +2" in text, text
    assert "Ledger floor (medium): 2 persisted, 1 file-only" in text, text


def main() -> int:
    case_dry_run_digest_reports_floor_ttl_and_archive()
    case_live_digest_reports_ledger_delta()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
