import json
import sqlite3

import pytest

from pr_remediation.boris_source import (AdmissionLookup, AdmissionStatus,
                                         BorisSource, EvidenceAnchor, Issue,
                                         Verdict)
from pr_remediation.config import RemediationConfig

# `created_at` is boris_intake's own REAL epoch column (see its store.py's
# schema), carried here because `request_exists` asks a question about *when* a
# request was admitted, not only whether one was.
SCHEMA = """
CREATE TABLE review_requests (
    request_id TEXT PRIMARY KEY, owner TEXT, repo TEXT, pr_number INTEGER,
    head_sha TEXT, pr_author TEXT, requester_user_id TEXT, root_thread_ts TEXT,
    status_message_ts TEXT,
    workflow_state TEXT, semantic_outcome TEXT, validated_result_json TEXT,
    created_at REAL, completed_at REAL
);
"""

# The epoch the fixture's single request was admitted at, and the value every
# `since` below is written relative to. A literal, so no test asserts a
# boundary against the same expression the fixture builds it from.
ADMITTED_AT = 1_000_000.0


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
        "created_at": ADMITTED_AT,
        "completed_at": 1_000_000.0,
    }
    row.update(overrides)
    db.execute(
        "INSERT INTO review_requests VALUES (:request_id,:owner,:repo,:pr_number,"
        ":head_sha,:pr_author,:requester_user_id,:root_thread_ts,:status_message_ts,"
        ":workflow_state,:semantic_outcome,:validated_result_json,:created_at,"
        ":completed_at)", row)
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


def test_a_review_whose_slack_thread_vanished_is_not_a_verdict(tmp_path):
    """`slack_unavailable` is terminal for boris_intake too, and the row keeps
    its semantic_outcome, so it passes every other filter here. It must not be
    routed: it reaches that state only when the source thread is gone, and
    root_thread_ts -- the one place this loop can post a re-review -- points
    into it. Routing one spends a remediation round and then posts into
    nothing, with SlackIO._post swallowing the error into a bare None."""
    source = BorisSource(boris_db(tmp_path, workflow_state="slack_unavailable"),
                         RemediationConfig())
    assert source.new_verdicts() == ()


def test_a_completed_review_with_no_outcome_is_not_a_verdict(tmp_path):
    """loop.py branches on verdict.outcome; a NULL arrives as None and matches
    no branch. boris_intake sets the outcome before it transitions to
    complete, but that is boris_intake's invariant, not this reader's."""
    source = BorisSource(boris_db(tmp_path, semantic_outcome=None),
                         RemediationConfig())
    assert source.new_verdicts() == ()


def _backlog(tmp_path, count):
    """`count` completed verdicts, inserted oldest first so insertion order and
    completed_at order agree -- which is what makes a dropped ORDER BY
    detectable here rather than accidentally right."""
    path = boris_db(tmp_path, request_id="br_0", completed_at=ADMITTED_AT)
    db = sqlite3.connect(str(path))
    for index in range(1, count):
        db.execute(
            "INSERT INTO review_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"br_{index}", "Ylopo", "example", 42 + index, "a" * 40,
             "Chadtao206", "U07E3AHV686", "1.0", "2.0", "complete",
             "ISSUES_FOUND", json.dumps({"blocking_issues": [], "incomplete": None}),
             ADMITTED_AT, ADMITTED_AT + index),
        )
    db.commit()
    db.close()
    return path


def test_verdicts_come_back_newest_first(tmp_path):
    source = BorisSource(_backlog(tmp_path, 5), RemediationConfig())

    assert [v.request_id for v in source.new_verdicts()] == [
        "br_4", "br_3", "br_2", "br_1", "br_0"]


def test_a_backlog_wider_than_the_window_keeps_the_newest_not_the_oldest(tmp_path):
    """The ordering is load-bearing because nothing marks a row consumed on
    this side -- `store.mark_verdict_seen` does that after reading, so this is
    a window over the whole matching history. The live database already holds
    58 rows matching against a default window of 50, so a cold start after
    extended downtime reads a truncated view either way; DESC is what makes
    the truncation fall on verdicts the loop has already routed instead of on
    the ones that arrived while it was down."""
    source = BorisSource(_backlog(tmp_path, 60), RemediationConfig())

    verdicts = source.new_verdicts()

    assert len(verdicts) == 50, "the default window is 50, not 1 and not 60"
    assert verdicts[0].request_id == "br_59"
    assert verdicts[-1].request_id == "br_10"
    assert "br_0" not in {v.request_id for v in verdicts}


def test_the_source_never_writes_to_the_boris_database(tmp_path):
    path = boris_db(tmp_path)
    BorisSource(path, RemediationConfig()).new_verdicts()
    db = sqlite3.connect(str(path))
    # A read-only connection would have failed already; assert the row is untouched.
    assert db.execute("SELECT COUNT(*) FROM review_requests").fetchone()[0] == 1


# --- request_exists: did a re-review request this loop posted ever get in? ---
#
# boris_intake's gate reads GitHub synchronously while admitting a message and
# cannot tell a failed read from a missing pull request, so a GitHub outage
# rejects the loop's re-review post outright. The loop has already moved to
# `rereview_posted` by then, a state only an arriving verdict leaves, so it is
# parked forever with nothing recorded. This read is how it finds out.


def test_a_request_at_exactly_this_head_is_admitted(tmp_path):
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    assert source.request_exists("Ylopo", "example", 42, "a" * 40,
                                 ADMITTED_AT - 60).status is AdmissionStatus.ADMITTED


def test_a_request_at_the_previous_head_does_not_count_as_this_one(tmp_path):
    """The whole point of the head term, and the mutation that matters most.

    A remediation round always leaves an *older* request behind: the one whose
    verdict started the round, admitted at the head the lane has since moved
    off. Without `head_sha` in the WHERE clause that stale row answers "already
    admitted" for every later head, so a re-review the gate rejected looks
    admitted and the loop strands exactly as it does today -- silently, which
    is the failure mode this whole read exists to end.
    """
    source = BorisSource(boris_db(tmp_path, head_sha="a" * 40),
                         RemediationConfig())
    assert source.request_exists("Ylopo", "example", 42, "b" * 40,
                                 ADMITTED_AT - 60).not_admitted


@pytest.mark.parametrize(
    "owner,repo,head_sha",
    [("ylopo", "example", "a" * 40),
     ("Ylopo", "EXAMPLE", "a" * 40),
     ("Ylopo", "example", "A" * 40)],
    ids=["owner", "repo", "head_sha"],
)
def test_case_alone_never_makes_a_request_look_absent(tmp_path, owner, repo,
                                                      head_sha):
    """`pr_discovery/discover.py::IntakeSource.already_requested` queries these
    same three columns of this same table with COLLATE NOCASE and argues at
    length why it must: GitHub resolves owner and repo case-insensitively and a
    commit SHA is case-insensitive hex, so `Ylopo/Million-Stars` and
    `Ylopo/million-stars` are one repository. The invariant was documented in
    one module and not honoured in its sibling; a False here re-posts a
    re-review on top of a live one."""
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    assert source.request_exists(owner, repo, 42, head_sha,
                                 ADMITTED_AT - 60).status is AdmissionStatus.ADMITTED


@pytest.mark.parametrize(
    "owner,repo,pr_number",
    [("Someone", "example", 42), ("Ylopo", "other", 42), ("Ylopo", "example", 43)],
    ids=["owner", "repo", "pr_number"],
)
def test_a_request_for_a_different_pull_request_does_not_count(
        tmp_path, owner, repo, pr_number):
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    assert source.request_exists(owner, repo, pr_number, "a" * 40,
                                 ADMITTED_AT - 60).not_admitted


def test_a_review_still_under_way_counts_as_admitted(tmp_path):
    """Any workflow_state counts, deliberately -- unlike `new_verdicts`, which
    selects completed reviews. A request that is `reviewing` will produce a
    verdict on its own, so re-posting under it would be pointless noise and a
    duplicate review."""
    source = BorisSource(
        boris_db(tmp_path, workflow_state="reviewing", semantic_outcome=None),
        RemediationConfig())
    assert source.request_exists("Ylopo", "example", 42, "a" * 40,
                                 ADMITTED_AT - 60).status is AdmissionStatus.ADMITTED


def test_the_requester_and_author_filters_are_not_applied_here(tmp_path):
    """`new_verdicts` filters on requester_user_id and pr_author to select the
    verdicts this loop owns. This question is only "does a review already exist
    at this head" -- a narrower query would answer False for a review somebody
    else requested and re-post on top of it."""
    source = BorisSource(
        boris_db(tmp_path, pr_author="jacobmaurer", requester_user_id="U5GB9F75Z"),
        RemediationConfig())
    assert source.request_exists("Ylopo", "example", 42, "a" * 40,
                                 ADMITTED_AT - 60).status is AdmissionStatus.ADMITTED


def test_a_request_admitted_before_this_wait_began_does_not_count(tmp_path):
    """The other half of "is this request in?", and the one the head term alone
    cannot answer.

    `_repost` re-sends a re-review without moving the loop's head, so the row
    that produced the verdict which opened the loop still sits at exactly that
    head -- and answers "already admitted" for a post the gate has just
    rejected. Asking "admitted since this wait began" instead of "admitted at
    all" is what tells the two apart. There is no head-based way to: on that
    path there is only ever one head.
    """
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    assert source.request_exists("Ylopo", "example", 42, "a" * 40,
                                 ADMITTED_AT + 1).not_admitted


def test_a_request_admitted_at_the_instant_the_wait_began_counts(tmp_path):
    """Inclusive, and it has to be: the request and the transition that starts
    the wait are written by two clocks on this one machine within the same
    second, and the post is made *before* the row moves -- so a strict `>`
    would drop the very request this read exists to find."""
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    assert source.request_exists("Ylopo", "example", 42, "a" * 40,
                                 ADMITTED_AT).status is AdmissionStatus.ADMITTED


def test_a_read_that_fails_answers_none_rather_than_raising(tmp_path):
    """Three-way, the same contract shape `github_io.unresolved_bot_threads`
    uses: ADMITTED / NOT_ADMITTED / UNREADABLE. This runs inside a routing pass,
    where a raise would abort the loop's whole settle -- and where NOT_ADMITTED
    would be worse still, provoking a duplicate re-review post off a database we
    simply could not read."""
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    source.close()  # the read now raises ProgrammingError from sqlite3
    lookup = source.request_exists("Ylopo", "example", 42, "a" * 40,
                                   ADMITTED_AT - 60)
    assert lookup.unreadable
    assert lookup.admitted_at is None, \
        "nothing to anchor a deadline to, and the caller falls back accordingly"


# --- and *when* it was admitted, which is what the caller's absolute deadline
# is anchored on. Anchored at the wait instead, a request admitted late in a
# long wait got whatever was left of four hours -- ten minutes, for one admitted
# at 3h50m -- and REREVIEW_VERDICT_OVERDUE then sent a human to read a row whose
# review had barely begun. ---


def test_an_admitted_request_reports_when_the_gate_took_it(tmp_path):
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    lookup = source.request_exists("Ylopo", "example", 42, "a" * 40,
                                   ADMITTED_AT - 60)
    assert lookup.status is AdmissionStatus.ADMITTED
    assert lookup.admitted_at == ADMITTED_AT


def test_several_requests_at_this_head_report_the_latest_admission(tmp_path):
    """`MAX(created_at)`, and the mutation to `MIN` is the one to fear here.

    The deadline this feeds means "a request was accepted and produced nothing".
    If an older admission produced nothing but a newer one is in flight, the loop
    is not stuck -- so anchoring on the older one escalates out from under a
    review that is running perfectly, which is the whole defect this shape
    exists to fix. `MIN` is the reading someone will "fix" this to later.
    """
    path = boris_db(tmp_path)
    db = sqlite3.connect(str(path))
    db.execute(
        "INSERT INTO review_requests (request_id, owner, repo, pr_number, "
        "head_sha, created_at) VALUES ('br_2','Ylopo','example',42,?,?)",
        ("a" * 40, ADMITTED_AT + 3600.0))
    db.commit()
    db.close()

    lookup = BorisSource(path, RemediationConfig()).request_exists(
        "Ylopo", "example", 42, "a" * 40, ADMITTED_AT - 60)
    assert lookup.admitted_at == ADMITTED_AT + 3600.0, \
        "the newest admission at this head, not the oldest"


def test_a_request_that_was_never_admitted_reports_no_time(tmp_path):
    """And the aggregate is why this matters more than it looks.

    `SELECT MAX(...)` with no GROUP BY returns exactly one row even when nothing
    matches, so `row is not None` -- what the `SELECT 1` shape correctly used --
    would now read ADMITTED for every head, forever: the strand the `head_sha`
    term exists to prevent, rebuilt in the result handling. NULL in that one row
    is what "no such request" looks like.
    """
    source = BorisSource(boris_db(tmp_path), RemediationConfig())
    lookup = source.request_exists("Ylopo", "example", 42, "b" * 40,
                                   ADMITTED_AT - 60)
    assert lookup.status is AdmissionStatus.NOT_ADMITTED
    assert lookup.admitted_at is None


def test_a_created_at_that_is_not_a_number_reads_as_unreadable(tmp_path):
    """`created_at` is `REAL NOT NULL` in boris_intake's schema, so this takes a
    hand-repair or a bad migration -- but it is the first value from a column
    this reader does not own to reach arithmetic, and the caller compares it
    against a clock. Reaching the caller as a string, `admitted_at <= now()`
    raises a TypeError that aborts a routing pass which may owe other writes.
    UNREADABLE is the honest answer ("a row is there, but not when"), and the
    caller already bounds that case by the episode start.
    """
    source = BorisSource(boris_db(tmp_path, created_at="whenever"),
                         RemediationConfig())
    lookup = source.request_exists("Ylopo", "example", 42, "a" * 40,
                                   ADMITTED_AT - 60)
    assert lookup.unreadable
    assert lookup.admitted_at is None


def test_a_lookup_carries_a_time_exactly_when_it_is_admitted(tmp_path):
    """The invariant that keeps the falsy-timestamp trap unwritable.

    A design where a float means "admitted at t" and False/None mean the other
    two states is one `if not admitted:` away from reading an epoch `0.0` as
    "never admitted" -- and that reading re-posts a re-review on top of a live
    review. An `AdmissionLookup` is always truthy, and these four raises are
    what stop the timestamp and the state from ever disagreeing.
    """
    with pytest.raises(ValueError):
        AdmissionLookup(AdmissionStatus.ADMITTED)
    with pytest.raises(ValueError):
        AdmissionLookup(AdmissionStatus.NOT_ADMITTED, admitted_at=0.0)
    with pytest.raises(ValueError):
        AdmissionLookup(AdmissionStatus.UNREADABLE, admitted_at=ADMITTED_AT)
    assert AdmissionLookup(AdmissionStatus.ADMITTED,
                          admitted_at=0.0).status is AdmissionStatus.ADMITTED, \
        "an epoch 0.0 is an admission like any other, which is the point"


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


def test_a_non_string_title_degrades_to_empty_like_its_seven_siblings(tmp_path):
    """`title` was the one field that skipped `_text`, coercing with a bare
    `str(...)` instead. A JSON null therefore produced `Issue(title='None')`
    -- the literal four-character string -- and because "None" is truthy,
    build_brief's `issue.title or '(untitled)'` fallback never fired and the
    remediation brief read `1. None`. An empty title reaches the placeholder;
    a stringified null does not.
    """
    path = boris_db(tmp_path, validated_result_json=json.dumps({
        "blocking_issues": [{"title": None}, {"title": 42}, {"title": ["a"]}],
        "incomplete": None,
    }))
    verdict = BorisSource(path, RemediationConfig()).new_verdicts()[0]
    assert [i.title for i in verdict.blocking_issues] == ["", "", ""]


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
