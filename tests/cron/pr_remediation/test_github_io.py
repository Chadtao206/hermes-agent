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


# --- branches(): base/head branch names for the remediation brief ----------
#
# GitHubIO.branches() feeds pr_remediation/lanes.py's build_brief, which
# needs both names at once: the head branch removes a real ambiguity ("push",
# without saying where, against a worktree that lives on a kanban-generated
# branch that is not the PR's), and the base branch is context for whoever
# reviews next. Both come off ONE `_view()` call, so the pair costs one
# `gh pr view` rather than two. That is a claim about this method only:
# `_view()` is not memoized, so each accessor on this class -- `has_hold`,
# `state`, `author`, `branches` -- runs its own `gh pr view` subprocess. See
# `_view`'s own comment for why that is deliberate.


def test_the_view_asks_for_base_and_head_ref_names(tmp_path):
    """Pinned on the argv for the same reason the `state` field is pinned
    above: every driver-level test wires a fake and would never notice a
    field silently dropped back out of `_view`'s request. Dropping one would
    turn the pair's single `gh pr view` into a `branches()` that can never
    answer at all."""
    runner, calls = runner_for({"view": json.dumps({"labels": []})})
    io_with(runner, tmp_path).branches("Ylopo", "example", 42)
    views = [c for c in calls if "view" in c]
    assert views, "no gh pr view was issued"
    fields = views[0][views[0].index("--json") + 1].split(",")
    assert "baseRefName" in fields and "headRefName" in fields


def test_branches_reads_both_names_from_the_live_pr(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "baseRefName": "main", "headRefName": "chad/fix-webhook"})})
    assert io_with(runner, tmp_path).branches("Ylopo", "example", 42) == (
        "main", "chad/fix-webhook")


def test_branches_reads_the_pair_in_one_gh_call_not_two(tmp_path):
    """Both names come from one `_view()` invocation, so reading the PAIR is
    one `gh pr view` rather than the two that separate `base()`/`head()`
    accessors would cost.

    This does NOT say the call is free. `_view()` is deliberately not
    memoized, so `branches()` is its own subprocess on top of whatever
    `has_hold`/`state`/`author` already spent -- the old name for this test
    ("costs exactly one gh call") asserted the broader claim while the
    assertion below only ever proved the narrow one.
    """
    runner, calls = runner_for({"view": json.dumps(
        {"labels": [], "baseRefName": "main", "headRefName": "chad/fix"})})
    io_with(runner, tmp_path).branches("Ylopo", "example", 42)
    assert len([c for c in calls if "view" in c]) == 1


def test_an_unreadable_pr_reads_branches_as_unknown_not_as_a_raise(tmp_path):
    runner, _ = runner_for({})  # empty stdout: nothing to parse
    assert io_with(runner, tmp_path).branches("Ylopo", "example", 42) == (None, None)


def test_missing_ref_name_keys_read_as_unknown(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": []})})
    assert io_with(runner, tmp_path).branches("Ylopo", "example", 42) == (None, None)


def test_non_string_ref_names_read_as_unknown_not_as_raise(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "baseRefName": 5, "headRefName": ["chad/fix"]})})
    assert io_with(runner, tmp_path).branches("Ylopo", "example", 42) == (None, None)


def test_empty_string_ref_names_read_as_unknown_not_as_a_real_branch(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "baseRefName": "", "headRefName": ""})})
    assert io_with(runner, tmp_path).branches("Ylopo", "example", 42) == (None, None)


def test_one_readable_ref_name_does_not_hide_the_other_being_unreadable(tmp_path):
    runner, _ = runner_for({"view": json.dumps(
        {"labels": [], "baseRefName": "main", "headRefName": None})})
    assert io_with(runner, tmp_path).branches("Ylopo", "example", 42) == ("main", None)


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


def _gql(nodes, has_next=False, end_cursor=None):
    """A single page's GraphQL response. Defaults to `hasNextPage: False`
    (a complete, one-page read) so every pre-pagination test that calls this
    with just a node list keeps meaning exactly what it always meant."""
    return json.dumps({
        "data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
            }}}}})


def paged_runner_for(pages):
    """Fake runner that hands back `pages` in order, one response per call
    to `gh api graphql`. Deliberately NOT `runner_for`'s single-fixed-answer
    shape: a fake that always returns the same page regardless of the
    cursor passed in cannot catch a pagination bug -- a method that never
    advances the cursor, or that drops a later page's threads, would sail
    through a fake like that. Non-graphql calls (there aren't any exercised
    here) get an empty success response, matching `runner_for`.
    """
    calls = []
    remaining = list(pages)

    def run(command, **kwargs):
        calls.append(command)
        if "graphql" in command:
            payload = remaining.pop(0) if remaining else pages[-1]
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run, calls


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


# --- unresolved_bot_threads(): pagination --------------------------------
#
# The gap this closes: `reviewThreads(first: 100)` alone silently drops
# everything past the hundredth thread. The PR this feature was built for
# reached 97 threads after its first live run -- one thread away from that
# cliff. These tests use `paged_runner_for`, not `runner_for`: a fake that
# always answers the same page cannot tell a real pagination bug (cursor
# never advances, later pages dropped) from correct behaviour.


def test_threads_spanning_multiple_pages_are_all_returned(tmp_path):
    page_one = _gql([
        _thread(False, False, "chatgpt-codex-connector", "a.ts"),
        _thread(False, False, "chatgpt-codex-connector", "b.ts"),
    ], has_next=True, end_cursor="CURSOR_1")
    page_two = _gql([
        _thread(False, False, "chatgpt-codex-connector", "c.ts"),
    ], has_next=False)
    runner, calls = paged_runner_for([page_one, page_two])

    threads = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 103)

    assert threads == [
        {"path": "a.ts", "outdated": False},
        {"path": "b.ts", "outdated": False},
        {"path": "c.ts", "outdated": False},
    ]
    graphql_calls = [c for c in calls if "graphql" in c]
    assert len(graphql_calls) == 2, "the second page must actually be fetched"
    second_call_args = " ".join(graphql_calls[1])
    assert "after=CURSOR_1" in second_call_args, \
        "the cursor from page one's pageInfo must be threaded into page two's request"


def test_the_page_bound_being_hit_returns_none_not_a_clean_or_partial_answer(
        tmp_path, monkeypatch):
    """Hitting `_MAX_THREAD_PAGES` while GitHub still reports more pages is
    truncation, not completeness, and must not be reported as either a clean
    read ([]) or a partial-but-trustworthy one (a non-empty list): both of
    those would repeat the exact false all-clear this whole change exists to
    remove. A low bound is monkeypatched in so this test doesn't need
    dozens of fake pages to prove the point."""
    from pr_remediation import github_io
    monkeypatch.setattr(github_io, "_MAX_THREAD_PAGES", 3)

    pages = [
        _gql([_thread(False, False, "chatgpt-codex-connector", f"{n}.ts")],
             has_next=True, end_cursor=f"CURSOR_{n}")
        for n in range(1, 4)
    ]
    runner, calls = paged_runner_for(pages)

    result = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 103)

    assert result is None
    graphql_calls = [c for c in calls if "graphql" in c]
    assert len(graphql_calls) == 3, \
        "must stop at the page bound rather than spinning forever"


def test_a_failure_mid_pagination_returns_none_not_the_threads_gathered_so_far(
        tmp_path):
    """Page one succeeds and finds a real unresolved bot thread; page two's
    response is garbage. The whole read must fail to None -- handing back
    page one's thread would be exactly the partial-list-dressed-as-whole
    this method exists to prevent."""
    page_one = _gql([
        _thread(False, False, "chatgpt-codex-connector", "a.ts"),
    ], has_next=True, end_cursor="CURSOR_1")
    runner, calls = paged_runner_for([page_one, "{not valid json"])

    result = io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 103)

    assert result is None
    graphql_calls = [c for c in calls if "graphql" in c]
    assert len(graphql_calls) == 2, "page two must actually have been attempted"


def test_a_page_reporting_more_without_a_cursor_returns_none(tmp_path):
    """`hasNextPage: true` with no `endCursor` to fetch the next page with
    cannot be paginated through -- an unreadable answer, not a complete
    one-page read."""
    runner, _ = runner_for({"graphql": _gql(
        [_thread(False, False, "chatgpt-codex-connector", "a.ts")],
        has_next=True, end_cursor=None)})
    assert io_with(runner, tmp_path).unresolved_bot_threads("Ylopo", "example", 42) is None


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


# --- M8: head_sha() is shape-checked, exactly as its five siblings are -------
#
# The one accessor on this adapter that trusted its payload. A non-string
# `headRefOid` was handed straight back and reached
# `transition(head_sha=...)`, where sqlite refuses to bind it -- and that raise
# lands inside the due pass, which catches per loop and reschedules, so the
# same bad value comes back on the next tick and every tick after it. None
# instead is a `head_unreadable` observation, which is bounded.


def test_a_non_string_head_reads_as_unreadable_not_as_a_sha(tmp_path):
    for value in (7, ["b" * 40], {"oid": "b" * 40}, True, None):
        runner, _ = runner_for({"view": json.dumps(
            {"labels": [], "headRefOid": value,
             "author": {"login": "Chadtao206"}})})
        assert io_with(runner, tmp_path).head_sha("Ylopo", "example", 42) is None, \
            f"{value!r} is not a sha"


def test_a_missing_head_key_reads_as_unreadable(tmp_path):
    runner, _ = runner_for({"view": json.dumps({"labels": []})})
    assert io_with(runner, tmp_path).head_sha("Ylopo", "example", 42) is None


def test_a_head_from_an_unreadable_pr_is_unreadable(tmp_path):
    runner, _ = runner_for({})  # empty stdout: nothing to parse
    assert io_with(runner, tmp_path).head_sha("Ylopo", "example", 42) is None


def test_a_scalar_payload_does_not_reach_the_head_accessor(tmp_path):
    """`gh` answering with a JSON scalar rather than an object: the old
    `(data or {}).get(...)` form raised AttributeError here."""
    runner, _ = runner_for({"view": json.dumps("nope")})
    assert io_with(runner, tmp_path).head_sha("Ylopo", "example", 42) is None


def test_a_missing_or_unreadable_state_reads_as_unknown_not_as_closed(tmp_path):
    """Unknown must never be read as "not open": that answer closes the loop,
    and terminal states are absorbing."""
    missing, _ = runner_for({"view": json.dumps({"labels": []})})
    assert io_with(missing, tmp_path).state("Ylopo", "example", 42) is None

    unreadable, _ = runner_for({})  # empty stdout
    assert io_with(unreadable, tmp_path).state("Ylopo", "example", 42) is None

    scalar, _ = runner_for({"view": json.dumps({"labels": [], "state": 7})})
    assert io_with(scalar, tmp_path).state("Ylopo", "example", 42) is None


# --- HOME in subprocess environment so gh writes its state correctly ----------


def test_subprocess_environment_contains_home(tmp_path):
    """The gh binary resolves its state directory relative to HOME, so it must
    be present in the subprocess environment. Without it, gh writes state into
    the process's working directory, creating .local/state/gh/device-id wherever
    the subprocess happens to be running."""
    env_captured = []

    def capturing_runner(command, **kwargs):
        env_captured.append(kwargs.get("env", {}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    io = io_with(capturing_runner, tmp_path)
    io.has_hold("Ylopo", "example", 42)

    assert env_captured, "runner must have been called"
    env = env_captured[0]
    assert "HOME" in env, "HOME must be in the subprocess environment"
    assert env["HOME"] == "/Users/ctao", "HOME must be set to the user home directory"


def test_token_is_passed_via_environment_not_argv(tmp_path):
    """The token must never appear in argv, where it could be logged or exposed.
    It must be passed only through the GH_TOKEN environment variable."""
    commands_captured = []

    def capturing_runner(command, **kwargs):
        commands_captured.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    io = io_with(capturing_runner, tmp_path)
    io.has_hold("Ylopo", "example", 42)

    assert commands_captured, "runner must have been called"
    argv = " ".join(commands_captured[0])
    assert "secret" not in argv, "the token must not appear in argv"
    assert "GH_TOKEN" not in argv, "GH_TOKEN variable name must not appear in argv"
