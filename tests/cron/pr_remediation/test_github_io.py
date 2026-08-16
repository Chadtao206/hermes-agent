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


def test_non_utf8_token_file_fails_closed_instead_of_raising(tmp_path):
    """A corrupt token file is an unreadable-token failure, same as a missing
    file or an OSError. has_hold()'s entire safety contract is that GitHub
    being unreadable reads as 'assume hold' -- if a UnicodeDecodeError were
    allowed to escape _run() instead of being folded into that contract, it
    would raise all the way out of has_hold() into the daemon's polling
    loop, which is worse than reporting a false hold: it can crash automation
    entirely while leaving the human's kill switch unenforced. Unreadable
    must always mean held, never an exception.
    """
    def run(command, **kwargs):
        raise AssertionError("gh must never be invoked when the token can't be read")

    token = tmp_path / "tok"
    token.write_bytes(b"\xff\xfe\x00\x80not-utf8")
    io = GitHubIO(RemediationConfig(), token, runner=run)

    assert io.has_hold("Ylopo", "example", 42) is True, "fail closed: assume hold"


def test_empty_token_file_fails_closed_without_calling_gh(tmp_path):
    """An empty or whitespace-only token file is a locally-detectable
    unusable-token case: handing gh an empty GH_TOKEN would still fail
    closed today (gh exits non-zero), but that makes the safety property
    depend on an external tool's behavior and burns a network call on every
    check. Unreadable/unusable must fail closed on its own, without relying
    on gh to say no -- so the runner must never be reached.
    """
    for contents in ("", "   \n\t  "):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        token = tmp_path / "tok"
        token.write_text(contents)
        io = GitHubIO(RemediationConfig(), token, runner=run)
        assert io.has_hold("Ylopo", "example", 42) is True, "fail closed: assume hold"
        assert calls == [], "runner must not be invoked for an unusable token"
