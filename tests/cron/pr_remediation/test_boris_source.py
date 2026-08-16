import json
import sqlite3

import pytest

from pr_remediation.boris_source import BorisSource, Verdict
from pr_remediation.config import RemediationConfig

SCHEMA = """
CREATE TABLE review_requests (
    request_id TEXT PRIMARY KEY, owner TEXT, repo TEXT, pr_number INTEGER,
    head_sha TEXT, pr_author TEXT, requester_user_id TEXT, root_thread_ts TEXT,
    workflow_state TEXT, semantic_outcome TEXT, validated_result_json TEXT,
    completed_at REAL
);
"""


def boris_db(tmp_path, **overrides):
    path = tmp_path / "boris.db"
    db = sqlite3.connect(str(path))
    db.executescript(SCHEMA)
    row = {
        "request_id": "br_1", "owner": "Ylopo", "repo": "example", "pr_number": 42,
        "head_sha": "a" * 40, "pr_author": "Chadtao206",
        "requester_user_id": "U07E3AHV686", "root_thread_ts": "1.0",
        "workflow_state": "complete", "semantic_outcome": "ISSUES_FOUND",
        "validated_result_json": json.dumps({
            "blocking_issues": [{"title": "Null deref in handler"}],
            "incomplete": None,
        }),
        "completed_at": 1_000_000.0,
    }
    row.update(overrides)
    db.execute(
        "INSERT INTO review_requests VALUES (:request_id,:owner,:repo,:pr_number,"
        ":head_sha,:pr_author,:requester_user_id,:root_thread_ts,:workflow_state,"
        ":semantic_outcome,:validated_result_json,:completed_at)", row)
    db.commit()
    db.close()
    return path


def test_a_pr_chad_posted_and_authored_qualifies(tmp_path):
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    verdicts = source.new_verdicts()
    assert len(verdicts) == 1
    assert verdicts[0].outcome == "ISSUES_FOUND"
    assert verdicts[0].blocking_issues == ("Null deref in handler",)


def test_a_pr_chad_posted_but_did_not_author_is_ignored(tmp_path):
    """The SOUL.md third-party gate: never write to someone else's PR."""
    source = BorisSource(boris_db(tmp_path, pr_author="jacobmaurer"), RemediationConfig())
    assert source.new_verdicts() == ()


def test_a_pr_chad_authored_but_someone_else_posted_is_ignored(tmp_path):
    source = BorisSource(boris_db(tmp_path, requester_user_id="U5GB9F75Z"),
                         RemediationConfig())
    assert source.new_verdicts() == ()


def test_incomplete_verdicts_carry_their_origin(tmp_path):
    path = boris_db(
        tmp_path, semantic_outcome="REVIEW_INCOMPLETE",
        validated_result_json=json.dumps({
            "blocking_issues": [],
            "incomplete": {"origin": "system", "reason_codes": ["HEAD_SUPERSEDED"]},
        }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.incomplete_origin == "system"


def test_unfinished_reviews_are_not_verdicts_yet(tmp_path):
    source = BorisSource(boris_db(tmp_path, workflow_state="reviewing"),
                         RemediationConfig())
    assert source.new_verdicts() == ()


def test_the_source_never_writes_to_the_boris_database(tmp_path):
    path = boris_db(tmp_path)
    BorisSource(path, RemediationConfig()).new_verdicts()
    db = sqlite3.connect(str(path))
    # A read-only connection would have failed already; assert the row is untouched.
    assert db.execute("SELECT COUNT(*) FROM review_requests").fetchone()[0] == 1


# --- defensive parsing: boris_intake owns validated_result_json's shape and
# evolves it independently of this reader, so a row whose JSON parses fine
# but into an unexpected shape must degrade, not take the poller down. ---


@pytest.mark.parametrize(
    "bad_payload",
    [json.dumps([1, 2, 3]), json.dumps("not an object"), json.dumps(42)],
    ids=["list", "string", "number"],
)
def test_non_dict_top_level_payload_does_not_crash(tmp_path, bad_payload):
    path = boris_db(tmp_path, validated_result_json=bad_payload)
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.blocking_issues == ()
    assert verdict.incomplete_origin is None
    # Identity fields come from real columns, not the payload, so they stay trustworthy.
    assert verdict.outcome == "ISSUES_FOUND"
    assert verdict.head_sha == "a" * 40


def test_blocking_issues_that_is_not_a_list_becomes_an_empty_tuple(tmp_path):
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": 5,
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.blocking_issues == ()


def test_non_dict_items_inside_blocking_issues_are_skipped(tmp_path):
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [
            {"title": "real issue"}, "not a dict", 42, None, ["nested"],
        ],
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.blocking_issues == ("real issue",)


def test_incomplete_that_is_not_a_dict_does_not_crash(tmp_path):
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [],
        "incomplete": "not an object",
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.incomplete_origin is None


def test_review_incomplete_with_unreadable_incomplete_never_defaults_to_system(tmp_path):
    """origin="system" tells the remediation loop a REVIEW_INCOMPLETE failure
    was transient and safe to retry automatically, without spending one of
    the loop's limited remediation rounds. An `incomplete` payload we can't
    interpret is NOT known to be transient — guessing "system" for it would
    make the loop retry forever on a row it never actually understood. None
    is the fail-safe answer: it routes to human escalation instead. Do not
    "helpfully" default this to "system".
    """
    path = boris_db(
        tmp_path, semantic_outcome="REVIEW_INCOMPLETE",
        validated_result_json=json.dumps({
            "blocking_issues": [],
            "incomplete": [1, 2, 3],
        }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.outcome == "REVIEW_INCOMPLETE"
    assert verdict.incomplete_origin is None
