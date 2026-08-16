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


# --- I2: the view must actually ask GitHub for the PR's state --------------


def test_the_view_asks_for_state_so_a_merged_pr_can_be_recognised(tmp_path):
    """The field list is the contract with `gh`, and it is the thing that was
    wrong: `_view` requested labels,headRefOid,author and deliberately not
    `state`, so nothing downstream could tell an open pull request from one
    merged between the verdict and the tick. Pinned on the argv, because every
    driver-level test wires a fake and would never notice the field going
    missing again."""
    runner, calls = runner_for({"view": json.dumps({"labels": []})})
    io_with(runner, tmp_path).state("Ylopo", "example", 42)
    views = [c for c in calls if "view" in c]
    assert views, "no gh pr view was issued"
    fields = views[0][views[0].index("--json") + 1].split(",")
    assert "state" in fields, f"gh was asked for {fields}, which cannot answer "\
        "whether the pull request is still open"


def test_state_is_read_from_the_live_pr(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "headRefOid": "b" * 40,
         "author": {"login": "Chadtao206"}, "state": "MERGED"})})
    assert io_with(runner, tmp_path).state("Ylopo", "example", 42) == "MERGED"


def test_state_is_upper_cased_so_one_comparison_serves(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": [], "state": "open"})})
    assert io_with(runner, tmp_path).state("Ylopo", "example", 42) == "OPEN"


# --- unresolved_bot_threads(): the Codex/CodeQL review-thread check --------
#
# The blind spot this method closes: the first live run declared a PR ready
# while three Codex threads sat open on the file it had just remediated.
# Thread resolution has no `gh pr view --json` field, so this goes through
# `gh api graphql` instead -- the same `_run`, the same fail-closed
# discipline as `has_hold`, but a different safe default: there is no `True`
# to substitute for "we don't know", so an unreadable or malformed response
# returns None, and the caller (SlackIO.notify_ready) is the one place that
# turns None into its own sentence rather than a silent clean bill of
# health.


def _thread(resolved, outdated, login, path):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": [{"path": path, "author": {"login": login}}]},
    }


def _gql(nodes):
    return json.dumps({
        "data": {"repository": {"pullRequest": {
            "reviewThreads": {"nodes": nodes}}}}})


def test_the_call_goes_through_gh_api_graphql(tmp_path):
    runner, calls = runner_for({"graphql": _gql([])})
    io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42)
    graphql_calls = [c for c in calls if "graphql" in c]
    assert graphql_calls, "gh api graphql was never invoked"
    assert "api" in graphql_calls[0]


def test_an_unresolved_codex_thread_is_returned_with_its_path(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        _thread(False, False, "chatgpt-codex-connector", "scripts/lib/tag-vendors.ts"),
    ])})
    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 103)
    assert threads == [{"path": "scripts/lib/tag-vendors.ts", "outdated": False}]


def test_an_unresolved_codeql_thread_is_also_recognised(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        _thread(False, False, "github-advanced-security", "scripts/lib/tag-vendors.ts"),
    ])})
    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 103)
    assert len(threads) == 1


def test_multiple_unresolved_bot_threads_are_all_returned(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        _thread(False, False, "chatgpt-codex-connector", "scripts/lib/tag-vendors.ts"),
        _thread(False, False, "chatgpt-codex-connector", "scripts/lib/tag-vendors.ts"),
        _thread(False, False, "chatgpt-codex-connector", "scripts/lib/tag-vendors.ts"),
    ])})
    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 103)
    assert len(threads) == 3


def test_a_pr_with_no_unresolved_bot_threads_returns_an_empty_list(tmp_path):
    runner, _ = runner_for({"graphql": _gql([])})
    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42)
    assert threads == []


def test_a_resolved_bot_thread_is_not_counted(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        _thread(True, False, "chatgpt-codex-connector", "a.ts"),
    ])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) == []


def test_a_resolved_threads_malformed_comments_do_not_poison_the_whole_read(tmp_path):
    """Once a thread is known-resolved, this method never needs its comments
    to answer the question this call exists for -- so a resolved thread with
    garbage comments must not turn an otherwise-clean read into None."""
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": True, "isOutdated": False, "comments": "not even a dict"},
    ])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) == []


def test_a_thread_from_a_non_bot_author_is_not_counted(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        _thread(False, False, "some-human-reviewer", "a.ts"),
    ])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) == []


def test_bot_and_human_threads_together_only_the_bot_one_is_counted(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        _thread(False, False, "some-human-reviewer", "human.ts"),
        _thread(False, False, "chatgpt-codex-connector", "bot.ts"),
    ])})
    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42)
    assert threads == [{"path": "bot.ts", "outdated": False}]


def test_an_outdated_unresolved_thread_is_still_returned_with_the_flag_set(tmp_path):
    """isOutdated does not mean resolved -- it must still be reported, only
    distinguishable from a current thread."""
    runner, _ = runner_for({"graphql": _gql([
        _thread(False, True, "chatgpt-codex-connector", "a.ts"),
    ])})
    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42)
    assert threads == [{"path": "a.ts", "outdated": True}]


# --- unresolved_bot_threads(): fails closed to None, never to [] -----------
#
# [] means "GitHub was asked and nothing is outstanding". None means "GitHub
# could not be trusted to answer at all". A test here that got these two
# backwards would be exactly the false all-clear this whole change exists to
# prevent, so every failure and shape mismatch below is pinned to None, not
# to the empty list a lazy fallback would produce.


def test_a_failed_gh_call_returns_none_not_an_empty_list(tmp_path):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    io = io_with(run, tmp_path)
    assert io.unresolved_bot_threads("Ylopo", "example", 42) is None


def test_empty_stdout_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": ""})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_unparseable_json_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": "{not json"})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_a_json_scalar_payload_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": json.dumps(42)})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_missing_data_key_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": json.dumps({"errors": ["boom"]})})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_a_null_repository_returns_none_not_a_raise(tmp_path):
    """gh api graphql can return HTTP 200 with `data.repository: null` for a
    repo the token cannot see or that was renamed -- that must fail closed
    like any other unreadable answer, not raise past this method."""
    runner, _ = runner_for({"graphql": json.dumps({"data": {"repository": None}})})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_review_threads_nodes_not_a_list_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": json.dumps({
        "data": {"repository": {"pullRequest": {
            "reviewThreads": {"nodes": "not-a-list"}}}}})})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_a_node_that_is_not_a_dict_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql(["not-a-dict"])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_isresolved_missing_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isOutdated": False, "comments": {"nodes": []}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_isresolved_wrong_type_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": "no", "isOutdated": False, "comments": {"nodes": []}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_isoutdated_wrong_type_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": False, "isOutdated": "no", "comments": {"nodes": []}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_comments_not_a_dict_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": False, "isOutdated": False, "comments": ["nope"]}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_empty_comments_returns_none(tmp_path):
    """A thread with zero comments cannot answer who opened it -- unknown,
    not silently skipped."""
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": False, "isOutdated": False, "comments": {"nodes": []}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_author_not_a_dict_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": False, "isOutdated": False,
         "comments": {"nodes": [{"path": "a.ts", "author": "bot"}]}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_login_not_a_string_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": False, "isOutdated": False,
         "comments": {"nodes": [{"path": "a.ts", "author": {"login": 7}}]}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_missing_path_returns_none(tmp_path):
    runner, _ = runner_for({"graphql": _gql([
        {"isResolved": False, "isOutdated": False,
         "comments": {"nodes": [
             {"author": {"login": "chatgpt-codex-connector"}}]}}])})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


def test_a_missing_or_unreadable_state_reads_as_unknown_not_as_closed(tmp_path):
    """Unknown must never be read as "not open": that answer closes the loop,
    and terminal states are absorbing."""
    missing, _ = runner_for({"view": json.dumps({"labels": []})})
    assert io_with(missing, tmp_path).state("Ylopo", "example", 42) is None

    unreadable, _ = runner_for({})  # empty stdout
    assert io_with(unreadable, tmp_path).state("Ylopo", "example", 42) is None

    scalar, _ = runner_for({"view": json.dumps({"labels": [], "state": 7})})
    assert io_with(scalar, tmp_path).state("Ylopo", "example", 42) is None
