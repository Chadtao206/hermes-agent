"""Tests for the discovery readiness filter.

Every test in this file passes with no network and no `gh` binary: the `gh`
dependency `candidates()` takes is always either `_FakeGh` (a plain in-memory
double) or `GhClient` driven by a `_ScriptedRunner` fake in place of
`subprocess.run`. `_gh()` builds a single-record fake around the exact
overrides the task brief's Step 1 skeleton uses (`labels=`, `checks=`,
`repo=`, `draft=`, `author=`), plus a few this suite adds for filter clauses
the skeleton named in prose but didn't show a test for (state, org
allowlist) and for the fail-closed behaviour `candidates()` promises.
"""
from __future__ import annotations

import pytest

from pr_discovery.discover import Candidate, GhClient, candidates


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeGh:
    """`open_prs(org, author) -> list[dict] | None`, returning one canned
    record (or `records`, if given) regardless of what it is asked -- the
    org/author allowlisting under test is `candidates()`'s job, not this
    fake's, so the fake hands back whatever it was built with and lets the
    filter decide."""

    def __init__(self, records):
        self._records = records
        self.calls: list[tuple[str, str]] = []

    def open_prs(self, org, author):
        self.calls.append((org, author))
        return self._records


class _FailingGh:
    """`open_prs` that always answers "could not read this org"."""

    def open_prs(self, org, author):
        return None


def _record(**overrides):
    checks = overrides.pop("checks", "SUCCESS")
    record = {
        "owner": "Ylopo",
        "repo": "million-stars",
        "number": 42,
        "head_sha": "abc123",
        "state": "OPEN",
        "draft": False,
        "author": "Chadtao206",
        "labels": [],
        "checks_state": checks,
        "merge_state": "CLEAN",
    }
    record.update(overrides)
    return record


def _gh(**overrides):
    """One PR, with `overrides` applied on top of a fully-qualifying default
    record. Mirrors the task brief's Step 1 `_gh(...)` fixture exactly:
    `_gh(labels=[...])`, `_gh(checks="FAILURE")`, `_gh(repo="ylopo-infra")`,
    `_gh(draft=True)`, `_gh(author="someone-else")`, `_gh()`."""
    return _FakeGh([_record(**overrides)])


def _args():
    return dict(orgs=["Ylopo"], author="Chadtao206",
               banned_repos=frozenset({"Ylopo/ylopo-infra"}))


# ---------------------------------------------------------------------------
# Readiness filter -- one test per clause (brief's Step 1, plus the clauses
# named in prose that Step 1's skeleton didn't show a test for: state OPEN,
# and org allowlist).
# ---------------------------------------------------------------------------

def test_excludes_pr_owned_by_the_loop():
    c = candidates(_gh(labels=["hermes-owner-andrej"]), **_args())
    assert c == []


def test_excludes_held_pr():
    assert candidates(_gh(labels=["hermes-hold"]), **_args()) == []


def test_excludes_failing_checks():
    assert candidates(_gh(checks="FAILURE"), **_args()) == []


def test_excludes_pending_checks():
    """Pending is 'not yet', not 'no' -- it must re-qualify later, not be
    posted now."""
    assert candidates(_gh(checks="PENDING"), **_args()) == []


def test_excludes_infra_repo():
    assert candidates(_gh(repo="ylopo-infra"), **_args()) == []


def test_excludes_draft_and_non_chad_author():
    assert candidates(_gh(draft=True), **_args()) == []
    assert candidates(_gh(author="someone-else"), **_args()) == []


def test_excludes_pr_that_is_not_open():
    """MERGED and CLOSED are the two states this can plausibly be besides
    OPEN -- both must exclude, not just be treated as 'not OPEN == closed'
    in one specific direction."""
    assert candidates(_gh(state="MERGED"), **_args()) == []
    assert candidates(_gh(state="CLOSED"), **_args()) == []


def test_excludes_pr_from_an_unlisted_org():
    """`candidates()` only ever asks `gh.open_prs` about orgs in `orgs`, but
    a non-conforming (or compromised) `gh` implementation could still answer
    with a record for some other owner. That record must never be trusted
    just because it arrived in the response for an allowlisted org."""
    gh = _FakeGh([_record(owner="SomeOtherOrg")])
    assert candidates(gh, **_args()) == []


def test_admits_a_clean_pr():
    c = candidates(_gh(), **_args())
    assert len(c) == 1 and c[0].head_sha == "abc123"


def test_admitted_candidate_carries_every_field():
    c = candidates(_gh(), **_args())[0]
    assert c == Candidate(owner="Ylopo", repo="million-stars", number=42,
                          head_sha="abc123", merge_state="CLEAN",
                          checks_state="SUCCESS", has_hold=False)


# ---------------------------------------------------------------------------
# Fail-closed behaviour: candidates() must never raise on a bad `gh` answer.
# ---------------------------------------------------------------------------

def test_open_prs_failure_is_skipped_not_raised():
    """None from gh.open_prs (a whole org's listing could not be read) must
    not raise, and must not be conflated with 'genuinely no PRs' for any
    OTHER org in the same call."""
    assert candidates(_FailingGh(), **_args()) == []


def test_one_failing_org_does_not_block_a_healthy_one():
    class _MixedGh:
        def open_prs(self, org, author):
            if org == "Ylopo":
                return None
            return [_record(owner=org)]

    c = candidates(_MixedGh(), orgs=["Ylopo", "OtherOrg"],
                   author="Chadtao206", banned_repos=frozenset())
    assert len(c) == 1 and c[0].owner == "OtherOrg"


@pytest.mark.parametrize("bad_record", [
    None,
    "not-a-dict",
    42,
    {},  # missing every required field
    {"owner": "Ylopo", "repo": "x", "number": "not-an-int",
     "head_sha": "abc", "state": "OPEN", "draft": False,
     "author": "Chadtao206", "labels": [], "checks_state": "SUCCESS"},
    {"owner": "Ylopo", "repo": "x", "number": 1, "head_sha": "abc",
     "state": "OPEN", "draft": False, "author": "Chadtao206",
     "labels": "not-a-list", "checks_state": "SUCCESS"},
])
def test_malformed_raw_record_is_excluded_not_crashed(bad_record):
    gh = _FakeGh([bad_record])
    assert candidates(gh, **_args()) == []


def test_open_prs_non_list_answer_is_treated_like_none():
    class _WrongShapeGh:
        def open_prs(self, org, author):
            return {"not": "a list"}

    assert candidates(_WrongShapeGh(), **_args()) == []


# ---------------------------------------------------------------------------
# Multi-org / multi-PR aggregation
# ---------------------------------------------------------------------------

def test_aggregates_admitted_prs_across_orgs():
    class _TwoOrgGh:
        def open_prs(self, org, author):
            return [_record(owner=org, repo="repo-a", number=1)]

    c = candidates(_TwoOrgGh(), orgs=["Ylopo", "AcmeCo"], author="Chadtao206",
                   banned_repos=frozenset())
    assert {cand.owner for cand in c} == {"Ylopo", "AcmeCo"}


def test_one_excluded_pr_does_not_hide_others_in_the_same_org():
    gh = _FakeGh([
        _record(number=1, checks="FAILURE"),
        _record(number=2),
    ])
    c = candidates(gh, **_args())
    assert [cand.number for cand in c] == [2]


# ---------------------------------------------------------------------------
# GhClient: the production `gh` adapter. Fully injected -- `_ScriptedRunner`
# stands in for `subprocess.run` so these tests never shell out either.
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _ScriptedRunner:
    """Routes a `gh`/`ghx_ro.sh` invocation to a canned response keyed by
    which verb it is (search / pr view / pr checks), and records every call
    made so a test can assert on scoping (BORIS_PR_SCOPE)."""

    def __init__(self, *, search=None, view=None, checks=None):
        self._search = search if search is not None else _Proc(1)
        self._view = view or {}
        self._checks = checks or {}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs.get("env") or {})))
        if argv[:2] == ["gh", "search"]:
            return self._search
        if len(argv) >= 6 and argv[1:3] == ["pr", "view"]:
            key = argv[5]  # argv = [ghx_ro, "pr", "view", num, "-R", "<owner>/<repo>", ...]
            return self._view.get(key, _Proc(1))
        if len(argv) >= 6 and argv[1:3] == ["pr", "checks"]:
            key = argv[5]
            return self._checks.get(key, _Proc(1))
        raise AssertionError(f"unscripted call: {argv}")


def test_ghclient_merges_search_and_detail_into_one_open_pr_record():
    import json

    search_out = json.dumps([{
        "number": 7,
        "repository": {"nameWithOwner": "Ylopo/million-stars"},
        "author": {"login": "Chadtao206"},
        "isDraft": False,
        "state": "OPEN",
        "labels": [{"name": "needs-review"}],
    }])
    view_out = json.dumps({"headRefOid": "deadbeef", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"},
                             {"name": "lint", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/million-stars": _Proc(0, view_out)},
        checks={"Ylopo/million-stars": _Proc(0, checks_out)},
    )
    client = GhClient(runner=runner)
    result = client.open_prs("Ylopo", "Chadtao206")
    assert result == [{
        "owner": "Ylopo", "repo": "million-stars", "number": 7,
        "state": "OPEN", "draft": False, "author": "Chadtao206",
        "labels": ["needs-review"], "head_sha": "deadbeef",
        "merge_state": "CLEAN", "checks_state": "SUCCESS",
    }]
    # BORIS_PR_SCOPE must pin exactly the PR the per-PR calls concern.
    scoped_envs = [env for argv, env in runner.calls if argv[0] == "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"]
    assert all(env.get("BORIS_PR_SCOPE") == "Ylopo/million-stars#7" for env in scoped_envs)
    assert len(scoped_envs) == 2  # one `pr view`, one `pr checks`


def test_ghclient_search_failure_returns_none():
    runner = _ScriptedRunner(search=_Proc(1, ""))
    assert GhClient(runner=runner).open_prs("Ylopo", "Chadtao206") is None


def test_ghclient_skips_a_pr_whose_detail_read_fails_but_keeps_the_rest():
    import json

    search_out = json.dumps([
        {"number": 1, "repository": {"nameWithOwner": "Ylopo/repo-a"},
         "author": {"login": "Chadtao206"}, "isDraft": False,
         "state": "OPEN", "labels": []},
        {"number": 2, "repository": {"nameWithOwner": "Ylopo/repo-b"},
         "author": {"login": "Chadtao206"}, "isDraft": False,
         "state": "OPEN", "labels": []},
    ])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        # repo-a's `pr view` is left unscripted -> _Proc(1) (failure).
        view={"Ylopo/repo-b": _Proc(0, view_out)},
        checks={"Ylopo/repo-b": _Proc(0, checks_out)},
    )
    result = GhClient(runner=runner).open_prs("Ylopo", "Chadtao206")
    assert [r["repo"] for r in result] == ["repo-b"]


def test_ghclient_no_checks_configured_does_not_read_as_success():
    import json

    search_out = json.dumps([{
        "number": 1, "repository": {"nameWithOwner": "Ylopo/repo-a"},
        "author": {"login": "Chadtao206"}, "isDraft": False,
        "state": "OPEN", "labels": [],
    }])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, "")},  # no checks configured
    )
    result = GhClient(runner=runner).open_prs("Ylopo", "Chadtao206")
    assert result[0]["checks_state"] != "SUCCESS"


def test_ghclient_failing_check_beats_a_passing_sibling():
    import json

    search_out = json.dumps([{
        "number": 1, "repository": {"nameWithOwner": "Ylopo/repo-a"},
        "author": {"login": "Chadtao206"}, "isDraft": False,
        "state": "OPEN", "labels": [],
    }])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"},
                             {"name": "lint", "state": "FAILURE"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    result = GhClient(runner=runner).open_prs("Ylopo", "Chadtao206")
    assert result[0]["checks_state"] == "FAILURE"


def test_ghclient_pending_check_is_neither_success_nor_failure():
    import json

    search_out = json.dumps([{
        "number": 1, "repository": {"nameWithOwner": "Ylopo/repo-a"},
        "author": {"login": "Chadtao206"}, "isDraft": False,
        "state": "OPEN", "labels": [],
    }])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"},
                             {"name": "slow-e2e", "state": "PENDING"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    result = GhClient(runner=runner).open_prs("Ylopo", "Chadtao206")
    assert result[0]["checks_state"] == "PENDING"


def test_candidates_end_to_end_through_ghclient():
    """The full pipe, `candidates()` driving a real `GhClient` against a
    scripted runner -- no fake `gh` shortcut, still no network."""
    import json

    search_out = json.dumps([{
        "number": 9, "repository": {"nameWithOwner": "Ylopo/million-stars"},
        "author": {"login": "Chadtao206"}, "isDraft": False,
        "state": "OPEN", "labels": [],
    }])
    view_out = json.dumps({"headRefOid": "f00dcafe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/million-stars": _Proc(0, view_out)},
        checks={"Ylopo/million-stars": _Proc(0, checks_out)},
    )
    result = candidates(GhClient(runner=runner), **_args())
    assert result == [Candidate(owner="Ylopo", repo="million-stars", number=9,
                                head_sha="f00dcafe", merge_state="CLEAN",
                                checks_state="SUCCESS", has_hold=False)]
