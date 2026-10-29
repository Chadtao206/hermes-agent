import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "cron" / "scripts" / "jev_shadow_report.py"
spec = importlib.util.spec_from_file_location("jev_shadow_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

JUDGED_NO = {"would_open": False, "issues": [{"title": "t", "in_diff": True, "mechanical": 0.45,
                                               "author_decision": 0.1, "instruction": 0.1}]}
SKIPPED = {"would_open": None, "skipped": "no_issues"}
LEGACY_SKIP = {"would_open": None, "error": "TypeSafeError('At least one question is required.')"}
API_ERROR = {"would_open": None, "error": "TypeSafeAPITimeoutError('read timed out')"}


@pytest.fixture
def con():
    db = sqlite3.connect(":memory:")
    db.execute("create table loop_events (id integer primary key, loop_id text, kind text)")
    yield db
    db.close()


def events(con, loop_id, *kinds):
    for kind in kinds:
        con.execute("insert into loop_events (loop_id, kind) values (?, ?)", (loop_id, kind))


def test_an_escalation_is_its_own_outcome_not_a_failed_fix(con):
    events(con, "L1", "jev_gate_shadow", "escalation_delivered")
    events(con, "L2", "jev_gate_shadow", "lane_abandoned")
    events(con, "L3", "jev_gate_shadow", "escalation_delivered", "lane_pushed")
    assert report.loop_outcome(con, "L1", 0) == "escalated"
    assert report.loop_outcome(con, "L2", 0) == "no_fix"
    assert report.loop_outcome(con, "L3", 0) == "fix"


def test_go_no_go_ignores_skips_and_counts_only_real_api_errors():
    settled = [("L", JUDGED_NO, "no_fix")] * report.MIN_GATE_OUTCOMES
    assert report.gate_recommendation(settled + [("S", SKIPPED, None), ("T", LEGACY_SKIP, None)]).startswith("GO")
    assert report.gate_recommendation(settled + [("E", API_ERROR, None)]) == "STAY IN SHADOW: 1 API errors"


def test_escalated_lanes_do_not_count_as_evidence_the_refusal_was_right():
    notes = [("L", JUDGED_NO, "escalated")] * report.MIN_GATE_OUTCOMES
    assert "only 0 lanes with a known outcome" in report.gate_recommendation(notes)


@pytest.mark.parametrize("enabled, notes, expected", [
    (False, [], "OFF"),
    (True, [], "NO notes"),
    (True, [("L", JUDGED_NO, None), ("S", SKIPPED, None), ("E", API_ERROR, None)],
     "1 judged, 1 skipped, 1 API errors"),
])
def test_banner_reports_what_the_gate_actually_wrote(enabled, notes, expected):
    banner = report.gate_banner(enabled, notes, 7)
    assert expected in banner
    assert "still collecting" not in banner


def test_calibration_table_counts_outcomes_per_mechanical_band():
    high = {"would_open": True, "issues": [{**JUDGED_NO["issues"][0], "mechanical": 0.82},
                                           {**JUDGED_NO["issues"][0], "mechanical": 0.1}]}
    out_of_diff = {"would_open": False, "issues": [{**JUDGED_NO["issues"][0], "in_diff": False}]}
    lines = report.gate_calibration([("A", JUDGED_NO, "no_fix"), ("B", JUDGED_NO, "fix"),
                                     ("C", high, None), ("D", SKIPPED, None), ("E", out_of_diff, "fix")])
    rows = {line.split()[0]: line.split()[1:] for line in lines[2:]}
    assert rows["0.4-0.5"] == ["2", "1", "1", "0", "0", "50%"]
    assert rows["0.8-0.9"] == ["1", "0", "0", "0", "1", "-"]
    assert len(rows) == 2
