from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from boris_intake import github
from boris_intake.models import WorkflowState
from boris_intake.result import canonicalize_judgment, validate_judgment
from tests.cron.boris_intake.test_github import lifecycle_request
from tests.cron.boris_intake.test_reconciler import (
    Clock,
    GitHubFake,
    KanbanFake,
    SlackFake,
    admit_request,
    required_check_snapshot,
    valid_payload,
)

NIT = {
    "title": "Rename local variable",
    "location": "src/update.py:12",
    "suggestion": "Use owner_id.",
}


def _published_approvals(tmp_path, payload, snapshot=None):
    from boris_intake.reconciler import ReconcilerDeps, tick

    store, admitted = admit_request(tmp_path, snapshot=snapshot)
    clock = Clock()
    fake = GitHubFake(admitted.gate_snapshot)
    deps = ReconcilerDeps(slack=SlackFake(), github=fake, kanban=KanbanFake(), now=clock)
    for _ in range(12):
        tick(store, deps)
        request = store.get_request(admitted.request_id)
        if (
            request.workflow_state is WorkflowState.REVIEWING
            and store.judgment_for_request(request.request_id) is None
        ):
            store.submit_judgment(
                request.request_id,
                canonicalize_judgment(validate_judgment(payload)),
                clock(),
            )
        if request.workflow_state is WorkflowState.COMPLETE:
            break
        clock.advance()
    assert fake.publications == 1
    return fake.approvals


@pytest.mark.parametrize("payload", (valid_payload(), valid_payload(nits=[NIT])))
def test_a_ready_verdict_with_or_without_comments_is_published_as_an_approval(tmp_path, payload):
    assert _published_approvals(tmp_path, payload) == [True]


def test_a_verdict_blocked_by_a_required_check_is_not_published_as_an_approval(tmp_path):
    blocked = required_check_snapshot("failure")
    assert _published_approvals(tmp_path, valid_payload(), blocked) == [False]


RELEASE_CONDITION = {
    "title": "Install the database role before deployment",
    "phase": "deployment",
    "requirement": "Apply roles/auto_dba.sql as an admin.",
    "authoritative_source": "internalMonitoring/autoDba/README.md",
    "completion_criterion": "column_stats() returns rows as auto_dba.",
}


def test_a_ready_verdict_that_names_release_conditions_is_not_published_as_an_approval(tmp_path):
    payload = valid_payload(release_conditions=[RELEASE_CONDITION])
    assert _published_approvals(tmp_path, payload) == [False]


def _pull(**changes):
    pull = {
        "title": "fix(search): keep the cursor stable",
        "user": {"login": "octocat"},
        "head": {"ref": "fix/PLAT-1-cursor"},
        "base": {"ref": "main", "repo": {"default_branch": "main"}},
        "changed_files": 1,
    }
    pull.update(changes)
    return pull


def _publish(tmp_path, pull, files=("src/search.ts",), viewer="boris", files_fail=False):
    request = lifecycle_request(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    posted = []

    def run(command, **kwargs):
        target = command[-1]
        if "input" in kwargs:
            posted.append(json.loads(kwargs["input"]))
            answer = {"id": 7, "html_url": "https://github/reviews/7", "body": "body"}
        elif target == "user":
            answer = {"login": viewer}
        elif target.endswith("/files?per_page=100"):
            if files_fail:
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")
            answer = [[{"filename": name} for name in files]]
        else:
            answer = pull
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr="")

    github.GitHubIO(token, runner=run).publish_review(request, "body", approve=True)
    (review,) = posted
    return request, review


def test_an_ordinary_change_is_approved_at_the_head_that_was_reviewed(tmp_path):
    request, review = _publish(tmp_path, _pull())

    assert review == {"event": "APPROVE", "body": "body", "commit_id": request.head_sha}


@pytest.mark.parametrize(
    ("pull", "files"),
    (
        pytest.param(
            _pull(head={"ref": "releases/8.99.0"},
                  base={"ref": "main", "repo": {"default_branch": "development"}}),
            ("src/search.ts",), id="release-branch"),
        pytest.param(_pull(title="chore(release): v8.99.0"), ("src/search.ts",), id="release-title"),
        pytest.param(
            _pull(base={"ref": "main", "repo": {"default_branch": "development"}}),
            ("src/search.ts",), id="hotfix-straight-to-production"),
        pytest.param(_pull(), ("drizzle/20261030_checks.sql",), id="migration"),
        pytest.param(_pull(), ("migration_scripts/2026/trip.js",), id="migration-directory"),
        pytest.param(_pull(), ("tf/ec2/dba-runner/iam.tf",), id="terraform"),
        pytest.param(_pull(), (".github/workflows/build-push.yml",), id="workflow"),
        pytest.param(_pull(), ("services/api/Dockerfile",), id="dockerfile"),
        pytest.param(_pull(), ("apps/web/.env.production",), id="env-file"),
        pytest.param(_pull(changed_files=2), ("src/search.ts",), id="file-list-incomplete"),
        pytest.param(_pull(user={"login": "Boris"}), ("src/search.ts",), id="own-pull-request"),
        pytest.param(_pull(base={"ref": "main"}), ("src/search.ts",), id="default-branch-unknown"),
    ),
)
def test_a_release_or_a_sensitive_change_keeps_a_comment_review(tmp_path, pull, files):
    _, review = _publish(tmp_path, pull, files)

    assert review == {"event": "COMMENT", "body": "body"}


def test_an_example_env_file_does_not_withhold_the_approval(tmp_path):
    _, review = _publish(tmp_path, _pull(), (".env.example",))

    assert review["event"] == "APPROVE"


def test_a_product_page_named_migrations_does_not_withhold_the_approval(tmp_path):
    files = ("src/app/internal/migrations/_components/wave-selection.tsx",)
    _, review = _publish(tmp_path, _pull(), files)

    assert review["event"] == "APPROVE"


def test_a_stacked_pull_request_into_a_feature_branch_is_approved(tmp_path):
    pull = _pull(base={"ref": "feat/PLAT-2-core", "repo": {"default_branch": "main"}})
    _, review = _publish(tmp_path, pull)

    assert review["event"] == "APPROVE"


def test_unreadable_pull_request_facts_still_publish_the_review_as_a_comment(tmp_path):
    _, review = _publish(tmp_path, _pull(), files_fail=True)

    assert review == {"event": "COMMENT", "body": "body"}
