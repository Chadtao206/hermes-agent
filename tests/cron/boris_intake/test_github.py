import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake import github
from boris_intake.github import fetch_pr
from boris_intake.models import (
    GateSnapshot,
    NormalizedEvent,
    ReviewCommand,
    ReviewPolicy,
)
from boris_intake.store import Store


def fake_runner(payload, returncode=0):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")
    return run


def test_parses_open_pr():
    info = fetch_pr("Ylopo", "repo", 1, runner=fake_runner(
        {"headRefOid": "a" * 40, "author": {"login": "zjenkins"}, "state": "OPEN"}))
    assert info.head_sha == "a" * 40
    assert info.author == "zjenkins"
    assert info.state == "OPEN"


def test_returns_none_on_nonzero_exit():
    assert fetch_pr("Ylopo", "repo", 1, runner=fake_runner({}, returncode=1)) is None


def test_returns_none_on_garbage_output():
    def run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")
    assert fetch_pr("Ylopo", "repo", 1, runner=run) is None


def test_returns_none_on_short_sha():
    assert fetch_pr("Ylopo", "repo", 1, runner=fake_runner(
        {"headRefOid": "abc", "author": {"login": "x"}, "state": "OPEN"})) is None


def test_fetch_gate_snapshot_pins_head_and_marks_required_checks():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "view" in cmd:
            payload = {"headRefOid": "a" * 40}
        elif "--required" in cmd:
            payload = [{"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"}]
        else:
            payload = [
                {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"},
                {"name": "optional-lint", "state": "SUCCESS", "link": "https://ci/lint"},
            ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    snapshot = github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    )

    assert snapshot is not None
    required = {check.check_name: check.required for check in snapshot.checks}
    assert required == {"optional-lint": False, "unit-tests": True}
    assert len(calls) == 3


def test_fetch_gate_snapshot_fails_closed_when_head_changed():
    snapshot = github.fetch_gate_snapshot(
        "Ylopo",
        "repo",
        1,
        "a" * 40,
        "2026-08-14T12:00:00Z",
        runner=fake_runner({"headRefOid": "b" * 40}),
    )

    assert snapshot is None


def test_fetch_gate_snapshot_accepts_check_failure_exit_with_valid_json():
    def run(cmd, **kwargs):
        if "view" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40}),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="[]", stderr="checks failed")

    assert github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    ) is not None


def test_fetch_gate_snapshot_fails_closed_when_required_check_is_missing_from_rollup():
    def run(cmd, **kwargs):
        if "view" in cmd:
            payload = {"headRefOid": "a" * 40}
        elif "--required" in cmd:
            payload = [{"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"}]
        else:
            payload = []
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    assert github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    ) is None


def lifecycle_request(tmp_path):
    store = Store(tmp_path / "review.db")
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
        policy=ReviewPolicy(False),
    )
    event = NormalizedEvent(
        "C1", "channel", "U1", "1.0", None, "discarded", False, None
    )
    snapshot = GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())
    return store.admit_event(event, (command,), (snapshot,), 1_000_000.0).admitted[0]


def test_lifecycle_review_lookup_matches_only_the_hidden_request_marker(tmp_path):
    request = lifecycle_request(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        payload = [
            [
                {
                    "id": 1,
                    "html_url": "https://github/reviews/1",
                    "body": "ordinary comment",
                },
                {
                    "id": 2,
                    "html_url": "https://github/reviews/2",
                    "body": (
                        "ready\n<!-- boris-review request_id="
                        f"{request.request_id} result_hash=sha256:{'a' * 64} -->"
                    ),
                },
            ]
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    found = github.GitHubIO(token, runner=run).find_review(request)

    assert found.review_id == "2"
    assert found.url == "https://github/reviews/2"
    assert found.body.startswith("ready")
    assert calls[0][0] == [
        "/opt/homebrew/bin/gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/Ylopo/example/pulls/42/reviews?per_page=100",
    ]
    assert calls[0][1]["env"]["GH_TOKEN"] == "secret"


def test_lifecycle_publication_posts_comment_review_with_body_on_json_input(tmp_path):
    request = lifecycle_request(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        payload = {
            "id": 7,
            "html_url": "https://github/reviews/7",
            "body": "rendered body",
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    published = github.GitHubIO(token, runner=run).publish_review(
        request, "rendered body"
    )

    assert published.review_id == "7"
    assert seen["command"] == [
        "/opt/homebrew/bin/gh",
        "api",
        "--method",
        "POST",
        "repos/Ylopo/example/pulls/42/reviews",
        "--input",
        "-",
    ]
    assert json.loads(seen["kwargs"]["input"]) == {
        "event": "COMMENT",
        "body": "rendered body",
    }
    assert "rendered body" not in seen["command"]
