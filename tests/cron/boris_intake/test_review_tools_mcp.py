import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from boris_intake.models import (
    GateSnapshot,
    NormalizedEvent,
    ReviewCommand,
    ReviewPolicy,
    WorkflowState,
)
from boris_intake.store import Store


def valid_payload(**changes):
    payload = {
        "summary": "No substantiated blocking issue was found.",
        "evidence_reviewed": [{"kind": "diff", "reference": "complete PR diff"}],
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


def bind_request(tmp_path, monkeypatch, task_id="t_boris_lane"):
    from boris_intake import review_tools_mcp

    db_path = tmp_path / "review.db"
    store = Store(db_path)
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
        policy=ReviewPolicy(ignore_ci=False, rubric_version="2"),
    )
    event = NormalizedEvent("C1", "channel", "U1", "1.0", None, "discarded", False, None)
    snapshot = GateSnapshot("sha256:gate", "2026-08-14T12:00:00Z", ())
    request = store.admit_event(event, (command,), (snapshot,), 1_000_000.0).admitted[0]
    assert store.transition(
        request.request_id,
        WorkflowState.ADMITTED,
        WorkflowState.STATUS_MESSAGE_PENDING,
    )
    assert store.transition(
        request.request_id,
        WorkflowState.STATUS_MESSAGE_PENDING,
        WorkflowState.STARTING_REVIEW,
    )
    assert store.transition(
        request.request_id,
        WorkflowState.STARTING_REVIEW,
        WorkflowState.REVIEWING,
        lane_id=task_id,
    )
    token_path = tmp_path / "github-token"
    token_path.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_PROFILE", "boris_on_demand")
    monkeypatch.setenv("BORIS_REVIEW_DB", str(db_path))
    monkeypatch.setenv("BORIS_GH_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(review_tools_mcp, "GITHUB_RUNNER", None, raising=False)
    return review_tools_mcp, store, request


def github_runner(head="a" * 40, evidence=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": head, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if evidence is not None:
            return SimpleNamespace(returncode=0, stdout=evidence, stderr="")
        return SimpleNamespace(returncode=0, stdout="diff --git a/a b/a", stderr="")

    return run, calls


def test_public_handlers_accept_no_caller_selected_identity_or_destination():
    from boris_intake import review_tools_mcp

    github_params = tuple(inspect.signature(review_tools_mcp.read_github_evidence).parameters)
    acceptance_params = tuple(inspect.signature(review_tools_mcp.read_acceptance_evidence).parameters)
    submission_params = tuple(inspect.signature(review_tools_mcp.submit_review_result).parameters)

    assert github_params == ("operation", "path", "page")
    assert acceptance_params == ("reference",)
    assert submission_params == ("payload",)


def test_submission_tool_exposes_the_complete_judgment_contract():
    from boris_intake import review_tools_mcp

    tools = asyncio.run(review_tools_mcp.server.list_tools())
    schema = next(
        tool for tool in tools if tool.name == "submit_review_result"
    ).inputSchema
    payload = schema["properties"]["payload"]

    assert payload["additionalProperties"] is False
    assert set(payload["required"]) == {
        "summary",
        "evidence_reviewed",
        "verification_limits",
        "blocking_issues",
        "non_blocking_issues",
        "risk_hypotheses",
        "verification_gaps",
        "release_conditions",
        "nits",
    }
    anchor = payload["properties"]["evidence_reviewed"]["items"]
    assert set(anchor["required"]) == {"kind", "reference"}
    assert set(anchor["properties"]) == {"kind", "reference", "detail"}
    assert set(anchor["properties"]["kind"]["enum"]) == {
        "diff",
        "test",
        "ci",
        "ticket",
        "log",
        "documentation",
    }

    issue = payload["properties"]["blocking_issues"]["items"]
    assert set(issue["required"]) == {
        "title",
        "expected_behavior",
        "changed_mechanism",
        "trigger",
        "consequence",
        "impact_scope",
        "evidence_anchors",
        "action",
    }
    assert issue["properties"]["evidence_anchors"]["items"] == anchor

    risk = payload["properties"]["risk_hypotheses"]["items"]
    assert set(risk["required"]) == {
        "title",
        "observed_facts",
        "hypothesized_link",
        "unresolved_link",
        "evidence_anchors",
        "verification_step",
    }
    gap = payload["properties"]["verification_gaps"]["items"]
    assert set(gap["required"]) == {
        "title",
        "unassessed_claim",
        "evidence_inspected",
        "missing_evidence",
        "prevents_core_assessment",
        "next_step",
    }
    assert "reason_code" in gap["properties"]
    release = payload["properties"]["release_conditions"]["items"]
    assert set(release["required"]) == {
        "title",
        "requirement",
        "authoritative_source",
        "phase",
        "completion_criterion",
    }
    assert set(release["properties"]["phase"]["enum"]) == {
        "merge",
        "deployment",
        "migration",
        "rollout",
        "production_validation",
    }
    nit = payload["properties"]["nits"]["items"]
    assert set(nit["required"]) == {"title", "location"}
    assert "suggestion" in nit["properties"]


def test_missing_or_unbound_task_context_fails_closed(tmp_path, monkeypatch):
    from boris_intake import review_tools_mcp

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("BORIS_REVIEW_DB", str(tmp_path / "missing.db"))
    with pytest.raises(review_tools_mcp.ToolError, match="task binding"):
        review_tools_mcp.read_github_evidence("pr_metadata")

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_not_bound")
    monkeypatch.setenv("HERMES_PROFILE", "boris_on_demand")
    with pytest.raises(review_tools_mcp.ToolError, match="database"):
        review_tools_mcp.read_github_evidence("pr_metadata")


def test_github_metadata_is_pinned_to_the_bound_request(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_metadata")

    assert result["operation"] == "pr_metadata"
    assert result["head_sha"] == "a" * 40
    assert result["data"]["title"] == "Example"
    assert len(calls) == 1
    assert "secret" not in json.dumps(result)
    assert "github-token" not in json.dumps(result)


def test_github_evidence_rejects_head_change_unknown_operations_and_extra_paths(
    tmp_path, monkeypatch
):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, calls = github_runner(head="b" * 40)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="head"):
        module.read_github_evidence("pr_diff")
    with pytest.raises(module.ToolError, match="operation"):
        module.read_github_evidence("raw_api")
    with pytest.raises(module.ToolError, match="path"):
        module.read_github_evidence("pr_checks", "README.md")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "path",
    ("../secret", "/etc/passwd", "src/../../secret", "https://evil.test/a", "src\\secret"),
)
def test_file_evidence_rejects_traversal_and_non_repository_paths(tmp_path, monkeypatch, path):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="path"):
        module.read_github_evidence("file_at_head", path)
    assert calls == []


def test_file_evidence_constructs_only_the_pinned_head_route(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, calls = github_runner(evidence=json.dumps({"content": "IyBkb2NzCg=="}))
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("file_at_head", "docs/review guide.md")

    command = calls[-1][0]
    assert "/repos/Ylopo/example/contents/docs/review%20guide.md?ref=" + "a" * 40 in command
    assert result["data"]["content"] == "IyBkb2NzCg=="


def test_check_evidence_accepts_failure_exit_with_valid_json(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)

    def run(command, **kwargs):
        if "view" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps([{"name": "unit-tests", "state": "FAILURE"}]),
            stderr="checks failed",
        )

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    result = module.read_github_evidence("pr_checks")

    assert result["data"][0]["state"] == "FAILURE"


def test_acceptance_evidence_returns_only_a_pre_authorized_reference(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    evidence = {
        "kind": "ticket",
        "reference": "PLAT-123 acceptance criterion 4",
        "content": "The migration must have a rollback rehearsal.",
    }
    store.authorize_acceptance_evidence(
        request.request_id,
        "PLAT-123#acceptance-4",
        evidence,
    )

    assert module.read_acceptance_evidence("PLAT-123#acceptance-4") == evidence
    for unapproved in ("PLAT-999", "https://confluence.example/page/1", "rollback search"):
        with pytest.raises(module.ToolError, match="authorized"):
            module.read_acceptance_evidence(unapproved)


def test_pre_authorized_acceptance_evidence_is_immutable(tmp_path, monkeypatch):
    _, store, request = bind_request(tmp_path, monkeypatch)
    original = {"kind": "ticket", "content": "Original acceptance contract."}
    store.authorize_acceptance_evidence(request.request_id, "PLAT-123#AC1", original)
    store.authorize_acceptance_evidence(request.request_id, "PLAT-123#AC1", original)

    with pytest.raises(ValueError, match="conflict"):
        store.authorize_acceptance_evidence(
            request.request_id,
            "PLAT-123#AC1",
            {"kind": "ticket", "content": "Changed contract."},
        )

    assert store.acceptance_evidence(request.request_id, "PLAT-123#AC1") == original


def test_submission_validates_and_is_idempotent_for_the_bound_task(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    completed = []
    monkeypatch.setattr(module, "_complete_bound_task", lambda task, req, digest: completed.append(task) or True)

    accepted = module.submit_review_result(valid_payload())
    retried = module.submit_review_result(valid_payload())

    assert accepted == {"status": "accepted", "task_completed": True}
    assert retried == {"status": "idempotent", "task_completed": True}
    assert completed == ["t_boris_lane"]
    assert json.loads(store.judgment_for_request(request.request_id))["summary"].startswith("No substantiated")


def test_invalid_submission_returns_retryable_feedback_without_storing_a_judgment(
    tmp_path, monkeypatch
):
    module, store, request = bind_request(tmp_path, monkeypatch)
    invalid = valid_payload(code_owned="not allowed")

    result = module.submit_review_result(invalid)

    assert result["status"] == "validation_error"
    assert result["error"] == "judgment has unknown field code_owned"
    assert result["retry_same_judgment"] is True
    assert store.judgment_for_request(request.request_id) is None


def test_conflicting_submission_never_replaces_the_first_judgment(
    tmp_path, monkeypatch
):
    module, store, request = bind_request(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_complete_bound_task", lambda task, req, digest: True)

    module.submit_review_result(valid_payload())
    first = store.judgment_for_request(request.request_id)
    with pytest.raises(module.ToolError, match="conflicting"):
        module.submit_review_result(valid_payload(summary="A different judgment."))

    assert store.judgment_for_request(request.request_id) == first
    assert "judgment_conflict" in [event["kind"] for event in store.request_events(request.request_id)]


def test_identical_retry_completes_the_task_after_a_completion_failure(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    attempts = []

    def complete(task_id, request_id, digest):
        attempts.append((task_id, request_id, digest))
        return len(attempts) > 1

    monkeypatch.setattr(module, "_complete_bound_task", complete)

    with pytest.raises(module.ToolError, match="retry the identical payload"):
        module.submit_review_result(valid_payload())
    stored = store.judgment_for_request(request.request_id)

    assert module.submit_review_result(valid_payload()) == {
        "status": "idempotent",
        "task_completed": True,
    }
    assert store.judgment_for_request(request.request_id) == stored
    assert len(attempts) == 2


def diff_for(*files):
    parts = []
    for name, lines in files:
        body = "".join(f"+line {n} of {name}\n" for n in range(lines))
        parts.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -0,0 +1,{lines} @@\n{body}")
    return "".join(parts)


def test_paginate_diff_keeps_every_file_across_pages_without_silent_loss(tmp_path, monkeypatch):
    from boris_intake import review_tools_mcp as module

    diff = diff_for(("a.py", 40), ("b.py", 40), ("c.py", 40))
    seen, page, guard = [], 1, 0
    while guard < 20:
        guard += 1
        result = module.paginate_diff(diff, page, budget=900)
        seen.append(result["diff"])
        assert result["all_files"] == ["a.py", "b.py", "c.py"]
        if not result["has_more"]:
            assert result["page"] == result["total_pages"]
            break
        page += 1

    assert result["total_pages"] > 1, "budget should force more than one page"
    assert "".join(seen) == diff


def test_paginate_diff_marks_a_hunk_it_had_to_shorten(tmp_path, monkeypatch):
    from boris_intake import review_tools_mcp as module

    diff = diff_for(("huge.py", 400))
    pages = [module.paginate_diff(diff, n, budget=1_000) for n in (1,)]
    text = pages[0]["diff"]

    assert "lines of this hunk omitted" in text
    assert "file_at_head" in text
    assert pages[0]["files_on_page"] == ["huge.py"]


def test_paginate_diff_rejects_a_page_beyond_the_end(tmp_path, monkeypatch):
    from boris_intake import review_tools_mcp as module

    with pytest.raises(module.ToolError, match="out of range"):
        module.paginate_diff(diff_for(("a.py", 2)), 5, budget=900)


def test_pr_diff_returns_first_page_and_advertises_the_rest(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    big = diff_for(("x.py", 4000), ("y.py", 4000))
    runner, calls = github_runner(evidence=big)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_diff")

    assert result["data"]["page"] == 1
    assert result["data"]["has_more"] is True
    assert result["data"]["all_files"] == ["x.py", "y.py"]
    assert len(result["data"]["diff"]) <= module._DIFF_PAGE_BUDGET


def test_pr_diff_rejects_page_on_other_operations(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="page is valid only"):
        module.read_github_evidence("pr_checks", None, 2)
    with pytest.raises(module.ToolError, match="positive integer"):
        module.read_github_evidence("pr_diff", None, 0)
