import json
from types import SimpleNamespace

from pr_remediation.config import RemediationConfig
from pr_remediation.github_io import GitHubIO


def runner_for(payloads):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        for needle, payload in payloads.items():
            if needle in command:
                return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run, calls


def io_with(runner, tmp_path):
    token = tmp_path / "tok"
    token.write_text("secret")
    return GitHubIO(RemediationConfig(), token, runner=runner)


def test_hold_label_is_detected(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [{"name": "hermes-hold"}], "headRefOid": "a" * 40,
         "author": {"login": "Chadtao206"}})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_absent_hold_label_is_detected(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [{"name": "bug"}], "headRefOid": "a" * 40,
         "author": {"login": "Chadtao206"}})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is False


def test_claim_applies_the_ownership_label(tmp_path):
    runner, calls = runner_for({})
    io_with(runner, tmp_path).claim("Ylopo", "example", 42)
    applied = [c for c in calls if "--add-label" in c]
    assert applied and "hermes-owner-andrej" in applied[0]


def test_release_removes_the_ownership_label(tmp_path):
    runner, calls = runner_for({})
    io_with(runner, tmp_path).release("Ylopo", "example", 42)
    removed = [c for c in calls if "--remove-label" in c]
    assert removed and "hermes-owner-andrej" in removed[0]


def test_head_sha_and_author_are_read_from_the_live_pr(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40, "author": {"login": "Chadtao206"}})})
    io = io_with(runner, tmp_path)
    assert io.head_sha("Ylopo", "example", 42) == "b" * 40
    assert io.author("Ylopo", "example", 42) == "Chadtao206"


def test_a_failed_gh_call_reads_as_unknown_not_as_false(tmp_path):
    """Never let a transient failure look like 'no hold label'."""
    def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    io = io_with(run, tmp_path)
    assert io.head_sha("Ylopo", "example", 42) is None
    assert io.has_hold("Ylopo", "example", 42) is True, "fail closed: assume hold"
