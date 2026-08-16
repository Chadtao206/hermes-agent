import json
import sqlite3

import pytest

from pr_remediation.boris_source import BorisSource, EvidenceAnchor, Issue, Verdict
from pr_remediation.config import RemediationConfig

SCHEMA = """
CREATE TABLE review_requests (
    request_id TEXT PRIMARY KEY, owner TEXT, repo TEXT, pr_number INTEGER,
    head_sha TEXT, pr_author TEXT, requester_user_id TEXT, root_thread_ts TEXT,
    status_message_ts TEXT,
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
        "status_message_ts": "2.0",
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
        ":head_sha,:pr_author,:requester_user_id,:root_thread_ts,:status_message_ts,"
        ":workflow_state,:semantic_outcome,:validated_result_json,:completed_at)", row)
    db.commit()
    db.close()
    return path


def test_a_pr_chad_posted_and_authored_qualifies(tmp_path):
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    verdicts = source.new_verdicts()
    assert len(verdicts) == 1
    assert verdicts[0].outcome == "ISSUES_FOUND"
    # blocking_issues carries full Issue objects now, not bare title strings
    # (see boris_source.Issue's module docstring) -- the fixture's payload
    # supplies only a title, so every other field defaults to its empty
    # value and this equality still pins the title exactly.
    assert verdicts[0].blocking_issues == (Issue(title="Null deref in handler"),)


def test_status_message_ts_is_carried_onto_the_verdict(tmp_path):
    """The reaction anchor (see pr_remediation/loop.py), read from the same
    column boris_intake stamps and later replaces with its verdict -- distinct
    from root_thread_ts, which must come through unchanged alongside it."""
    source = BorisSource(boris_db(tmp_path, status_message_ts="2.0",
                                  root_thread_ts="1.0"), RemediationConfig())
    verdict = source.new_verdicts()[0]
    assert verdict.status_message_ts == "2.0"
    assert verdict.root_thread_ts == "1.0", "the two columns must not be conflated"


def test_a_null_status_message_ts_comes_through_as_none_not_a_crash(tmp_path):
    """Nullable in the source schema: some rows never got a status message."""
    source = BorisSource(boris_db(tmp_path, status_message_ts=None),
                         RemediationConfig())
    verdict = source.new_verdicts()[0]
    assert verdict.status_message_ts is None


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
    assert verdict.blocking_issues == (Issue(title="real issue"),)


def test_incomplete_that_is_not_a_dict_does_not_crash(tmp_path):
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [],
        "incomplete": "not an object",
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.incomplete_origin is None


def test_the_full_issue_structure_survives_onto_the_verdict(tmp_path):
    """The whole point of widening Verdict.blocking_issues: everything Boris
    produced for a finding -- not only its title -- has to reach the
    remediation brief, and this is the boundary that must stop discarding
    it. All eight fields boris_intake's Issue carries, round-tripped."""
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [{
            "title": "Null deref in handler",
            "expected_behavior": "Handler returns 400 on a missing field",
            "changed_mechanism": "The new guard short-circuits before validation",
            "trigger": "POST with a missing `id` field",
            "consequence": "500 instead of 400, leaking a stack trace",
            "impact_scope": "Every caller of /webhook",
            "action": "Move the guard after validation",
            "evidence_anchors": [
                {"kind": "diff", "reference": "src/webhook.py", "detail": "line 42"},
                {"kind": "test", "reference": "tests/test_webhook.py::test_missing_id"},
            ],
        }],
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert len(verdict.blocking_issues) == 1
    issue = verdict.blocking_issues[0]
    assert issue.title == "Null deref in handler"
    assert issue.expected_behavior == "Handler returns 400 on a missing field"
    assert issue.changed_mechanism == "The new guard short-circuits before validation"
    assert issue.trigger == "POST with a missing `id` field"
    assert issue.consequence == "500 instead of 400, leaking a stack trace"
    assert issue.impact_scope == "Every caller of /webhook"
    assert issue.action == "Move the guard after validation"
    assert issue.evidence_anchors == (
        EvidenceAnchor(kind="diff", reference="src/webhook.py", detail="line 42"),
        EvidenceAnchor(kind="test", reference="tests/test_webhook.py::test_missing_id"),
    )


def test_an_issue_with_malformed_extra_fields_still_contributes_its_title(tmp_path):
    """A payload boris_intake's own schema would never write (wrong types on
    every non-title field), read anyway: the issue must still construct and
    still carry its title, not raise and not vanish. This is the case the
    defensive-parsing constraint names explicitly."""
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [{
            "title": "Race in seed",
            "expected_behavior": 123,
            "changed_mechanism": None,
            "trigger": ["not", "a", "string"],
            "consequence": {"nested": "object"},
            "impact_scope": 4.5,
            "action": False,
            "evidence_anchors": "not a list",
        }],
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert len(verdict.blocking_issues) == 1
    issue = verdict.blocking_issues[0]
    assert issue.title == "Race in seed"
    assert issue.expected_behavior == ""
    assert issue.changed_mechanism == ""
    assert issue.trigger == ""
    assert issue.consequence == ""
    assert issue.impact_scope == ""
    assert issue.action == ""
    assert issue.evidence_anchors == ()


def test_an_issue_missing_every_extra_field_still_contributes_its_title(tmp_path):
    """The minimal case: a dict with only a title, nothing else present at
    all (not merely malformed) -- the same "still contributes its title"
    guarantee from the other end."""
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [{"title": "Bare issue"}],
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.blocking_issues == (Issue(title="Bare issue"),)


def test_malformed_evidence_anchor_entries_are_dropped_individually(tmp_path):
    """One bad anchor must not cost the issue its other, good anchors, and
    must not cost the issue itself."""
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [{
            "title": "Race in seed",
            "evidence_anchors": [
                {"kind": "diff", "reference": "src/seed.py"},
                {"kind": "diff"},                 # missing reference
                {"reference": "src/x.py"},        # missing kind
                "not a dict",
                {"kind": 5, "reference": "y"},     # kind not a string
                {"kind": "diff", "reference": ""},  # empty reference
            ],
        }],
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert verdict.blocking_issues[0].evidence_anchors == (
        EvidenceAnchor(kind="diff", reference="src/seed.py"),
    )


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
