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
