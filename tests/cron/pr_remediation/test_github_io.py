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


def test_empty_stdout_with_success_reads_as_unknown_not_as_false(tmp_path):
    """returncode 0 with empty stdout is not a valid empty label set --
    it's nothing to parse, and must fail open."""
    runner, calls = runner_for({"view": ""})
    io = io_with(runner, tmp_path)
    assert io.has_hold("Ylopo", "example", 42) is True
    assert any("view" in c for c in calls), "the runner must actually be invoked"


def test_missing_labels_key_reads_as_unknown_not_as_false(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"headRefOid": "a" * 40})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_null_labels_reads_as_unknown_not_as_false(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": None})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_labels_as_bare_string_reads_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": "hermes-hold"})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_labels_as_dict_reads_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": {"a": 1}})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_labels_as_int_reads_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": 5})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_labels_list_of_strings_reads_as_unknown_not_as_raise(tmp_path):
    """Entries in `labels` are expected to be dicts with a `name` field;
    bare strings are an unrecognised shape, not something to coerce."""
    runner, _ = runner_for({"view": json.dumps({"labels": ["hermes-hold"]})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_label_entry_missing_name_reads_as_unknown_not_as_false(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [{"color": "red"}]})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is True


def test_empty_labels_list_is_a_known_state_and_reads_as_false(tmp_path):
    """The one shape that must NOT fail open: [] is a genuinely known,
    label-free PR, not an unknown one."""
    runner, _ = runner_for({"view": json.dumps({"labels": []})})
    assert io_with(runner, tmp_path).has_hold("Ylopo", "example", 42) is False


def test_claim_returns_true_when_the_write_succeeds(tmp_path):
    runner, calls = runner_for({})
    assert io_with(runner, tmp_path).claim("Ylopo", "example", 42) is True
    assert any("--add-label" in c for c in calls)


def test_claim_returns_false_when_the_write_fails(tmp_path):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    io = io_with(run, tmp_path)
    assert io.claim("Ylopo", "example", 42) is False


def test_release_returns_true_when_the_write_succeeds(tmp_path):
    runner, calls = runner_for({})
    assert io_with(runner, tmp_path).release("Ylopo", "example", 42) is True
    assert any("--remove-label" in c for c in calls)


def test_release_returns_false_when_the_write_fails(tmp_path):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    io = io_with(run, tmp_path)
    assert io.release("Ylopo", "example", 42) is False


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


# --- I6: author() is shape-checked, exactly as has_hold is ------------------
#
# Task 5 declared this class of defect Critical for has_hold and hardened only
# that method, leaving author() on the chained-.get form. The consequence is
# worse here than a wrong answer: the raise lands in tick()'s per-verdict
# handler AFTER the verdict is marked seen, so the verdict is spent and the
# loop is stranded in `observed`, where the due pass has nothing to do for it.


def test_author_as_a_bare_string_reads_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40, "author": "Chadtao206"})})
    assert io_with(runner, tmp_path).author("Ylopo", "example", 42) is None


def test_author_as_a_list_reads_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40, "author": [{"login": "x"}]})})
    assert io_with(runner, tmp_path).author("Ylopo", "example", 42) is None


def test_author_as_an_int_reads_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40, "author": 7})})
    assert io_with(runner, tmp_path).author("Ylopo", "example", 42) is None


def test_a_non_string_login_reads_as_unknown_not_as_a_login(tmp_path):
    """A login that is not a string can never equal cfg.chad_github_login, but
    returning it would hand the driver a value it compares and logs as one."""
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40, "author": {"login": 42}})})
    assert io_with(runner, tmp_path).author("Ylopo", "example", 42) is None


def test_missing_author_key_reads_as_unknown(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40})})
    assert io_with(runner, tmp_path).author("Ylopo", "example", 42) is None


def test_an_unreadable_pr_reads_as_an_unknown_author(tmp_path):
    runner, _ = runner_for({})  # empty stdout: nothing to parse
    assert io_with(runner, tmp_path).author("Ylopo", "example", 42) is None
