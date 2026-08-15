import importlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from boris_intake.models import (
    GateSnapshot,
    NormalizedEvent,
    ReviewCommand,
    ReviewPolicy,
)
from boris_intake.store import Store


def result_module():
    return importlib.import_module("boris_intake.result")


def valid_payload(**changes):
    payload = {
        "summary": "No substantiated blocking issue was found.",
        "evidence_reviewed": [
            {"kind": "diff", "reference": "complete PR diff"},
            {"kind": "ci", "reference": "exact-head check rollup"},
        ],
        "verification_limits": ["Static review; code was not executed locally."],
        "blocking_issues": [],
        "non_blocking_issues": [],
        "risk_hypotheses": [],
        "verification_gaps": [],
        "release_conditions": [],
        "nits": [],
    }
    payload.update(changes)
    return payload


def issue(title="Authorization bypass"):
    return {
        "title": title,
        "expected_behavior": "Only the owner may update the record.",
        "changed_mechanism": "The ownership predicate was removed.",
        "trigger": "A signed-in non-owner submits the update endpoint.",
        "consequence": "The non-owner changes another user's record.",
        "impact_scope": "Any record addressable by ID.",
        "evidence_anchors": [
            {"kind": "diff", "reference": "src/update.py:42"}
        ],
        "action": "Restore the ownership predicate before the write.",
    }


def risk(title="Retry may duplicate publication"):
    return {
        "title": title,
        "observed_facts": "The retry calls the publication adapter again.",
        "hypothesized_link": "A timeout after success could cause a second write.",
        "unresolved_link": "The provider's idempotency behavior is not shown.",
        "evidence_anchors": [
            {"kind": "diff", "reference": "src/publish.py:18"}
        ],
        "verification_step": "Inject a timeout after provider success and retry.",
    }


def gap(title="Migration rollback unassessed", *, core=False, reason="MISSING_EVIDENCE"):
    value = {
        "title": title,
        "unassessed_claim": "The migration can be rolled back without data loss.",
        "evidence_inspected": [
            {"kind": "documentation", "reference": "migration plan"}
        ],
        "missing_evidence": "No rollback rehearsal or inverse migration is present.",
        "prevents_core_assessment": core,
        "next_step": "Run the rollback rehearsal and attach its log.",
    }
    if core:
        value["reason_code"] = reason
    return value


def release_condition():
    return {
        "title": "Production backfill",
        "requirement": "Run the backfill after deployment.",
        "authoritative_source": "PLAT-123 acceptance criterion 4",
        "phase": "deployment",
        "completion_criterion": "The backfill reports zero failed rows.",
    }


def nit(title="Rename local variable"):
    return {"title": title, "location": "src/update.py:12", "suggestion": "Use owner_id."}


def lifecycle_request(tmp_path, *, ignore_ci=False, snapshot=None):
    store = Store(tmp_path / "review.db")
    policy = ReviewPolicy(ignore_ci=ignore_ci, rubric_version="2")
    command = ReviewCommand(
        owner="Ylopo",
        repo="example",
        pr_number=42,
        head_sha="a" * 40,
        pr_author="octocat",
        requester_user_id="U1",
        requester_email="a@ylopo.com",
        channel_id="C1",
        message_ts="1.0",
        root_thread_ts="1.0",
        permalink="https://ylopo.slack.com/archives/C1/p10",
        policy=policy,
    )
    event = NormalizedEvent("C1", "channel", "U1", "1.0", None, "discarded", False, None)
    gate = snapshot or GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())
    return store.admit_event(event, (command,), (gate,), 1_000_000.0).admitted[0]


def registry(module):
    return module.load_gate_registry(
        Path(__file__).resolve().parents[3]
        / "cron"
        / "scripts"
        / "boris_intake"
        / "gate_registry.json"
    )


def checks_snapshot(module, rows, captured_at="2026-08-14T12:00:00Z"):
    return module.build_gate_snapshot(rows, captured_at, registry(module))


def test_validator_accepts_only_the_judgment_contract():
    module = result_module()

    judgment = module.validate_judgment(valid_payload())

    assert judgment.summary == "No substantiated blocking issue was found."
    assert [item.kind for item in judgment.evidence_reviewed] == ["ci", "diff"]


@pytest.mark.parametrize(
    "code_owned_field",
    (
        "schema_version",
        "request_id",
        "target",
        "policy",
        "gate_snapshot",
        "blocking_gate_conditions",
        "waived_ci_conditions",
        "incomplete",
        "derived",
        "result_hash",
    ),
)
def test_validator_rejects_code_owned_or_unknown_fields(code_owned_field):
    module = result_module()
    payload = valid_payload()
    payload[code_owned_field] = "attacker supplied"

    with pytest.raises(module.ValidationError, match="unknown field"):
        module.validate_judgment(payload)


@pytest.mark.parametrize("forbidden", ("confidence", "severity"))
def test_validator_rejects_forbidden_issue_ranking_fields(forbidden):
    module = result_module()
    observation = issue()
    observation[forbidden] = "high"

    with pytest.raises(module.ValidationError, match="unknown field"):
        module.validate_judgment(valid_payload(blocking_issues=[observation]))


def test_validator_requires_the_complete_issue_evidence_chain():
    module = result_module()
    observation = issue()
    del observation["trigger"]

    with pytest.raises(module.ValidationError, match="trigger"):
        module.validate_judgment(valid_payload(blocking_issues=[observation]))


def test_validator_rejects_an_issue_without_evidence_anchors():
    module = result_module()
    observation = issue()
    observation["evidence_anchors"] = []

    with pytest.raises(module.ValidationError, match="evidence_anchors"):
        module.validate_judgment(valid_payload(blocking_issues=[observation]))


def test_validator_rejects_exact_duplicate_issues_across_categories():
    module = result_module()
    observation = issue()

    with pytest.raises(module.ValidationError, match="duplicate observation"):
        module.validate_judgment(
            valid_payload(
                blocking_issues=[observation], non_blocking_issues=[observation]
            )
        )


def test_core_verification_gap_requires_a_reason_code():
    module = result_module()
    observation = gap(core=True)
    del observation["reason_code"]

    with pytest.raises(module.ValidationError, match="reason_code"):
        module.validate_judgment(valid_payload(verification_gaps=[observation]))


def test_non_core_gap_cannot_claim_an_incomplete_reason_code():
    module = result_module()
    observation = gap(core=False)
    observation["reason_code"] = "NOT_CORE"

    with pytest.raises(module.ValidationError, match="reason_code"):
        module.validate_judgment(valid_payload(verification_gaps=[observation]))


def test_validator_enforces_renderer_safe_payload_bounds():
    module = result_module()

    with pytest.raises(module.ValidationError, match="summary"):
        module.validate_judgment(valid_payload(summary="x" * 5000))


def test_canonical_judgment_is_stable_across_collection_order():
    module = result_module()
    first = valid_payload(
        evidence_reviewed=list(reversed(valid_payload()["evidence_reviewed"])),
        verification_limits=["z limit", "a limit"],
        nits=[nit("Zulu"), nit("Alpha")],
    )
    second = valid_payload(
        verification_limits=["a limit", "z limit"],
        nits=[nit("Alpha"), nit("Zulu")],
    )

    assert module.canonicalize_judgment(module.validate_judgment(first)) == (
        module.canonicalize_judgment(module.validate_judgment(second))
    )


def test_gate_snapshot_fingerprint_is_sorted_and_excludes_capture_time():
    module = result_module()
    rows = [
        {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit", "required": True},
        {"name": "CodeQL", "state": "SUCCESS", "link": "https://ci/codeql", "required": True},
    ]

    first = checks_snapshot(module, rows, "2026-08-14T12:00:00Z")
    second = checks_snapshot(module, list(reversed(rows)), "2026-08-14T13:00:00Z")

    assert first.fingerprint == second.fingerprint
    assert [check.check_name for check in first.checks] == ["CodeQL", "unit-tests"]
    assert first.captured_at != second.captured_at


def test_ignore_ci_waives_only_failed_ordinary_required_checks():
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit", "required": True},
    ])

    blocked = module.apply_gate_policy(snapshot, ReviewPolicy(False))
    waived = module.apply_gate_policy(snapshot, ReviewPolicy(True))

    assert [condition.kind for condition in blocked.blocking_conditions] == ["required_ci"]
    assert waived.blocking_conditions == ()
    assert [condition.check_name for condition in waived.waived_conditions] == ["unit-tests"]


@pytest.mark.parametrize("name", ("CodeQL", "Snyk Security"))
def test_security_failures_are_never_waived(name):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": name, "state": "FAILURE", "link": "https://ci/security", "required": True},
    ])

    assessment = module.apply_gate_policy(snapshot, ReviewPolicy(True))

    assert [condition.kind for condition in assessment.blocking_conditions] == ["security"]
    assert assessment.waived_conditions == ()


def test_unknown_required_check_fails_closed_and_is_not_waived():
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "new-vendor-gate", "state": "FAILURE", "link": "https://ci/new", "required": True},
    ])

    assessment = module.apply_gate_policy(snapshot, ReviewPolicy(True))

    assert [condition.kind for condition in assessment.blocking_conditions] == [
        "unclassified_required"
    ]
    assert assessment.waived_conditions == ()


@pytest.mark.parametrize("state", ("PENDING", "QUEUED", "IN_PROGRESS"))
def test_pending_required_check_waits_even_when_ci_is_ignored(state):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": state, "link": "https://ci/unit", "required": True},
    ])

    assessment = module.apply_gate_policy(snapshot, ReviewPolicy(True))

    assert [check.check_name for check in assessment.pending_checks] == ["unit-tests"]
    assert assessment.blocking_conditions == ()
    assert assessment.waived_conditions == ()


def test_blocking_issue_wins_over_an_unassessed_area(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(
        valid_payload(blocking_issues=[issue()], verification_gaps=[gap(core=True)])
    )

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert assembled.incomplete.origin == "model"
    assert assembled.derived.outcome == "ISSUES_FOUND"
    assert assembled.derived.presentation == "ISSUES"


def test_multiple_core_gaps_derive_one_sorted_incomplete_object(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(valid_payload(verification_gaps=[
        gap("Z gap", core=True, reason="Z_REASON"),
        gap("A gap", core=True, reason="A_REASON"),
    ]))

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert assembled.derived.outcome == "REVIEW_INCOMPLETE"
    assert assembled.incomplete.reason_codes == ("A_REASON", "Z_REASON")
    assert "Run the rollback rehearsal" in assembled.incomplete.retry_guidance


def test_system_failure_produces_valid_incomplete_with_empty_observations(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    failure = module.SystemFailure(
        reason_code="LANE_TIMEOUT",
        detail="The reviewer lane timed out before valid submission.",
        retry_guidance="Repost the PR link to retry.",
    )

    assembled = module.assemble_result(
        request, None, request.gate_snapshot, failure, "2026-08-14T12:01:00Z"
    )

    assert assembled.derived.outcome == "REVIEW_INCOMPLETE"
    assert assembled.incomplete.origin == "system"
    assert assembled.blocking_issues == ()
    assert assembled.non_blocking_issues == ()


def test_system_failure_takes_precedence_over_pending_checks(tmp_path):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": "IN_PROGRESS", "link": "https://ci/unit", "required": True},
    ])
    request = lifecycle_request(tmp_path, snapshot=snapshot)

    assembled = module.assemble_result(
        request,
        None,
        snapshot,
        module.SystemFailure("LANE_TIMEOUT", "The reviewer lane timed out.", "Retry the request."),
        "2026-08-14T12:01:00Z",
    )

    assert assembled.derived.outcome == "REVIEW_INCOMPLETE"
    assert assembled.incomplete.reason_codes == ("LANE_TIMEOUT",)


def test_gate_only_failure_is_issues_found_without_a_code_issue(tmp_path):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit", "required": True},
    ])
    request = lifecycle_request(tmp_path, snapshot=snapshot)

    assembled = module.assemble_result(
        request,
        module.validate_judgment(valid_payload()),
        snapshot,
        None,
        "2026-08-14T12:01:00Z",
    )

    assert assembled.derived.outcome == "ISSUES_FOUND"
    assert assembled.derived.presentation == "GATE_BLOCKED"
    assert assembled.blocking_issues == ()
    assert len(assembled.blocking_gate_conditions) == 1


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"non_blocking_issues": [issue()]}, "READY_WITH_COMMENTS"),
        ({"risk_hypotheses": [risk()]}, "READY_WITH_COMMENTS"),
        ({"verification_gaps": [gap()]}, "READY_WITH_COMMENTS"),
        ({"release_conditions": [release_condition()]}, "READY_WITH_COMMENTS"),
        ({"nits": [nit()]}, "READY_WITH_COMMENTS"),
        ({}, "READY_CLEAN"),
    ),
)
def test_ready_presentation_preserves_comments_and_nits(tmp_path, changes, expected):
    module = result_module()
    request = lifecycle_request(tmp_path)

    assembled = module.assemble_result(
        request,
        module.validate_judgment(valid_payload(**changes)),
        request.gate_snapshot,
        None,
        "2026-08-14T12:01:00Z",
    )

    assert assembled.derived.outcome == "READY_FOR_HUMAN_REVIEW"
    assert assembled.derived.presentation == expected


def test_unused_ignore_ci_on_green_checks_stays_clean_ready(tmp_path):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": "SUCCESS", "link": "https://ci/unit", "required": True},
    ])
    request = lifecycle_request(tmp_path, ignore_ci=True, snapshot=snapshot)

    assembled = module.assemble_result(
        request,
        module.validate_judgment(valid_payload()),
        snapshot,
        None,
        "2026-08-14T12:01:00Z",
    )

    assert assembled.waived_ci_conditions == ()
    assert assembled.derived.presentation == "READY_CLEAN"


def test_result_hash_and_rendering_are_stable_across_input_order(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    first = module.validate_judgment(valid_payload(nits=[nit("Zulu"), nit("Alpha")]))
    second = module.validate_judgment(valid_payload(nits=[nit("Alpha"), nit("Zulu")]))

    a = module.assemble_result(
        request, first, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )
    b = module.assemble_result(
        request, second, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert a.result_hash == b.result_hash
    assert module.render_github(a) == module.render_github(b)


def test_canonical_result_uses_the_version_two_gate_shape(tmp_path):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": "SUCCESS", "link": "https://ci/unit", "required": True},
    ])
    request = lifecycle_request(tmp_path, snapshot=snapshot)
    assembled = module.assemble_result(
        request,
        module.validate_judgment(valid_payload()),
        snapshot,
        None,
        "2026-08-14T12:01:00Z",
    )

    payload = json.loads(module.canonicalize_result(assembled))

    assert payload["schema_version"] == 2
    assert payload["gate_snapshot"]["checks"][0]["class"] == "ordinary"
    assert "check_class" not in payload["gate_snapshot"]["checks"][0]
    assert payload["incomplete"] is None
    assert payload["result_hash"] == assembled.result_hash


def test_renderers_keep_gate_failure_hypothesis_and_release_condition_distinct(tmp_path):
    module = result_module()
    snapshot = checks_snapshot(module, [
        {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit", "required": True},
    ])
    request = lifecycle_request(tmp_path, snapshot=snapshot)
    judgment = module.validate_judgment(valid_payload(
        risk_hypotheses=[risk()], release_conditions=[release_condition()]
    ))
    assembled = module.assemble_result(
        request, judgment, snapshot, None, "2026-08-14T12:01:00Z"
    )

    github = module.render_github(assembled)
    slack = module.render_slack(assembled, "https://github.com/review/1")

    assert "## Blocking Gate Conditions" in github
    assert "Check status alone does not assert a code defect." in github
    assert "## Risk Hypotheses — Unconfirmed" in github
    assert "## Release Conditions" in github
    assert "concern" not in github.lower()
    assert "Required checks blocking: unit-tests" in slack


def test_incomplete_partial_observations_publish_to_github_but_not_into_slack(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(valid_payload(
        non_blocking_issues=[issue("Minor defect")], verification_gaps=[gap(core=True)]
    ))
    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert "Minor defect" in module.render_github(assembled)
    slack = module.render_slack(assembled, None)
    assert "Minor defect" not in slack
    assert "published in the GitHub review" in slack


def test_incomplete_review_with_other_findings_still_publishes_to_github(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(valid_payload(
        verification_gaps=[gap(core=True, reason="CROSS_REPO_STATUS_EVIDENCE_UNAVAILABLE")],
        non_blocking_issues=[issue("Duplicate ledger row")],
        nits=[nit()],
    ))

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )
    body = module.render_github(assembled)

    assert assembled.derived.outcome == "REVIEW_INCOMPLETE"
    assert assembled.derived.presentation == "INCOMPLETE_WITH_FINDINGS"
    assert "Core assessment was prevented" in body
    assert "Duplicate ledger row" in body
    assert "Rename local variable" in body


def test_incomplete_review_with_nothing_else_stays_audit_only(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(
        valid_payload(verification_gaps=[gap(core=True)])
    )

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert assembled.derived.presentation == "INCOMPLETE"
    with pytest.raises(module.ValidationError, match="audit-only"):
        module.render_github(assembled)


def test_system_failure_never_publishes_even_though_it_is_incomplete(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    failure = module.SystemFailure("LANE_NO_VALID_RESULT", "Lane died.", "Repost the link.")

    assembled = module.assemble_result(
        request, None, request.gate_snapshot, failure, "2026-08-14T12:01:00Z"
    )

    assert assembled.derived.presentation == "INCOMPLETE"
    with pytest.raises(module.ValidationError, match="audit-only"):
        module.render_github(assembled)


def test_slack_reports_publication_instead_of_claiming_findings_were_withheld(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(valid_payload(
        verification_gaps=[gap(core=True)],
        non_blocking_issues=[issue("Duplicate ledger row")],
    ))

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )
    text = module.render_slack(assembled, "https://github.com/Ylopo/example/pull/42#r1")

    assert "were not published" not in text
    assert "published in the GitHub review" in text
    assert "https://github.com/Ylopo/example/pull/42#r1" in text


def test_a_core_gap_alone_does_not_count_as_a_publishable_finding(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(valid_payload(verification_gaps=[
        gap("core one", core=True, reason="A"),
        gap("core two", core=True, reason="B"),
    ]))

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert assembled.derived.presentation == "INCOMPLETE"


def test_a_noncore_gap_alongside_a_core_gap_is_worth_publishing(tmp_path):
    module = result_module()
    request = lifecycle_request(tmp_path)
    judgment = module.validate_judgment(valid_payload(verification_gaps=[
        gap("core one", core=True, reason="A"),
        gap("soft one", core=False),
    ]))

    assembled = module.assemble_result(
        request, judgment, request.gate_snapshot, None, "2026-08-14T12:01:00Z"
    )

    assert assembled.derived.presentation == "INCOMPLETE_WITH_FINDINGS"
