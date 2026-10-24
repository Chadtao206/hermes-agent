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
    tool = next(tool for tool in tools if tool.name == "submit_review_result")
    # mcp 2.0 renamed Tool.inputSchema -> input_schema; accept both.
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
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
    contents = {
        "type": "file",
        "path": "docs/review guide.md",
        "sha": "f" * 40,
        "size": 7,
        "content": "IyBkb2NzCg==\n",
        "encoding": "base64",
    }
    runner, calls = github_runner(evidence=json.dumps(contents))
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("file_at_head", "docs/review guide.md")

    command = calls[-1][0]
    assert "/repos/Ylopo/example/contents/docs/review%20guide.md?ref=" + "a" * 40 in command
    assert result["data"]["content"] == "# docs\n"


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


def test_completion_calls_the_real_kanban_complete_task(tmp_path, monkeypatch):
    """Regression: upstream v0.21.0 dropped the fork-only ``operation_id`` kwarg
    from ``kanban_db.complete_task``. The MCP tool kept passing it, the call raised
    TypeError inside ``_complete_bound_task``, the judgment was accepted but the
    lane never completed, and the dispatcher re-ran every review three more times
    as a protocol violation (2026-09-06). Exercise the real kanban_db, not a stub."""
    from hermes_cli import kanban_db, kanban_db_connect

    module, _store, request = bind_request(tmp_path, monkeypatch)
    db_path = kanban_db_connect.init_db(tmp_path / "kanban.db")
    with kanban_db_connect.connect_closing(db_path) as conn:
        task_id = kanban_db.create_task(
            conn, title="Read-only Boris review: Ylopo/example#42 @ aaaaaaa",
            assignee="boris_on_demand",
        )
        assert kanban_db.claim_task(conn, task_id, claimer="test-dispatcher:1") is not None
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
    assert run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    assert module._complete_bound_task(task_id, request.request_id, "sha256:judgment") is True

    with kanban_db_connect.connect_closing(db_path) as conn:
        status, current = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        outcome = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    assert (status, current, outcome) == ("done", None, "completed")


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
    runner, _calls = github_runner(evidence=big)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_diff")

    assert result["data"]["page"] == 1
    assert result["data"]["has_more"] is True
    assert result["data"]["all_files"] == ["x.py", "y.py"]
    assert _hermes_rendered_size(result) <= _mcp_threshold()


TOO_LARGE = (
    "could not find pull request diff: HTTP 406: Sorry, the diff exceeded the "
    "maximum number of lines (20000) (https://api.github.com/repos/o/r/pulls/42)\n"
    "PullRequest.diff too_large"
)


def files_api_runner(pages, *, head="a" * 40, diff_stderr=TOO_LARGE):
    """A gh runner whose `pr diff` is refused but whose changed-file pages answer."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "view" in command:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"headRefOid": head}), stderr=""
            )
        if "diff" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr=diff_stderr)
        route = command[-1]
        page = int(route.split("page=")[-1])
        payload = pages[page - 1] if page <= len(pages) else []
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    return run, calls


def test_pr_diff_falls_back_to_changed_files_when_github_refuses_the_diff(
    tmp_path, monkeypatch
):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    pages = [
        [
            {
                "filename": "src/app.py",
                "status": "modified",
                "changes": 2,
                "patch": "@@ -1,1 +1,2 @@\n-old\n+new",
            }
        ]
    ]
    runner, calls = files_api_runner(pages)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_diff")

    assert result["data"]["all_files"] == ["src/app.py"]
    assert result["data"]["source"] == "changed_files_api"
    assert "diff --git a/src/app.py b/src/app.py" in result["data"]["diff"]
    assert "+new" in result["data"]["diff"]
    assert any("/pulls/42/files" in part for call in calls for part in call)


def test_changed_file_fallback_names_files_whose_patch_github_withheld(
    tmp_path, monkeypatch
):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    pages = [
        [
            {"filename": "big.ts", "status": "modified", "changes": 9000},
            {"filename": "gone.ts", "status": "removed", "changes": 3549},
        ]
    ]
    runner, _calls = files_api_runner(pages)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    diff = module.read_github_evidence("pr_diff")["data"]["diff"]

    assert "big.ts" in diff and "file_at_head" in diff
    # A file deleted by the pull request cannot be re-read at head; say so rather
    # than pointing the reviewer at a tool that will fail.
    assert "gone.ts" in diff
    gone = diff.split("diff --git a/gone.ts")[-1]
    assert "file_at_head" not in gone


def test_changed_file_fallback_walks_every_page(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    full = [
        {"filename": f"f{n}.py", "status": "modified", "changes": 1, "patch": "@@ -1 +1 @@\n+x"}
        for n in range(module._FILES_PER_PAGE)
    ]
    tail = [{"filename": "last.py", "status": "added", "changes": 1, "patch": "@@ -0,0 +1 @@\n+y"}]
    runner, _calls = files_api_runner([full, tail])
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_diff")

    assert "last.py" in result["data"]["all_files"]
    assert len(result["data"]["all_files"]) == module._FILES_PER_PAGE + 1


def test_pr_diff_does_not_fall_back_for_unrelated_github_failures(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, calls = files_api_runner([[]], diff_stderr="gh: authentication failed")
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="authentication failed"):
        module.read_github_evidence("pr_diff")
    assert not any("files" in part for call in calls for part in call)


def test_github_failures_report_why_github_refused(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)

    def run(command, **kwargs):
        if "view" in command and "headRefOid" in command[-1]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"headRefOid": "a" * 40}), stderr=""
            )
        return SimpleNamespace(returncode=4, stdout="", stderr="HTTP 502: upstream is unhappy")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    with pytest.raises(module.ToolError, match="upstream is unhappy") as caught:
        module.read_github_evidence("file_at_head", "src/app.py")
    assert caught.value.returncode == 4


def test_pr_diff_rejects_page_on_other_operations(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="page is valid only"):
        module.read_github_evidence("pr_checks", None, 2)
    with pytest.raises(module.ToolError, match="positive integer"):
        module.read_github_evidence("pr_diff", None, 0)


# --- Tool surface the model can actually learn from -----------------------------------
#
# The lane's user message is the bare "work kanban task <id>" and the kanban toolset is
# disabled, so the card body never reaches the model. Everything it knows about these
# tools comes from SOUL.md and the tool schema; every tool and parameter must carry a
# description that states what the operation returns and how to call it.


def _listed_tools(module):
    return {tool.name: tool for tool in asyncio.run(module.server.list_tools())}


def _input_schema(tool):
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)


def test_every_tool_and_parameter_carries_a_description():
    from boris_intake import review_tools_mcp

    tools = _listed_tools(review_tools_mcp)
    assert set(tools) == {"read_github_evidence", "read_acceptance_evidence", "submit_review_result"}
    for name, tool in tools.items():
        assert tool.description and len(tool.description) > 80, name
        for param, schema in _input_schema(tool)["properties"].items():
            assert schema.get("description"), f"{name}.{param} has no description"

    github = tools["read_github_evidence"].description
    for fact in ("pr_metadata", "review_context", "has_more", "file_at_head", "pr_comments"):
        assert fact in github
    # pr_metadata no longer carries comments/reviews (they made the first call of every
    # lane unbounded); the description must not call pr_comments redundant.
    assert "already returned" not in github
    assert "delete" in github  # all_files includes files the diff deletes
    everything = " ".join(
        text
        for tool in tools.values()
        for text in [tool.description or ""]
        + [str(schema.get("description", "")) for schema in _input_schema(tool)["properties"].values()]
    )
    assert "same data as pr_metadata" not in everything
    acceptance = tools["read_acceptance_evidence"].description
    assert "acceptance_references" in acceptance and "pr_metadata" in acceptance
    submit = tools["submit_review_result"].description
    assert "validation_error" in submit and "accepted" in submit


def _contents_runner(head="a" * 40, **contents):
    """GitHub runner whose /contents/ route answers with a contents-API object."""
    import base64

    calls = []
    encoded = contents.copy()
    if "text" in encoded:
        raw = encoded.pop("text").encode()
        encoded.setdefault("content", base64.b64encode(raw).decode() + "\n")
        encoded.setdefault("encoding", "base64")
        encoded.setdefault("size", len(raw))
    if "raw" in encoded:
        raw = encoded.pop("raw")
        encoded.setdefault("content", base64.b64encode(raw).decode())
        encoded.setdefault("encoding", "base64")
        encoded.setdefault("size", len(raw))
    encoded.setdefault("type", "file")
    encoded.setdefault("sha", "f" * 40)
    encoded.setdefault("path", "src/app.py")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": head, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(encoded), stderr="")

    return run, calls


def test_file_evidence_returns_decoded_text_not_base64(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    text = "import os\n\ndef main():\n    return os.getcwd()\n"
    runner, _calls = _contents_runner(text=text)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("file_at_head", "src/app.py")["data"]

    assert data["content"] == text
    assert data["encoding"] == "utf-8"
    assert data["path"] == "src/app.py"
    assert data["size"] == len(text.encode())
    assert data["total_lines"] == 4
    assert (data["page"], data["total_pages"], data["has_more"]) == (1, 1, False)
    assert data["line_range"] == [1, 4]
    assert "IyBk" not in json.dumps(data)  # no base64 leaks through
    for raw_field in ("git_url", "download_url", "_links", "html_url", "url"):
        assert raw_field not in data


def test_file_evidence_pages_a_large_file_by_whole_lines(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    lines = [f"line {i:03d} {'x' * 20}\n" for i in range(1, 241)]
    text = "".join(lines)
    monkeypatch.setattr(module, "_RESULT_BUDGET", 3_000)
    runner, _calls = _contents_runner(text=text)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    first = module.read_github_evidence("file_at_head", "src/app.py", 1)["data"]
    assert first["has_more"] is True
    assert first["total_pages"] > 1
    assert first["line_range"][0] == 1
    assert _hermes_rendered_size(first) <= 3_000

    seen = ""
    last_end = 0
    for page in range(1, first["total_pages"] + 1):
        data = module.read_github_evidence("file_at_head", "src/app.py", page)["data"]
        assert data["line_range"][0] == last_end + 1
        assert data["content"].endswith("\n")
        seen += data["content"]
        last_end = data["line_range"][1]
    assert seen == text
    assert last_end == 240
    assert data["has_more"] is False

    with pytest.raises(module.ToolError, match="out of range"):
        module.read_github_evidence("file_at_head", "src/app.py", first["total_pages"] + 1)


def test_file_evidence_shortens_a_single_line_longer_than_a_page(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_RESULT_BUDGET", 3_000)
    text = "short\n" + "m" * 5_000 + "\nafter\n"
    runner, _calls = _contents_runner(text=text)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    pages = []
    page = 1
    while True:
        data = module.read_github_evidence("file_at_head", "src/app.py", page)["data"]
        pages.append(data)
        if not data["has_more"]:
            break
        page += 1

    joined = "".join(p["content"] for p in pages)
    assert joined.startswith("short\n") and joined.endswith("\nafter\n")
    import re

    marker = re.search(r" \[boris: (\d+) chars of this line omitted\]\n", joined)
    assert marker, joined[:300]
    kept = joined.split("\n")[1].split(" [boris:")[0]
    assert set(kept) == {"m"}
    assert len(kept) + int(marker.group(1)) == 5_000, "omitted count must be exact"
    assert all(_hermes_rendered_size(p) <= 3_000 for p in pages)
    assert pages[-1]["line_range"][1] == 3
    assert pages[-1]["total_lines"] == 3


def test_file_evidence_flags_binary_content_instead_of_dumping_it(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(raw=b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe", path="logo.png")
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("file_at_head", "logo.png")["data"]

    assert data["binary"] is True
    assert data["content"] is None
    assert data["size"] == 12
    assert "note" in data


def test_file_evidence_lists_a_directory(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    listing = [
        {"name": "app.py", "path": "src/app.py", "type": "file", "size": 120, "sha": "1" * 40,
         "url": "u", "git_url": "g", "html_url": "h", "download_url": "d", "_links": {}},
        {"name": "lib", "path": "src/lib", "type": "dir", "size": 0, "sha": "2" * 40,
         "url": "u", "git_url": "g", "html_url": "h", "download_url": None, "_links": {}},
    ]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": "a" * 40, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    data = module.read_github_evidence("file_at_head", "src")["data"]

    assert data["type"] == "dir"
    assert data["path"] == "src"
    assert data["entries"] == [
        {"name": "app.py", "type": "file", "size": 120},
        {"name": "lib", "type": "dir", "size": 0},
    ]
    assert data["truncated"] is False and data["total_entries"] == 2
    assert "git_url" not in json.dumps(data)


def test_file_evidence_names_a_missing_path_plainly(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)

    def run(command, **kwargs):
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": "a" * 40, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(
            returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)\n{\"message\":\"Not Found\"}"
        )

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    with pytest.raises(module.ToolError, match=r"src/missing\.py.*not exist at the pinned head"):
        module.read_github_evidence("file_at_head", "src/missing.py")


def test_file_evidence_reports_content_github_withholds(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(content="", encoding="none", size=3_000_000, path="big.bin")
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="1 MiB"):
        module.read_github_evidence("file_at_head", "big.bin")


def test_page_is_accepted_for_file_at_head_but_still_refused_elsewhere(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(text="one\n")
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    assert module.read_github_evidence("file_at_head", "src/app.py", 1)["data"]["page"] == 1
    with pytest.raises(module.ToolError, match="page is valid only"):
        module.read_github_evidence("pr_comments", None, 2)


# --- Request context the card body used to carry ----------------------------------------


def test_metadata_carries_review_context_for_a_first_review(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_metadata")

    context = result["review_context"]
    assert context["policy"] == {"rubric_version": "2", "ignore_ci": False}
    assert context["acceptance_references"] == []
    assert context["prior_review"] is None
    # GitHub's payload stays where it was; the context is workflow-owned, not evidence.
    assert result["data"]["title"] == "Example"
    assert "review_context" not in result["data"]

    store.authorize_acceptance_evidence(request.request_id, "PLAT-123", {"content": "AC"})
    store.authorize_acceptance_evidence(request.request_id, "LAB-7", {"content": "AC"})
    context = module.read_github_evidence("pr_metadata")["review_context"]
    assert context["acceptance_references"] == ["LAB-7", "PLAT-123"]


def _finish_prior_review(store, request, *, head, outcome, blocking, non_blocking=(), at):
    """An earlier request for the same PR that already produced a judgment."""
    command = ReviewCommand(
        owner=request.owner,
        repo=request.repo,
        pr_number=request.pr_number,
        head_sha=head,
        pr_author=request.pr_author,
        requester_user_id="U1",
        requester_email="a@ylopo.com",
        channel_id="C1",
        message_ts="0.5",
        root_thread_ts="0.5",
        permalink="https://ylopo.slack.com/archives/C1/p05",
        policy=ReviewPolicy(ignore_ci=False, rubric_version="2"),
    )
    event = NormalizedEvent("C1", "channel", "U1", "0.5", None, "discarded", False, None)
    snapshot = GateSnapshot("sha256:gate-prior", "2026-08-14T11:00:00Z", ())
    prior = store.admit_event(event, (command,), (snapshot,), 900_000.0).admitted[0]
    result = {
        "derived": {"outcome": outcome, "presentation": "ISSUES"},
        "assembled_at": at,
        "blocking_issues": [{"title": title} for title in blocking],
        "non_blocking_issues": [{"title": title} for title in non_blocking],
    }
    store._db.execute(
        "UPDATE review_requests SET validated_result_json=?, workflow_state='complete', "
        "completed_at=? WHERE request_id=?",
        (json.dumps(result), 900_500.0, prior.request_id),
    )
    store._db.commit()
    return prior


def test_metadata_review_context_carries_the_prior_review_as_data(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    _finish_prior_review(
        store,
        request,
        head="b" * 40,
        outcome="ISSUES_FOUND",
        blocking=("Ownership predicate removed",),
        non_blocking=("Log line spelling",),
        at="2026-09-05T10:00:00Z",
    )
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    prior = module.read_github_evidence("pr_metadata")["review_context"]["prior_review"]

    assert prior == {
        "head_sha": "b" * 40,
        "same_head": False,
        "outcome": "ISSUES_FOUND",
        "gate_only": False,
        "assembled_at": "2026-09-05T10:00:00Z",
        "blocking_titles": ["Ownership predicate removed"],
        "non_blocking_titles": ["Log line spelling"],
    }


def test_metadata_review_context_marks_a_prior_review_of_the_same_head(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    _finish_prior_review(
        store, request, head=request.head_sha, outcome="READY_FOR_HUMAN_REVIEW", blocking=(),
        at="2026-09-05T10:00:00Z",
    )
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    prior = module.read_github_evidence("pr_metadata")["review_context"]["prior_review"]

    assert prior["same_head"] is True
    assert prior["blocking_titles"] == []


def test_unauthorized_acceptance_reference_names_what_is_authorized(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)

    with pytest.raises(module.ToolError, match="no acceptance evidence is authorized"):
        module.read_acceptance_evidence("PLAT-999")

    store.authorize_acceptance_evidence(request.request_id, "PLAT-123", {"content": "AC"})
    with pytest.raises(module.ToolError, match=r"authorized references: PLAT-123"):
        module.read_acceptance_evidence("https://ylopo.atlassian.net/browse/PLAT-123")


# --- Results must fit Hermes' MCP spill threshold ------------------------------------------
#
# Hermes persists (or, without a file tool, truncates) any mcp_ tool result whose rendered
# handler output exceeds tools.budget_config.DEFAULT_MCP_RESULT_SIZE_CHARS. The rendered
# form is the MCP server's indent=2 JSON of the return value, JSON-encoded once more by
# Hermes, so quotes, backslashes and control characters inflate. A page that spills is
# unreadable to this lane, which has no read_file.


def _hermes_rendered_size(payload) -> int:
    """Size Hermes compares against the spill threshold for a tool return value."""
    import pydantic_core
    from mcp.types import CallToolResult, TextContent

    from tools.mcp_tool_handlers import _render_call_tool_result

    text = pydantic_core.to_json(payload, fallback=str, indent=2).decode()
    result = CallToolResult(content=[TextContent(type="text", text=text)])
    return len(_render_call_tool_result(result, "boris_review"))


def _mcp_threshold() -> int:
    from tools.budget_config import DEFAULT_BUDGET
    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    return int(DEFAULT_BUDGET.resolve_threshold(mcp_prefixed_tool_name("boris_review", "read_github_evidence")))


def _worst_case_text(lines: int) -> str:
    # Quote/backslash/tab-heavy lines (JSON fixtures, regexes) inflate most when double-encoded.
    return "".join(f'{{"k{n}": "v\\"{n}\\\\", "t": "\\t\\\\path\\\\to"}}\t"end"\n' for n in range(lines))


def test_default_budget_sits_under_the_live_mcp_threshold():
    from boris_intake import review_tools_mcp as module

    assert module._RESULT_BUDGET < _mcp_threshold()


def test_file_pages_render_under_the_mcp_result_threshold(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    text = _worst_case_text(3_000)  # ~180k raw chars, ~2x when rendered
    runner, _calls = _contents_runner(text=text)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    seen, page = "", 1
    while True:
        result = module.read_github_evidence("file_at_head", "src/app.py", page)
        assert _hermes_rendered_size(result) <= _mcp_threshold(), f"page {page} would spill"
        seen += result["data"]["content"]
        if not result["data"]["has_more"]:
            break
        page += 1
    assert seen == text
    assert page > 1


@pytest.mark.parametrize("depth", (1, 4))
def test_diff_pages_render_under_the_mcp_result_threshold(tmp_path, monkeypatch, depth):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    # Many files (a long all_files list rides on every page) with quote-heavy patches;
    # depth 4 gives ~150-char paths, where a count-capped all_files list alone spilled.
    folder = "/".join(["a/very/deeply/nested/directory/segment"] * depth)
    files = [(f"{folder}/{n:04d}_fixture.json", 30) for n in range(320)]
    parts = []
    for name, lines in files:
        body = "".join(f'+{{"row": {n}, "q": "\\"x\\\\"}}\n' for n in range(lines))
        parts.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -0,0 +1,{lines} @@\n{body}")
    diff = "".join(parts)
    runner, _calls = github_runner(evidence=diff)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    seen_files, page = set(), 1
    while True:
        # Walking a diff past what one context can hold is a paginator property, not a review, so
        # the per-review evidence tally is cleared rather than ending the walk partway.
        module._evidence_served.clear()
        result = module.read_github_evidence("pr_diff", None, page)
        assert _hermes_rendered_size(result) <= _mcp_threshold(), f"page {page} would spill"
        data = result["data"]
        seen_files.update(data["files_on_page"])
        if not data["has_more"]:
            break
        page += 1
    assert seen_files == {name for name, _ in files}
    assert data["total_files"] == 320
    assert data["all_files_truncated"] is True
    assert 0 < len(data["all_files"]) <= module._ALL_FILES_LISTED
    assert data["all_files"] == sorted(name for name, _ in files)[: len(data["all_files"])]


def test_metadata_is_bounded_and_review_context_precedes_github_data(tmp_path, monkeypatch):
    module, _store, _request = bind_request(tmp_path, monkeypatch)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        payload = {"headRefOid": "a" * 40, "title": "Example", "comments": [], "reviews": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_metadata")

    # Order is part of the contract: if the payload ever spills, Hermes keeps a head
    # preview, and the workflow-owned context must be in it.
    assert list(result) == ["operation", "head_sha", "review_context", "data"]
    fields = calls[-1][0][-1]
    assert "comments" not in fields and "reviews" not in fields
    assert "body" in fields and "headRefOid" in fields

    module.read_github_evidence("pr_comments")
    assert "comments,reviews" in calls[-1][0][-1]


def test_review_context_failure_does_not_break_pr_metadata(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    def boom(self, request):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(module.Store, "prior_review", boom)

    result = module.read_github_evidence("pr_metadata")

    assert result["data"]["title"] == "Example"
    assert result["review_context"] == {"unavailable": "review context could not be loaded"}
    assert "locked" not in json.dumps(result)


def test_directory_listing_is_bounded(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    listing = [
        {"name": f"f{n:03d}.py", "path": f"src/f{n:03d}.py", "type": "file", "size": n, "sha": "1" * 40}
        for n in range(module._DIR_ENTRIES_LISTED + 50)
    ]

    def run(command, **kwargs):
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": "a" * 40, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    data = module.read_github_evidence("file_at_head", "src")["data"]

    assert len(data["entries"]) == module._DIR_ENTRIES_LISTED
    assert data["truncated"] is True
    assert data["total_entries"] == module._DIR_ENTRIES_LISTED + 50
    assert _hermes_rendered_size(data) <= _mcp_threshold()
    with pytest.raises(module.ToolError, match="single page"):
        module.read_github_evidence("file_at_head", "src", 2)


@pytest.mark.parametrize(
    ("text", "expected_lines"),
    (
        ("a\n\x0cb\nc\n", 3),  # form feed is not a line break to editors or `git blame`
        ("a b\nc", 2),  # no trailing newline: last line still counts
        ("a\r\nb\r\n", 2),  # CRLF stays inside the line
        ("", 0),
    ),
)
def test_file_line_numbers_follow_newlines_only(tmp_path, monkeypatch, text, expected_lines):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(text=text)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("file_at_head", "src/app.py")["data"]

    assert data["total_lines"] == expected_lines
    assert data["content"] == text
    assert data["line_range"] == ([1, expected_lines] if expected_lines else [0, 0])


def test_binary_detection_covers_nul_bytes_in_valid_utf8(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(raw=b"plain text\x00with a NUL\n", path="data.bin")
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("file_at_head", "data.bin")["data"]

    assert data["binary"] is True and data["content"] is None


@pytest.mark.parametrize(
    "contents",
    (
        {"content": "", "encoding": "none", "size": 3_000_000},
        {"content": "", "encoding": "base64", "size": 1_048_577},
    ),
)
def test_each_withheld_content_shape_is_reported(tmp_path, monkeypatch, contents):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(path="big.bin", **contents)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="1 MiB"):
        module.read_github_evidence("file_at_head", "big.bin")


@pytest.mark.parametrize(
    "contents",
    (
        {"type": "symlink", "target": "../elsewhere", "size": 12},
        {"type": "submodule", "submodule_git_url": "https://x", "size": 0},
        {"content": None, "encoding": "base64", "size": 5},
    ),
)
def test_unreadable_content_shapes_fail_plainly(tmp_path, monkeypatch, contents):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(path="odd", **contents)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    with pytest.raises(module.ToolError, match="readable"):
        module.read_github_evidence("file_at_head", "odd")


def test_non_integer_size_is_tolerated(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = _contents_runner(text="x = 1\n", size="unknown")
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("file_at_head", "src/app.py")["data"]

    assert data["content"] == "x = 1\n" and data["size"] == 0


@pytest.mark.parametrize(
    "stderr",
    (
        "gh: Forbidden (HTTP 403)\n{\"message\":\"Resource not accessible\"}",
        "ghx_ro: REFUSED api path is outside the pinned repository head",
    ),
)
def test_only_a_real_404_is_reported_as_a_missing_path(tmp_path, monkeypatch, stderr):
    module, _, _ = bind_request(tmp_path, monkeypatch)

    def run(command, **kwargs):
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": "a" * 40, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    with pytest.raises(module.GitHubCommandError) as caught:
        module.read_github_evidence("file_at_head", "src/app.py")
    assert "does not exist" not in str(caught.value)


def test_missing_path_message_carries_githubs_reason(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)

    def run(command, **kwargs):
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": "a" * 40, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    with pytest.raises(module.ToolError, match=r"deletes.*HTTP 404"):
        module.read_github_evidence("file_at_head", "src/app.py")


def test_mode_only_diff_of_many_deep_files_stays_under_threshold(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    # Tiny per-file content, so files_on_page and all_files dominate the page.
    folder = "/".join(["service/module/package/component"] * 4)
    names = [f"{folder}/{n:04d}.java" for n in range(2_000)]
    diff = "".join(f"diff --git a/{n} b/{n}\nold mode 100644\nnew mode 100755\n" for n in names)
    runner, _calls = github_runner(evidence=diff)
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    seen, page = set(), 1
    while True:
        # Walking a diff past what one context can hold is a paginator property, not a review, so
        # the per-review evidence tally is cleared rather than ending the walk partway.
        module._evidence_served.clear()
        result = module.read_github_evidence("pr_diff", None, page)
        assert _hermes_rendered_size(result) <= _mcp_threshold(), f"page {page} would spill"
        seen.update(result["data"]["files_on_page"])
        if not result["data"]["has_more"]:
            break
        page += 1
    assert seen == set(names)


def test_deep_directory_listing_is_bounded_by_size(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    folder = "/".join(["a/very/deeply/nested/directory/segment"] * 3)
    listing = [
        {"name": f"{'component' * 3}_{n:03d}.tsx", "path": f"{folder}/x{n}", "type": "file", "size": n}
        for n in range(module._DIR_ENTRIES_LISTED)
    ]

    def run(command, **kwargs):
        if "view" in command and "headRefOid" in command[-1]:
            payload = {"headRefOid": "a" * 40, "title": "Example", "comments": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)

    result = module.read_github_evidence("file_at_head", folder)

    assert _hermes_rendered_size(result) <= _mcp_threshold()
    data = result["data"]
    assert data["total_entries"] == module._DIR_ENTRIES_LISTED
    assert 0 < len(data["entries"]) <= module._DIR_ENTRIES_LISTED
    assert all("path" not in entry for entry in data["entries"])
    assert data["truncated"] == (len(data["entries"]) < module._DIR_ENTRIES_LISTED)


def test_long_pull_request_body_is_fitted_not_spilled(tmp_path, monkeypatch):
    module, store, request = bind_request(tmp_path, monkeypatch)
    _finish_prior_review(
        store, request, head="b" * 40, outcome="ISSUES_FOUND",
        blocking=tuple(f"Prior blocker {n} " + "x" * 120 for n in range(8)),
        non_blocking=tuple(f"Prior nit {n} " + "y" * 120 for n in range(8)),
        at="2026-09-05T10:00:00Z",
    )
    body = "\n".join(f'## Release {n}\n- fixed "quoted" thing \\ path' for n in range(3_000))
    assert len(body) > 60_000
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        payload = {"headRefOid": "a" * 40, "title": "Example", "body": body, "state": "OPEN"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_metadata")

    assert _hermes_rendered_size(result) <= _mcp_threshold()
    assert result["review_context"]["prior_review"]["blocking_titles"][7].startswith("Prior blocker 7")
    data = result["data"]
    assert data["body_truncated"] is True
    assert body.startswith(data["body"].split("\n[boris:")[0])
    assert "chars of the description omitted]" in data["body"]
    assert data["title"] == "Example"


def test_short_pull_request_body_is_passed_through(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    runner, _calls = github_runner()
    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("pr_metadata")["data"]

    assert "body_truncated" not in data


def test_busy_comment_thread_keeps_the_newest_entries_under_threshold(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    comments = [{"id": n, "author": {"login": "dev"}, "body": f"comment {n} " + "z" * 800} for n in range(300)]
    reviews = [{"id": n, "state": "COMMENTED", "body": f"review {n} " + "w" * 800} for n in range(40)]

    def runner(command, **kwargs):
        payload = {"headRefOid": "a" * 40, "title": "Example", "comments": comments, "reviews": reviews}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    result = module.read_github_evidence("pr_comments")

    assert _hermes_rendered_size(result) <= _mcp_threshold()
    data = result["data"]
    assert data["total_comments"] == 300 and data["total_reviews"] == 40
    assert data["comments_omitted"] == 300 - len(data["comments"]) > 0
    assert data["comments"][-1]["id"] == 299  # newest kept
    assert [c["id"] for c in data["comments"]] == list(range(300 - len(data["comments"]), 300))
    assert data["reviews"][-1]["id"] == 39
    assert "oldest" in data["note"]


def test_small_comment_thread_is_passed_through_whole(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)

    def runner(command, **kwargs):
        payload = {"headRefOid": "a" * 40, "title": "Example", "comments": [{"id": 1, "body": "hi"}], "reviews": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", runner)

    data = module.read_github_evidence("pr_comments")["data"]

    assert data["comments"] == [{"id": 1, "body": "hi"}]
    assert data["comments_omitted"] == 0 and data["reviews_omitted"] == 0
    assert "note" not in data


def _gh_comment(n, body, reactions=3):
    """A comment shaped like `gh pr view --json comments` really returns it."""
    return {
        "id": f"IC_kwDO{n:08d}",
        "author": {"id": "MDQ6VXNlcjM1NTY1OTYx", "is_bot": False, "login": f"dev{n % 7}", "name": "Dev Eloper"},
        "authorAssociation": "MEMBER",
        "body": body,
        "createdAt": "2026-09-06T15:20:08Z",
        "includesCreatedEdit": False,
        "isMinimized": False,
        "minimizedReason": "",
        "reactionGroups": [{"content": c, "users": {"totalCount": 1}} for c in ("THUMBS_UP", "EYES", "HEART")[:reactions]],
        "url": f"https://github.com/Ylopo/example/pull/42#issuecomment-{5560190070 + n}",
        "viewerDidAuthor": False,
    }


def _thread_runner(comments, reviews):
    def runner(command, **kwargs):
        payload = {"headRefOid": "a" * 40, "title": "Example", "comments": comments, "reviews": reviews}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    return runner


def test_nested_list_size_estimate_matches_the_real_render():
    from boris_intake import review_tools_mcp as module

    items = [_gh_comment(n, "x" * n) for n in range(12)]
    empty = {"operation": "pr_comments", "head_sha": "a" * 40, "data": {"comments": [], "reviews": []}}
    full = {"operation": "pr_comments", "head_sha": "a" * 40, "data": {"comments": items, "reviews": []}}

    true_delta = _hermes_rendered_size(full) - _hermes_rendered_size(empty)
    estimate = module._rendered_items_len(items, 2) - module._rendered_items_len([], 2)

    assert estimate == true_delta


def test_many_short_gh_shaped_comments_stay_under_threshold(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    comments = [_gh_comment(n, "k", reactions=3) for n in range(600)]
    reviews = [{"id": f"PRR_{n}", "author": {"login": "dev"}, "state": "COMMENTED", "body": "r" * 40,
                "submittedAt": "2026-09-06T15:22:33Z", "commit": {"oid": "a" * 40}} for n in range(40)]
    monkeypatch.setattr(module, "GITHUB_RUNNER", _thread_runner(comments, reviews))

    result = module.read_github_evidence("pr_comments")

    assert _hermes_rendered_size(result) <= _mcp_threshold()
    data = result["data"]
    assert data["comments"][-1]["id"] == comments[-1]["id"]
    assert data["total_comments"] == 600


def test_one_giant_newest_comment_does_not_blank_the_thread(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    comments = [_gh_comment(n, f"human comment {n}") for n in range(12)]
    comments.append(_gh_comment(12, "| file | coverage |\n" + "| a/b/c.py | 100% |\n" * 3_000))
    reviews = [{"id": "PRR_1", "state": "APPROVED", "body": "lgtm"}, {"id": "PRR_2", "state": "COMMENTED", "body": "w" * 60_000}]
    monkeypatch.setattr(module, "GITHUB_RUNNER", _thread_runner(comments, reviews))

    result = module.read_github_evidence("pr_comments")

    assert _hermes_rendered_size(result) <= _mcp_threshold()
    data = result["data"]
    newest = data["comments"][-1]
    assert newest["id"] == comments[-1]["id"]
    assert newest["body_truncated"] is True
    assert newest["body"].startswith("| file | coverage |\n")
    assert "chars of this comment omitted]" in newest["body"]
    # The short human comments fit alongside the capped bot comment (each gh-shaped
    # comment renders to ~1.2k chars; the bot comment is capped at a quarter of the room).
    assert [c["id"] for c in data["comments"]] == [c["id"] for c in comments]
    assert data["comments_omitted"] == 0
    assert data["reviews"][-1]["id"] == "PRR_2" and data["reviews"][-1]["body_truncated"] is True
    assert data["reviews"][0]["id"] == "PRR_1" and "body_truncated" not in data["reviews"][0]
    assert data["reviews_omitted"] == 0 and "note" not in data


@pytest.mark.parametrize("oversized_single_file", [False, True])
def test_oversized_diff_and_file_batches_preserve_complete_inventory(
    tmp_path, monkeypatch, oversized_single_file
):
    from urllib.parse import parse_qs, urlsplit

    module, _, _ = bind_request(tmp_path, monkeypatch)
    entries = [
        {"filename": f"src/f{i}.py", "status": "modified", "changes": 2,
         "patch": "@@ -1 +1 @@\n-old\n+" + "x" * (2200000 if oversized_single_file and i == 0 else 60000)}
        for i in range(53)
    ]

    def run(command, **kwargs):
        if "view" in command:
            output = json.dumps({"headRefOid": "a" * 40})
        elif "diff" in command:
            output = "x" * 2200000
        else:
            route = command[2]
            query = parse_qs(urlsplit(route).query)
            count, page = int(query["per_page"][0]), int(query["page"][0])
            batch = entries[(page - 1) * count:page * count]
            if "--jq" in command:
                batch = [{k: v for k, v in entry.items() if k != "patch"} for entry in batch]
            output = json.dumps(batch)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)
    result = module.read_github_evidence("pr_diff")
    assert result["data"]["all_files"] == sorted(entry["filename"] for entry in entries)
    assert result["data"]["source"] == "changed_files_api"
    assert _hermes_rendered_size(result) <= _mcp_threshold()
    if oversized_single_file:
        assert "Patch unavailable" in result["data"]["diff"]
        assert "file_at_head" in result["data"]["diff"]

def test_gitattributes_generated_files_keep_their_place_but_not_their_patch(tmp_path, monkeypatch):
    """A repo declaring a path machine-written gets its patch withheld, not the file hidden.

    forge-content-service and million-stars both mark their emitted OpenAPI spec and generated
    client this way and pin them with a CI drift guard, but the pulls/files API returns the patch
    regardless, so a one-line route change buries the reviewable code under tens of thousands of
    emitted lines.
    """
    import base64

    module, _, _ = bind_request(tmp_path, monkeypatch)
    attrs = base64.b64encode(
        b"# emitted, pinned by the drift guard\n"
        b"openapi.json -diff linguist-generated=true\n"
        b"src/contract/forge-api.ts -diff linguist-generated=true\n"
        b"README.md text\n"
    ).decode()
    listing = [
        {"filename": "openapi.json", "status": "modified", "changes": 50318,
         "patch": "@@ -1 +1 @@\n-emitted\n+emitted harder\n"},
        {"filename": "src/services/normalized-save.ts", "status": "modified", "changes": 12,
         "patch": "@@ -1 +1 @@\n-real\n+really real\n"},
    ]

    def run(command, **kwargs):
        tail = " ".join(command)
        if "view" in command and "headRefOid" in tail:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40, "title": "Example", "comments": []}),
                stderr="")
        if ".gitattributes" in tail:
            return SimpleNamespace(returncode=0, stdout=attrs, stderr="")
        if "/files?" in tail:
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
        # Force the changed-files route, which is the one a diff this size takes.
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 406")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)
    data = module.read_github_evidence("pr_diff", None, 1)["data"]
    diff = data["diff"]

    assert "emitted harder" not in diff, "declared-generated patch text reached the reviewer"
    assert "declared machine-written in .gitattributes" in diff
    assert "50318 changed lines" in diff
    assert "really real" in diff, "an undeclared file must still be reviewable"
    assert "openapi.json" in data["files_on_page"], "suppression must not hide the file"
    assert "openapi.json" in data["all_files"]


def test_gitattributes_absent_leaves_every_patch_intact(tmp_path, monkeypatch):
    module, _, _ = bind_request(tmp_path, monkeypatch)
    listing = [{"filename": "openapi.json", "status": "modified", "changes": 9,
                "patch": "@@ -1 +1 @@\n-a\n+b\n"}]

    def run(command, **kwargs):
        tail = " ".join(command)
        if "view" in command and "headRefOid" in tail:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40, "title": "Example", "comments": []}),
                stderr="")
        if ".gitattributes" in tail:
            return SimpleNamespace(returncode=1, stdout="", stderr="Not Found")
        if "/files?" in tail:
            return SimpleNamespace(returncode=0, stdout=json.dumps(listing), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 406")

    monkeypatch.setattr(module, "GITHUB_RUNNER", run)
    diff = module.read_github_evidence("pr_diff", None, 1)["data"]["diff"]
    assert "+b" in diff
    assert "declared machine-written" not in diff
