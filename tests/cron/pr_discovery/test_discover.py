"""Tests for the discovery readiness filter.

Every test in this file passes with no network, no `gh` binary and no real
credential: the `gh` dependency `candidates()` takes is always either
`_FakeGh` (a plain in-memory double) or `GhClient` driven by a
`_ScriptedRunner` fake in place of `subprocess.run` and a `tmp_path` token
file in place of the machine's own. `_gh()` builds a single-record fake
around the exact overrides the task brief's Step 1 skeleton uses (`labels=`,
`checks=`, `repo=`, `draft=`, `author=`), plus a few this suite adds for
filter clauses the skeleton named in prose but didn't show a test for (state,
org allowlist) and for the fail-closed behaviour `candidates()` promises.

Two properties this file exists to pin, both of which shipped broken:

* an unreadable label shape must EXCLUDE, never normalise to "no labels" --
  otherwise `hermes-hold` and `hermes-owner-andrej` are decorative; and
* enumeration failing must be REPORTABLE, never indistinguishable from
  "nothing qualified" -- otherwise a total outage looks like a quiet tick.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from pr_discovery.discover import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    BANNED_REPO_SUFFIXES,
    DETAIL_ATTEMPTS,
    RETRY_DEADLINE,
    SEARCH_ATTEMPTS,
    SEARCH_LIMIT,
    TIMEOUT,
    Candidate,
    GhClient,
    IntakeSource,
    Listing,
    _aggregate_checks,
    candidates,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeGh:
    """`open_prs(org, author) -> Listing | None`, returning one canned
    listing (or `records`, if given) regardless of what it is asked -- the
    org/author allowlisting under test is `candidates()`'s job, not this
    fake's, so the fake hands back whatever it was built with and lets the
    filter decide."""

    def __init__(self, records, *, truncated=False, failed_pr_reads=0):
        self._listing = Listing(records=tuple(records), truncated=truncated,
                                failed_pr_reads=failed_pr_reads)
        self.calls: list[tuple[str, str]] = []

    def open_prs(self, org, author):
        self.calls.append((org, author))
        return self._listing


class _FailingGh:
    """`open_prs` that always answers "could not read this org"."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def open_prs(self, org, author):
        self.calls.append((org, author))


class _FakeReviewed:
    """`already_requested(owner, repo, number, head_sha) -> bool | None`, the
    one method `candidates()`'s `reviewed` dependency has to have.

    `heads` is the set of `(owner, repo, number, head_sha)` identities
    boris_intake already holds a review request for. `unreadable=True` makes
    every answer `None`, which is "the review database could not be read" --
    and the case whose degradation direction is the OPPOSITE of every other
    unreadable answer in this module, on purpose (see
    `test_an_unreadable_intake_database_admits_the_pull_request`).
    """

    def __init__(self, heads=(), *, unreadable=False):
        self._heads = {tuple(head) for head in heads}
        self._unreadable = unreadable
        self.calls: list[tuple] = []

    def already_requested(self, owner, repo, number, head_sha):
        self.calls.append((owner, repo, number, head_sha))
        if self._unreadable:
            return None
        return (owner, repo, number, head_sha) in self._heads


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
               banned_repos=frozenset({"Ylopo/ylopo-infra"}),
               reviewed=_FakeReviewed())


# ---------------------------------------------------------------------------
# Readiness filter -- one test per clause (brief's Step 1, plus the clauses
# named in prose that Step 1's skeleton didn't show a test for: state OPEN,
# and org allowlist).
# ---------------------------------------------------------------------------

def test_excludes_pr_owned_by_the_loop():
    result = candidates(_gh(labels=["hermes-owner-andrej"]), **_args())
    assert result.candidates == ()


def test_excludes_held_pr():
    assert candidates(_gh(labels=["hermes-hold"]), **_args()).candidates == ()


def test_excludes_failing_checks():
    assert candidates(_gh(checks="FAILURE"), **_args()).candidates == ()


def test_excludes_pending_checks():
    """Pending is 'not yet', not 'no' -- it must re-qualify later, not be
    posted now."""
    assert candidates(_gh(checks="PENDING"), **_args()).candidates == ()


def test_excludes_infra_repo():
    assert candidates(_gh(repo="ylopo-infra"), **_args()).candidates == ()


def test_excludes_infra_repo_regardless_of_case():
    """GitHub resolves repo names case-insensitively -- `Ylopo/YLOPO-INFRA`
    IS `Ylopo/ylopo-infra`, the exact-match ban lanes.py already refuses."""
    assert candidates(_gh(repo="YLOPO-INFRA"), **_args()).candidates == ()


def test_excludes_mixed_case_infra_suffix_match():
    assert candidates(_gh(repo="Platform-Infra"), **_args()).candidates == ()


def test_excludes_mixed_case_infrastructure_suffix_match():
    assert candidates(_gh(repo="Billing-INFRASTRUCTURE"), **_args()).candidates == ()


def test_banned_repos_parameter_matches_regardless_of_operator_casing():
    """`banned_repos` is operator-typed configuration (mirrors
    PR_REVIEW_EXCLUDED_REPOS on the lanes.py side); an entry spelled
    "Ylopo/Custom-Banned" must still match a PR record spelled
    "ylopo/custom-banned"."""
    args = _args()
    args["banned_repos"] = frozenset({"Ylopo/Custom-Banned"})
    assert candidates(_gh(repo="custom-banned"), **args).candidates == ()


def test_excludes_draft_and_non_chad_author():
    assert candidates(_gh(draft=True), **_args()).candidates == ()
    assert candidates(_gh(author="someone-else"), **_args()).candidates == ()


def test_excludes_pr_that_is_not_open():
    """MERGED and CLOSED are the two states this can plausibly be besides
    OPEN -- both must exclude, not just be treated as 'not OPEN == closed'
    in one specific direction."""
    assert candidates(_gh(state="MERGED"), **_args()).candidates == ()
    assert candidates(_gh(state="CLOSED"), **_args()).candidates == ()


def test_excludes_pr_from_an_unlisted_org():
    """`candidates()` only ever asks `gh.open_prs` about orgs in `orgs`, but
    a non-conforming (or compromised) `gh` implementation could still answer
    with a record for some other owner. That record must never be trusted
    just because it arrived in the response for an allowlisted org."""
    gh = _FakeGh([_record(owner="SomeOtherOrg")])
    assert candidates(gh, **_args()).candidates == ()


def test_excludes_record_without_a_head_sha():
    """`head_sha` is the ledger identity AND the intake dedup key
    (`...@<sha>`). A record that cannot supply one must be excluded, not
    admitted as `Candidate(head_sha=None)` -- that candidate would dedup
    against the literal string "None" for every PR in the fleet."""
    assert candidates(_gh(head_sha=None), **_args()).candidates == ()
    assert candidates(_gh(head_sha=""), **_args()).candidates == ()


def test_admits_a_clean_pr():
    result = candidates(_gh(), **_args())
    assert len(result.candidates) == 1
    assert result.candidates[0].head_sha == "abc123"


def test_admitted_candidate_carries_every_field():
    c = candidates(_gh(), **_args()).candidates[0]
    assert c == Candidate(owner="Ylopo", repo="million-stars", number=42,
                          head_sha="abc123", merge_state="CLEAN",
                          checks_state="SUCCESS", has_hold=False)


def test_a_healthy_pass_reports_itself_complete():
    result = candidates(_gh(), **_args())
    assert result.complete is True
    assert result.failed_orgs == () and result.truncated_orgs == ()
    assert result.config_errors == ()
    assert result.failed_pr_reads == ()


# ---------------------------------------------------------------------------
# Labels: an unreadable shape must EXCLUDE, never normalise to "no labels".
# ---------------------------------------------------------------------------

def test_excludes_label_list_holding_a_non_string():
    """A list whose entries are not label names is not a readable answer to
    "which labels does this PR carry", so it cannot be read as "not held"."""
    assert candidates(_gh(labels=[12345]), **_args()).candidates == ()
    assert candidates(_gh(labels=[{"name": "x"}]), **_args()).candidates == ()


def test_label_matching_is_exact_and_case_sensitive():
    """Recorded parity with `github_io.has_hold` and `lanes.py`, which both
    compare label names exactly -- NOT a loose match this filter forgot to
    add. `Hermes-Hold` is a different label and does not hold."""
    assert len(candidates(_gh(labels=["Hermes-Hold"]), **_args()).candidates) == 1


# ---------------------------------------------------------------------------
# Fail-closed behaviour: candidates() must never raise on a bad `gh` answer,
# and must never report a failure as an empty-but-healthy result.
# ---------------------------------------------------------------------------

def test_enumeration_failure_is_reported_not_silently_empty():
    """The whole point of the result object: `None` from `gh.open_prs` (a
    whole org's listing could not be read -- no credential, network down, a
    garbled response) must be reportable as a FAILURE. An empty candidate
    list alone is indistinguishable from "nothing qualified", which is how a
    permanent outage hides as a healthy quiet tick."""
    result = candidates(_FailingGh(), **_args())
    assert result.candidates == ()
    assert result.failed_orgs == ("Ylopo",)
    assert result.complete is False


def test_one_failing_org_does_not_block_a_healthy_one():
    class _MixedGh:
        def open_prs(self, org, author):
            if org == "Ylopo":
                return None
            return Listing(records=(_record(owner=org),))

    result = candidates(_MixedGh(), orgs=["Ylopo", "OtherOrg"],
                        author="Chadtao206", banned_repos=frozenset(),
                        reviewed=_FakeReviewed())
    assert len(result.candidates) == 1
    assert result.candidates[0].owner == "OtherOrg"
    # The healthy org's PRs are still returned -- and the failure is still
    # named, rather than being written off because the pass "found things".
    assert result.failed_orgs == ("Ylopo",)


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
    {"owner": "Ylopo", "repo": "x", "number": 1, "head_sha": "abc",
     "state": "OPEN", "draft": False, "author": "Chadtao206",
     "labels": None, "checks_state": "SUCCESS"},
])
def test_malformed_raw_record_is_excluded_not_crashed(bad_record):
    gh = _FakeGh([bad_record])
    assert candidates(gh, **_args()).candidates == ()


def test_open_prs_non_listing_answer_is_reported_as_a_failed_org():
    class _WrongShapeGh:
        def open_prs(self, org, author):
            return [{"not": "a Listing"}]

    result = candidates(_WrongShapeGh(), **_args())
    assert result.candidates == ()
    assert result.failed_orgs == ("Ylopo",)


def test_truncated_listing_is_reported_without_discarding_what_was_found():
    gh = _FakeGh([_record()], truncated=True)
    result = candidates(gh, **_args())
    assert len(result.candidates) == 1        # what we saw is still real
    assert result.truncated_orgs == ("Ylopo",)  # ...but it was not all of it
    assert result.complete is False


# ---------------------------------------------------------------------------
# `failed_pr_reads`: a per-PR detail read that failed (`gh pr view`) must be
# REPORTED, not folded into "this PR did not qualify" -- the same discipline
# `failed_orgs` applies at the org level, one level down.
# ---------------------------------------------------------------------------

def test_a_failed_pr_read_is_reported_not_silently_skipped():
    """`failed_pr_reads` is carried by the `Listing`, independent of
    `records` -- a PR dropped by `GhClient.open_prs` before it ever became a
    record never shows up in `records` at all (see the `GhClient` tests
    below), so `candidates()` must surface the count from the `Listing`
    itself rather than trying to infer it from what `records` contains."""
    gh = _FakeGh([_record()], failed_pr_reads=1)
    result = candidates(gh, **_args())
    assert len(result.candidates) == 1  # the healthy record still admits...
    assert result.failed_pr_reads != ()  # ...but the drop is still reported
    assert "1 pull request" in result.failed_pr_reads[0]
    assert result.complete is False


def test_the_failed_pr_read_count_is_summed_across_orgs():
    class _TwoOrgGh:
        def open_prs(self, org, author):
            return Listing(records=(), failed_pr_reads=2)

    result = candidates(_TwoOrgGh(), orgs=["Ylopo", "AcmeCo"],
                        author="Chadtao206", banned_repos=frozenset(),
                        reviewed=_FakeReviewed())
    assert "4 pull request" in result.failed_pr_reads[0]


def test_a_healthy_listing_reports_no_failed_pr_reads():
    result = candidates(_gh(), **_args())
    assert result.failed_pr_reads == ()


def test_the_failed_pr_read_report_carries_no_github_authored_text():
    """Reaches a human through the cron's stdout, the same surface
    `unverified_heads` and `post.py`'s docstring are about: no owner, repo,
    number or head may appear, only a count."""
    gh = _FakeGh([_record()], failed_pr_reads=1)
    result = candidates(gh, **_args())
    assert "million-stars" not in result.failed_pr_reads[0]
    assert "42" not in result.failed_pr_reads[0]
    assert "abc123" not in result.failed_pr_reads[0]
    assert "Ylopo" not in result.failed_pr_reads[0]


# ---------------------------------------------------------------------------
# Parameter guards: a bad argument costs the whole tick if it raises, and is
# worse than useless if it is quietly obeyed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, expect", [
    (dict(orgs=None), "orgs"),
    (dict(orgs="Ylopo"), "orgs"),          # a str would iterate CHARACTERS
    (dict(orgs=[]), "orgs"),               # enumerates nothing, silently
    (dict(orgs=["Ylopo", 7]), "orgs"),
    (dict(author=None), "author"),
    (dict(author=""), "author"),
    (dict(banned_repos=None), "banned_repos"),
    # A str would silently become SUBSTRING matching.
    (dict(banned_repos="Ylopo/ylopo-infra Ylopo/other"), "banned_repos"),
    (dict(banned_repo_suffixes=None), "banned_repo_suffixes"),
    (dict(banned_repo_suffixes="-infra"), "banned_repo_suffixes"),
])
def test_bad_parameters_are_reported_as_config_errors_not_raised(kwargs, expect):
    gh = _gh()
    args = _args()
    args.update(kwargs)
    result = candidates(gh, **args)
    assert result.candidates == ()
    assert any(expect in err for err in result.config_errors), result.config_errors
    assert result.complete is False
    # Nothing was enumerated: with an untrustworthy ban list or org list we
    # cannot know what we are allowed to post, so we ask GitHub nothing.
    assert gh.calls == []


def test_suffix_ban_can_be_disabled_explicitly():
    """`()` is the documented way to opt out -- distinct from the `None` a
    caller reaches by accident, which is a config error above."""
    args = _args()
    args["banned_repo_suffixes"] = ()
    result = candidates(_gh(repo="platform-infra"), **args)
    assert len(result.candidates) == 1


# ---------------------------------------------------------------------------
# Infra-ban parity with pr_remediation/lanes.py, pinned as a drift guard.
#
# The runtime keeps a literal (the deployed copy under `~/.hermes/scripts/`
# cannot import `pr_remediation`); this test imports the real thing and
# asserts the two agree case by case -- the same arrangement
# `lanes.MAX_BRIEF_CHARS` uses against kanban's `_CTX_MAX_BODY_BYTES`.
# ---------------------------------------------------------------------------

# Every row the review measured a gap on, plus a control that must be
# admitted by both and mixed-case variants proving the ban is
# case-insensitive on both sides (GitHub resolves repo names
# case-insensitively, so `Ylopo/YLOPO-INFRA` IS `Ylopo/ylopo-infra`).
_PARITY_REPOS = [
    "Ylopo/ylopo-infra",                      # banned: exact + suffix
    "Ylopo/ylopo-github-actions-workflows",   # banned: exact only
    "Ylopo/platform-infra",                   # banned: suffix only
    "Ylopo/billing-infrastructure",           # banned: suffix only
    "Ylopo/million-stars",                    # admitted by both
    "Ylopo/infra-tools",                      # suffix is a SUFFIX, not a prefix
    "Ylopo/YLOPO-INFRA",                      # banned: exact + suffix, mixed case
    "Ylopo/YLOPO-GITHUB-ACTIONS-WORKFLOWS",   # banned: exact only, mixed case
    "Ylopo/Platform-Infra",                   # banned: suffix only, mixed case
]


def _lanes_excludes(full: str) -> bool:
    from pr_remediation import lanes
    # Mirrors `lanes._repo_is_excluded` exactly, including its casefold --
    # `_excluded_repos()` now returns casefolded entries, so `full` must be
    # casefolded here too or this helper stops matching the real function it
    # exists to shadow.
    full = full.casefold()
    return (full in lanes._excluded_repos()
            or full.endswith(lanes._EXCLUDED_REPO_SUFFIXES))


def _discovery_excludes(full: str) -> bool:
    from pr_remediation import lanes
    owner, _, repo = full.partition("/")
    result = candidates(_FakeGh([_record(owner=owner, repo=repo)]),
                        orgs=[owner], author="Chadtao206",
                        banned_repos=lanes._excluded_repos(),
                        reviewed=_FakeReviewed())
    return result.candidates == ()


@pytest.mark.parametrize("full", _PARITY_REPOS)
def test_infra_ban_agrees_with_lanes(full):
    assert _discovery_excludes(full) == _lanes_excludes(full), full


def test_suffix_constant_has_not_drifted_from_lanes():
    from pr_remediation import lanes
    assert BANNED_REPO_SUFFIXES == lanes._EXCLUDED_REPO_SUFFIXES


def test_case_insensitive_ban_is_shared_behaviour():
    """The former case-sensitivity hole is closed on both sides: GitHub
    resolves repo names case-insensitively, so an upper-cased infra repo
    must be refused by both implementations, not merely agreed upon (the
    parametrized parity test above would also pass if both sides still
    admitted it -- False == False agrees too). Asserted explicitly so a
    regression on either side that reintroduces the hole fails loudly here,
    not just as a silent parity match."""
    assert _lanes_excludes("Ylopo/YLOPO-INFRA") is True
    assert _discovery_excludes("Ylopo/YLOPO-INFRA") is True


# ---------------------------------------------------------------------------
# Multi-org / multi-PR aggregation
# ---------------------------------------------------------------------------

def test_aggregates_admitted_prs_across_orgs():
    class _TwoOrgGh:
        def open_prs(self, org, author):
            return Listing(records=(_record(owner=org, repo="repo-a",
                                            number=1),))

    result = candidates(_TwoOrgGh(), orgs=["Ylopo", "AcmeCo"],
                        author="Chadtao206", banned_repos=frozenset(),
                        reviewed=_FakeReviewed())
    assert {c.owner for c in result.candidates} == {"Ylopo", "AcmeCo"}


def test_one_excluded_pr_does_not_hide_others_in_the_same_org():
    gh = _FakeGh([
        _record(number=1, checks="FAILURE"),
        _record(number=2),
    ])
    result = candidates(gh, **_args())
    assert [c.number for c in result.candidates] == [2]


# ---------------------------------------------------------------------------
# GhClient: the production `gh` adapter. Fully injected -- `_ScriptedRunner`
# stands in for `subprocess.run` and a tmp_path file for the real credential,
# so these tests never shell out and never read the machine's token.
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _Clock:
    """The injected `sleep`/`clock` pair, driven by hand.

    Nothing in this suite is allowed to really sleep -- `GhClient` now backs off
    between retries, and a suite that waited out those backoffs (let alone
    `RETRY_DEADLINE`) is a suite people stop running. `slept` records what the
    adapter asked for, and `step` is how much the clock advances on its own per
    reading, which is how a test walks the retry window shut without waiting.
    """

    def __init__(self, *, step=0.0):
        self.now = 0.0
        self.step = step
        self.slept: list[float] = []

    def __call__(self):
        value = self.now
        self.now += self.step
        return value

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


# The wrapper every read is routed through, spelled out rather than imported
# from `discover`: a test that compared argv[0] against the same constant the
# production code builds argv[0] from would pass no matter which script that
# constant named, including `gh` itself.
GHX_DISCOVERY_PATH = "/Users/ctao/.hermes/scripts/pr/ghx_discovery.sh"


class _ScriptedRunner:
    """Routes a `ghx_discovery.sh` invocation to a canned response keyed by
    which verb it is (search / pr view / pr checks), and records every call
    made so a test can assert on scoping (DISCOVERY_ORG_SCOPE) and on the
    environment the credential rides in.

    Keyed on the SUBCOMMAND, never on argv[0], so that the tests asserting
    which program is executed are the only place that fact is checked -- a
    router that dispatched on argv[0] would quietly stop matching (and raise
    "unscripted call") if the wrapper were swapped out, instead of letting the
    dedicated test report it."""

    def __init__(self, *, search=None, view=None, checks=None):
        self._search = search if search is not None else _Proc(1)
        self._view = view or {}
        self._checks = checks or {}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs.get("env") or {})))
        if argv[1:3] == ["search", "prs"]:
            return self._search
        if len(argv) >= 6 and argv[1:3] == ["pr", "view"]:
            key = argv[5]  # argv = [wrapper, "pr", "view", num, "-R", "<owner>/<repo>", ...]
            return self._view.get(key, _Proc(1))
        if len(argv) >= 6 and argv[1:3] == ["pr", "checks"]:
            key = argv[5]
            return self._checks.get(key, _Proc(1))
        raise AssertionError(f"unscripted call: {argv}")


def _token_file(tmp_path, content="ghp_test_token"):
    path = tmp_path / ".gh_token"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _client(runner, tmp_path, token="ghp_test_token", clock=None):
    """A `GhClient` that never really sleeps. Every test gets a hand-driven
    clock whether or not it inspects one, because several of them script a
    failing call and would otherwise sit through the real backoff."""
    clock = clock if clock is not None else _Clock()
    return GhClient(runner=runner, token_path=_token_file(tmp_path, token),
                    sleep=clock.sleep, clock=clock)


def _hit(**overrides):
    hit = {
        "number": 1,
        "repository": {"nameWithOwner": "Ylopo/repo-a"},
        "author": {"login": "Chadtao206"},
        "isDraft": False,
        "state": "OPEN",
        "labels": [],
    }
    hit.update(overrides)
    return hit


def test_ghclient_merges_search_and_detail_into_one_open_pr_record(tmp_path):
    search_out = json.dumps([_hit(
        number=7,
        repository={"nameWithOwner": "Ylopo/million-stars"},
        labels=[{"name": "needs-review"}],
    )])
    view_out = json.dumps({"headRefOid": "deadbeef", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"},
                             {"name": "lint", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/million-stars": _Proc(0, view_out)},
        checks={"Ylopo/million-stars": _Proc(0, checks_out)},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert listing.records == ({
        "owner": "Ylopo", "repo": "million-stars", "number": 7,
        "state": "OPEN", "draft": False, "author": "Chadtao206",
        "labels": ["needs-review"], "head_sha": "deadbeef",
        "merge_state": "CLEAN", "checks_state": "SUCCESS",
    },)
    assert listing.truncated is False
    assert listing.failed_pr_reads == 0
    # The per-PR reads name the PR in argv and stay inside the scoped org.
    pr_calls = [argv for argv, _ in runner.calls if argv[1] == "pr"]
    assert len(pr_calls) == 2  # one `pr view`, one `pr checks`
    for argv in pr_calls:
        assert argv[3:6] == ["7", "-R", "Ylopo/million-stars"], argv


def test_ghclient_search_failure_returns_none(tmp_path):
    """Discriminating on the returncode guard specifically: the failing
    process still carries a perfectly parseable body, so ONLY the non-zero
    exit can be what turns this into `None`. (`gh search` exits 4 with usage
    text when it has no credential -- the shape this whole guard exists
    for.) The version this replaced scripted an empty stdout, which the
    `not out.strip()` check answered independently, so it passed with the
    line it named deleted."""
    runner = _ScriptedRunner(search=_Proc(4, json.dumps([_hit()])))
    assert _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206") is None


def test_ghclient_skips_a_pr_whose_detail_read_fails_but_keeps_the_rest(tmp_path):
    search_out = json.dumps([
        _hit(number=1, repository={"nameWithOwner": "Ylopo/repo-a"}),
        _hit(number=2, repository={"nameWithOwner": "Ylopo/repo-b"}),
    ])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        # repo-a's `pr view` is left unscripted -> _Proc(1) (failure).
        view={"Ylopo/repo-b": _Proc(0, view_out)},
        checks={"Ylopo/repo-b": _Proc(0, checks_out)},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert [r["repo"] for r in listing.records] == ["repo-b"]
    # The skip is counted, not just silent -- see `Listing.failed_pr_reads`.
    assert listing.failed_pr_reads == 1


def test_ghclient_skips_a_pr_whose_detail_carries_no_head_sha(tmp_path):
    """A detail read that answers without a usable `headRefOid` is not a
    usable PR: `head_sha` is the ledger identity and the intake dedup key."""
    search_out = json.dumps([_hit()])
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    for bad_view in ({"mergeStateStatus": "CLEAN"},
                     {"headRefOid": None, "mergeStateStatus": "CLEAN"},
                     {"headRefOid": "", "mergeStateStatus": "CLEAN"}):
        runner = _ScriptedRunner(
            search=_Proc(0, search_out),
            view={"Ylopo/repo-a": _Proc(0, json.dumps(bad_view))},
            checks={"Ylopo/repo-a": _Proc(0, checks_out)},
        )
        listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
        assert listing.records == (), bad_view
        assert listing.failed_pr_reads == 1, bad_view


def test_ghclient_drops_a_search_hit_for_another_owner(tmp_path):
    """Defense in depth: the search is already `--owner=`-scoped, but a hit
    naming a different owner must never be trusted just because it arrived
    in this org's response -- it would carry that org's PR into a repo the
    ban list was never evaluated for."""
    search_out = json.dumps([
        _hit(number=1, repository={"nameWithOwner": "SomeOtherOrg/repo-a"}),
    ])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"SomeOtherOrg/repo-a": _Proc(0, view_out)},
        checks={"SomeOtherOrg/repo-a": _Proc(0, checks_out)},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert listing.records == ()


@pytest.mark.parametrize("labels_value", [
    "__absent__",                      # key missing entirely
    None,                              # null
    {"name": "hermes-hold"},           # a dict, not a list
    [{"id": 1, "name": None}],         # entry without a readable name
    [{"title": "hermes-hold"}],        # entry keyed differently
    [12345],                           # entry that is not an object at all
    ["hermes-hold"],                   # bare strings: not the shape gh emits
])
def test_ghclient_rejects_a_hit_whose_labels_cannot_be_read(tmp_path, labels_value):
    """C1's table, row by row. Every one of these used to normalise to `[]`
    -- "we read the labels and there were none" -- which is a positive claim
    with nothing behind it, and which admits a `hermes-hold` or
    `hermes-owner-andrej` PR the moment the label field is unreadable (or
    simply not requested). `github_io.has_hold` answers "assume held" for
    exactly this set; this must exclude for exactly this set."""
    hit = _hit()
    if labels_value == "__absent__":
        hit.pop("labels")
    else:
        hit["labels"] = labels_value
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, json.dumps([hit])),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert listing.records == ()


@pytest.mark.parametrize("checks_proc", [
    _Proc(1, ""),   # what `gh pr checks` really emits for "no checks reported"
    _Proc(0, ""),   # a zero-exit empty body, for completeness
])
def test_ghclient_no_checks_configured_does_not_read_as_success(tmp_path, checks_proc):
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": checks_proc},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert listing.records[0]["checks_state"] is None
    # A `gh pr checks` failure is NOT counted into `failed_pr_reads`: see
    # `DiscoveryResult.failed_pr_reads`'s docstring -- production cannot tell
    # "no checks configured" apart from "the read failed" at this boundary,
    # so counting it here would misreport a routine, correctly-excluded PR as
    # flakiness on nearly every tick.
    assert listing.failed_pr_reads == 0


def test_ghclient_unreadable_checks_read_is_the_live_exclusion_path(tmp_path):
    """`gh pr checks` exits 8 when checks are pending and 1 when any check
    failed, so `_run`'s returncode guard turns BOTH into `None` before a
    single row is parsed -- `_aggregate_checks(None) -> None` is the only
    mechanism that actually excludes red and pending CI in production. This
    scripts a non-zero exit whose stdout is a perfectly parseable all-green
    body, so admitting this PR is possible only if the returncode guard or
    the `None` arm is broken."""
    search_out = json.dumps([_hit(
        repository={"nameWithOwner": "Ylopo/million-stars"})])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    green = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/million-stars": _Proc(0, view_out)},
        checks={"Ylopo/million-stars": _Proc(8, green)},  # 8 == pending
    )
    client = _client(runner, tmp_path)
    listing = client.open_prs("Ylopo", "Chadtao206")
    assert listing.records[0]["checks_state"] is None
    # Excluded via the normal readiness filter (checks_state != SUCCESS),
    # not reported as a per-PR read failure -- this PR's fate is exactly as
    # legitimate-looking as a genuinely failing or pending build, which is
    # the whole reason `failed_pr_reads` deliberately does not cover it.
    assert listing.failed_pr_reads == 0
    result = candidates(client, **_args())
    assert result.candidates == ()
    assert result.failed_pr_reads == ()


def test_aggregate_checks_treats_an_empty_row_set_as_not_a_pass():
    """No checks configured is not evidence of a clean build. (Reachable
    from a caller supplying rows directly; `_checks` itself now answers
    `None` for an empty body -- see its comment for why `[]` was
    unreachable in production.)"""
    assert _aggregate_checks([]) is None
    assert _aggregate_checks(None) is None


def test_ghclient_failing_check_beats_a_passing_sibling(tmp_path):
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"},
                             {"name": "lint", "state": "FAILURE"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert listing.records[0]["checks_state"] == "FAILURE"


def test_ghclient_pending_check_is_neither_success_nor_failure(tmp_path):
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"},
                             {"name": "slow-e2e", "state": "PENDING"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    listing = _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert listing.records[0]["checks_state"] == "PENDING"


def test_candidates_end_to_end_through_ghclient(tmp_path):
    """The full pipe, `candidates()` driving a real `GhClient` against a
    scripted runner -- no fake `gh` shortcut, still no network."""
    search_out = json.dumps([_hit(
        number=9, repository={"nameWithOwner": "Ylopo/million-stars"})])
    view_out = json.dumps({"headRefOid": "f00dcafe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/million-stars": _Proc(0, view_out)},
        checks={"Ylopo/million-stars": _Proc(0, checks_out)},
    )
    result = candidates(_client(runner, tmp_path), **_args())
    assert result.candidates == (
        Candidate(owner="Ylopo", repo="million-stars", number=9,
                  head_sha="f00dcafe", merge_state="CLEAN",
                  checks_state="SUCCESS", has_hold=False),)
    assert result.complete is True


# ---------------------------------------------------------------------------
# The subprocess environment: credential, HOME, and what must NOT be in argv.
# `subprocess.run(env=...)` REPLACES the environment, so everything `gh`
# needs has to be named explicitly -- the reason this component could never
# have run at all.
# ---------------------------------------------------------------------------

def _search_call(runner):
    return next((argv, env) for argv, env in runner.calls
                if argv[1:3] == ["search", "prs"])


def test_every_github_read_goes_through_the_command_allowlisted_wrapper(tmp_path):
    """No bare `gh` anywhere in this adapter -- enumeration included, which
    was the last one left. The wrapper refuses every verb but `search prs`,
    `pr view` and `pr checks`, so a future edit here cannot add a write call
    without also editing (and re-testing) the wrapper."""
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert len(runner.calls) == 3  # search + pr view + pr checks
    for argv, _ in runner.calls:
        assert argv[0] == GHX_DISCOVERY_PATH, argv


def test_every_call_is_scoped_to_the_org_being_enumerated(tmp_path):
    """`DISCOVERY_ORG_SCOPE` is what the wrapper checks `--owner=` and `-R`
    against; unset, it refuses every verb, so this env line is the difference
    between a working tick and a dead one."""
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert len(runner.calls) == 3
    for argv, env in runner.calls:
        assert env.get("DISCOVERY_ORG_SCOPE") == "Ylopo", argv


def test_the_credential_travels_to_the_wrapper_as_a_path(tmp_path):
    """The wrapper reads the 0600 file itself and exports `GH_TOKEN` just
    before `exec gh`, so what this adapter has to get right is the PATH: point
    it at the wrong file (or at nothing, leaving the wrapper's own default) and
    a configured token override applies to none of the three reads. The token's
    VALUE is deliberately absent from the environment -- the wrapper would
    overwrite it anyway."""
    runner = _ScriptedRunner(search=_Proc(0, "[]"))
    path = _token_file(tmp_path, "ghp_secret_value")
    GhClient(runner=runner, token_path=path).open_prs("Ylopo", "Chadtao206")
    _argv, env = _search_call(runner)
    assert env.get("DISCOVERY_GH_TOKEN_PATH") == str(path)
    assert "ghp_secret_value" not in str(env)


def test_credential_never_appears_in_argv(tmp_path):
    runner = _ScriptedRunner(search=_Proc(0, "[]"))
    _client(runner, tmp_path, token="ghp_secret_value").open_prs("Ylopo", "Chadtao206")
    for argv, _ in runner.calls:
        assert "ghp_secret_value" not in " ".join(argv)


@pytest.mark.parametrize("token", ["", "   \n"])
def test_unusable_token_file_fails_the_search_closed(tmp_path, token):
    """An empty (or whitespace-only) token file is locally detectable, so
    `gh` is never spawned with it -- the fail-closed guarantee must not
    depend on how `gh` reacts to an empty GH_TOKEN."""
    runner = _ScriptedRunner(search=_Proc(0, json.dumps([_hit()])))
    client = GhClient(runner=runner, token_path=_token_file(tmp_path, token))
    assert client.open_prs("Ylopo", "Chadtao206") is None
    assert runner.calls == []


def test_missing_token_file_fails_the_search_closed(tmp_path):
    runner = _ScriptedRunner(search=_Proc(0, json.dumps([_hit()])))
    client = GhClient(runner=runner, token_path=tmp_path / "nope" / ".gh_token")
    assert client.open_prs("Ylopo", "Chadtao206") is None
    assert runner.calls == []


def test_every_subprocess_environment_carries_home(tmp_path):
    """With HOME unset, `gh` writes `.local/state/gh/device-id` into whatever
    directory the cron happens to be running in. Fixed once already on this
    branch for `pr_remediation/github_io.py` (203c17b0a6); pinned here for all
    three wrapper calls -- the wrapper `exec`s `gh`, so it inherits whatever
    environment this adapter assembles -- so it cannot regress a third
    time."""
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert len(runner.calls) == 3  # search + pr view + pr checks
    for argv, env in runner.calls:
        assert env.get("HOME") == "/Users/ctao", argv


def test_every_call_is_pointed_at_the_same_token_file(tmp_path):
    """`ghx_discovery.sh` sources its credential from
    DISCOVERY_GH_TOKEN_PATH; all three reads must name one and the same file,
    or a configured token path silently applies to part of the adapter."""
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    path = _token_file(tmp_path)
    GhClient(runner=runner, token_path=path).open_prs("Ylopo", "Chadtao206")
    assert len(runner.calls) == 3
    assert all(env.get("DISCOVERY_GH_TOKEN_PATH") == str(path)
               for _, env in runner.calls)


# ---------------------------------------------------------------------------
# The search argv itself: untested, one edit away from disabling a kill
# switch, and one omission away from silently capping enumeration.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "labels",      # both kill switches -- its absence fails OPEN
    "state",
    "isDraft",
    "author",
    "number",
    "repository",
])
def test_search_requests_every_field_the_filter_depends_on(tmp_path, field):
    """`_SEARCH_ARGS` is a comma-joined string constant, so dropping one
    field is a one-character-looking edit. Dropping `state`, `isDraft` or
    `author` fails closed; dropping `labels` fails OPEN -- `hermes-hold` and
    `hermes-owner-andrej` would both be evaluated against a PR that appears
    to have no labels at all. Every field is named here for that reason."""
    runner = _ScriptedRunner(search=_Proc(0, "[]"))
    _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    argv, _ = _search_call(runner)
    json_fields = argv[argv.index("--json") + 1].split(",")
    assert field in json_fields


def test_search_is_scoped_and_limited(tmp_path):
    """The org/author scoping, and an EXPLICIT limit: `gh search prs`
    defaults to 30, Chad runs 22-23 open PRs in scope today, and a capped
    listing is indistinguishable from a short one. 200 matches the cron this
    service retires (`pr_review_gather.py:259`)."""
    runner = _ScriptedRunner(search=_Proc(0, "[]"))
    _client(runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    argv, _ = _search_call(runner)
    assert f"--limit={SEARCH_LIMIT}" in argv
    assert SEARCH_LIMIT == 200
    assert "--state=open" in argv
    assert "--owner=Ylopo" in argv
    assert "--author=Chadtao206" in argv


def test_a_listing_at_the_limit_is_reported_as_truncated(tmp_path):
    """A response exactly `SEARCH_LIMIT` long cannot be told apart from a
    truncated one, so it is reported as truncated rather than silently
    passed off as the whole picture -- the same discipline as reporting a
    failed enumeration instead of an empty list."""
    hits = [_hit(number=n) for n in range(1, SEARCH_LIMIT + 1)]
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, json.dumps(hits)),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    client = _client(runner, tmp_path)
    listing = client.open_prs("Ylopo", "Chadtao206")
    assert listing.truncated is True
    assert len(listing.records) == SEARCH_LIMIT
    assert candidates(client, **_args()).truncated_orgs == ("Ylopo",)


def test_a_short_listing_is_not_reported_as_truncated(tmp_path):
    search_out = json.dumps([_hit()])
    view_out = json.dumps({"headRefOid": "cafebabe", "mergeStateStatus": "CLEAN"})
    checks_out = json.dumps([{"name": "build", "state": "SUCCESS"}])
    runner = _ScriptedRunner(
        search=_Proc(0, search_out),
        view={"Ylopo/repo-a": _Proc(0, view_out)},
        checks={"Ylopo/repo-a": _Proc(0, checks_out)},
    )
    assert _client(runner, tmp_path).open_prs(
        "Ylopo", "Chadtao206").truncated is False


# ---------------------------------------------------------------------------
# Bounded retry of transient server-side faults.
#
# GitHub's search endpoint was measured 503-ing on roughly half of all live
# calls, and with no retry the whole tick flapped: 1, 2, 4, 5 and 6 candidates
# reported within twenty minutes from an unchanged set of pull requests, one
# tick that enumerated no org at all, one that reported four unreadable PRs.
# Retrying the identical call succeeded immediately every time.
#
# Two things these tests hold down together, and the second is the one that
# matters more: that a transient fault is survived, AND that an exhausted one
# still reaches `failed_orgs` / `failed_pr_reads` with `complete` False. A retry
# that converted a real outage into a quiet empty result would reintroduce
# exactly the ambiguity `DiscoveryResult` exists to remove.
# ---------------------------------------------------------------------------

#: What a 503 looks like by the time it reaches this adapter: `gh` has exited
#: non-zero and `ghx_discovery.sh` passed the status through. The body is
#: deliberately empty, as `gh`'s is -- the error text goes to stderr.
_FAULT = _Proc(1, "")


class _FlakyRunner:
    """Fails the first N calls of each verb, then answers normally.

    Counts calls per verb (`counts`), which is what lets a test assert that the
    search and the per-PR reads got DIFFERENT attempt budgets rather than merely
    that both eventually worked.
    """

    def __init__(self, *, search_out="[]", view_out="{}", checks_out="[]",
                 search_failures=0, view_failures=0, checks_failures=0):
        self._out = {"search": search_out, "view": view_out,
                     "checks": checks_out}
        self._failures = {"search": search_failures, "view": view_failures,
                          "checks": checks_failures}
        self.counts = {"search": 0, "view": 0, "checks": 0}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs.get("env") or {})))
        verb = "search" if argv[1:3] == ["search", "prs"] else argv[2]
        if verb not in self.counts:
            raise AssertionError(f"unscripted call: {argv}")
        self.counts[verb] += 1
        if self.counts[verb] <= self._failures[verb]:
            return _FAULT
        return _Proc(0, self._out[verb])


def _flaky(**kwargs):
    return _FlakyRunner(
        search_out=json.dumps([_hit(
            number=7, repository={"nameWithOwner": "Ylopo/million-stars"})]),
        view_out=json.dumps({"headRefOid": "cafebabe",
                             "mergeStateStatus": "CLEAN"}),
        checks_out=json.dumps([{"name": "build", "state": "SUCCESS"}]),
        **kwargs)


def test_a_search_that_503s_once_succeeds_on_the_second_attempt(tmp_path):
    """The measured live shape: try1 503, try2 fine. Before the retry this was
    a whole tick lost -- "1 org(s) could not be enumerated at all"."""
    clock = _Clock()
    runner = _flaky(search_failures=1)
    listing = _client(runner, tmp_path, clock=clock).open_prs(
        "Ylopo", "Chadtao206")
    assert runner.counts["search"] == 2
    assert [r["number"] for r in listing.records] == [7]
    assert listing.failed_pr_reads == 0
    # Backed off between the attempts, and briefly -- these faults clear in
    # about a second, so a long sleep would spend the deadline waiting.
    assert clock.slept == [BACKOFF_BASE]


def test_a_per_pr_read_that_503s_once_succeeds_on_the_second_attempt(tmp_path):
    """The same fault one level down, where it used to surface as "4 pull
    request(s) could not be read" and as a candidate count that changed every
    tick."""
    clock = _Clock()
    runner = _flaky(view_failures=1, checks_failures=1)
    listing = _client(runner, tmp_path, clock=clock).open_prs(
        "Ylopo", "Chadtao206")
    assert runner.counts["view"] == 2
    assert runner.counts["checks"] == 2
    assert listing.failed_pr_reads == 0
    assert listing.records[0]["head_sha"] == "cafebabe"
    assert listing.records[0]["checks_state"] == "SUCCESS"
    assert clock.slept == [BACKOFF_BASE, BACKOFF_BASE]


def test_a_per_pr_read_survives_the_two_consecutive_faults_measured_live(tmp_path):
    """The shape that set the per-PR budget. A probe against one real PR during
    a bad window answered `pr checks` 503, 503, 503, then 200 -- so a
    two-attempt budget loses a pull request whenever two faults land in a row,
    which at the observed ~50% fault rate is a quarter of them. Two live ticks
    90 seconds apart showed precisely that: 4 candidates, then 1, from an
    unchanged set of pull requests.

    Worth the third attempt here more than anywhere else, because a `pr checks`
    fault is the one failure in this module that is NOT reported: it is
    indistinguishable from "checks are pending", so it excludes the PR while
    looking exactly like a routine exclusion."""
    clock = _Clock()
    runner = _flaky(checks_failures=2)
    listing = _client(runner, tmp_path, clock=clock).open_prs(
        "Ylopo", "Chadtao206")
    assert runner.counts["checks"] == 3
    assert listing.records[0]["checks_state"] == "SUCCESS"
    assert clock.slept == [BACKOFF_BASE, 2 * BACKOFF_BASE]


def test_the_backoff_doubles_from_a_second_and_stays_short(tmp_path):
    """Long enough to let a server-side fault clear (they clear in about a
    second), short enough that spending the entire search budget costs 11s of
    the 240s window rather than minutes -- a backoff that grew without a cap
    would spend the window sleeping instead of retrying."""
    clock = _Clock()
    runner = _flaky(search_failures=99)
    _client(runner, tmp_path, clock=clock).open_prs("Ylopo", "Chadtao206")
    assert clock.slept == [1.0, 2.0, 4.0, 4.0]  # doubling, then the cap
    assert sum(clock.slept) < 15


def test_a_search_that_never_recovers_is_still_reported_as_a_failed_org(tmp_path):
    """Exhaustion must be LOUD. The retry spends its whole budget and then
    answers exactly what it answered before this retry existed, so the
    enumeration failure still reaches a human through `failed_orgs` and still
    makes the tick incomplete. A retry that returned an empty listing here
    would be the Critical this package was already fixed for once."""
    clock = _Clock()
    runner = _flaky(search_failures=99)
    client = _client(runner, tmp_path, clock=clock)
    assert client.open_prs("Ylopo", "Chadtao206") is None
    assert runner.counts["search"] == SEARCH_ATTEMPTS
    result = candidates(client, **_args())
    assert result.failed_orgs == ("Ylopo",)
    assert result.candidates == ()
    assert result.complete is False


def test_a_pr_read_that_never_recovers_is_still_counted_as_a_failed_read(tmp_path):
    """Same guarantee for the per-PR half: the PR is withheld and SAID to be
    withheld, via the channel that tells "we could not read it" apart from
    "the filter looked at it and said no"."""
    clock = _Clock()
    runner = _flaky(view_failures=99)
    client = _client(runner, tmp_path, clock=clock)
    listing = client.open_prs("Ylopo", "Chadtao206")
    assert runner.counts["view"] == DETAIL_ATTEMPTS
    assert listing.records == ()
    assert listing.failed_pr_reads == 1
    result = candidates(client, **_args())
    assert result.candidates == ()
    assert len(result.failed_pr_reads) == 1
    assert "could not be read" in result.failed_pr_reads[0]
    assert result.complete is False


def test_the_search_is_retried_harder_than_a_per_pr_read(tmp_path):
    """The two budgets are deliberately unequal, and this asserts the
    BEHAVIOUR rather than the constants: losing the one search call costs every
    PR in the org, losing one per-PR read costs one PR for one 30-minute tick
    and is reported. Equal budgets would either under-protect the tick or blow
    through the interval (see `SEARCH_ATTEMPTS` for that arithmetic)."""
    search_runner = _flaky(search_failures=99)
    _client(search_runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    read_runner = _flaky(view_failures=99)
    _client(read_runner, tmp_path).open_prs("Ylopo", "Chadtao206")
    assert search_runner.counts["search"] > read_runner.counts["view"]
    assert (search_runner.counts["search"], read_runner.counts["view"]) == (
        SEARCH_ATTEMPTS, DETAIL_ATTEMPTS)


def test_the_retry_deadline_stops_retrying_and_reports_normally(tmp_path):
    """The part that actually bounds the worst case. With the tick's retry
    window already shut, a failing call is attempted ONCE and its failure is
    reported through the ordinary channel -- no budget is spent waiting, and
    nothing is silently dropped in exchange.

    The clock is stepped by the whole window per reading, so the window that
    the first call opens is closed by the time the first retry is considered."""
    clock = _Clock(step=RETRY_DEADLINE)
    runner = _flaky(search_failures=99)
    client = _client(runner, tmp_path, clock=clock)
    assert client.open_prs("Ylopo", "Chadtao206") is None
    assert runner.counts["search"] == 1  # first attempt only, no retries
    assert clock.slept == []
    assert candidates(client, **_args()).failed_orgs == ("Ylopo",)


def test_the_deadline_does_not_suppress_first_attempts(tmp_path):
    """A closed window must stop RETRYING, not stop reading. Every call still
    gets its first attempt, so a long tick keeps enumerating (and keeps
    succeeding, whenever the fault has cleared on its own) instead of
    degrading into a silently empty pass."""
    clock = _Clock(step=RETRY_DEADLINE)
    runner = _flaky()  # nothing fails; the window is shut from the first call
    listing = _client(runner, tmp_path, clock=clock).open_prs(
        "Ylopo", "Chadtao206")
    assert (runner.counts["search"], runner.counts["view"],
            runner.counts["checks"]) == (1, 1, 1)
    assert [r["number"] for r in listing.records] == [7]


def test_a_missing_credential_is_never_retried(tmp_path):
    """The one failure in this adapter that is NOT transient. A token file that
    is missing, unreadable or empty will still be missing a second later, so it
    must fail fast: no spawn, no backoff, no budget spent pretending."""
    clock = _Clock()
    runner = _flaky()
    client = GhClient(runner=runner, token_path=tmp_path / "nope" / ".gh_token",
                      sleep=clock.sleep, clock=clock)
    assert client.open_prs("Ylopo", "Chadtao206") is None
    assert runner.calls == []
    assert clock.slept == []


def test_the_retry_budgets_fit_inside_the_cron_interval():
    """The arithmetic that chose these numbers, pinned so raising one cannot
    quietly produce a cron that overruns its own schedule.

    A tick makes 2N+1 wrapper calls at `TIMEOUT` each. Retries only START
    inside the deadline, so the worst case is the un-retried pass, plus the
    window, plus one straggler attempt that began just inside it. Naive
    three-attempts-everywhere is the choice this rules out: 47*3*30 = 70
    minutes against a 30-minute interval.
    """
    interval = 30 * 60
    calls = 2 * 23 + 1
    unretried_worst = calls * TIMEOUT
    worst = unretried_worst + RETRY_DEADLINE + TIMEOUT + BACKOFF_CAP
    assert worst < interval, worst
    assert calls * SEARCH_ATTEMPTS * TIMEOUT > interval  # why a deadline exists
    assert SEARCH_ATTEMPTS > DETAIL_ATTEMPTS > 1
    # The faults clear in about a second; a backoff long enough to matter
    # against the deadline would be spending the window on sleep.
    assert BACKOFF_BASE <= 1.0 and BACKOFF_CAP <= 4.0


# ---------------------------------------------------------------------------
# "Boris was already asked about this head" -- the clause, and the one place
# in this package where an unreadable answer ADMITS instead of excluding.
#
# The retiring cron's SOP had this clause ("no Boris verdict for head") and
# discovery did not, which a live shadow tick caught: two of the five pull
# requests it selected were already in `boris_intake.db` at those exact heads.
# ---------------------------------------------------------------------------

#: `_record()`'s own identity, which is what a candidate built from it carries.
_DEFAULT_HEAD = ("Ylopo", "million-stars", 42, "abc123")


def _reviewed_args(reviewed):
    args = _args()
    args["reviewed"] = reviewed
    return args


def test_excludes_a_head_boris_has_already_been_asked_about():
    result = candidates(_gh(), **_reviewed_args(_FakeReviewed([_DEFAULT_HEAD])))
    assert result.candidates == ()
    # Skipping a head that was genuinely already requested is the steady
    # state, not a degradation -- nothing about this tick is incomplete.
    assert result.unverified_heads == ()
    assert result.complete is True


def test_a_moved_head_on_the_same_pull_request_is_admitted():
    """The whole reason the match includes `head_sha`: new commits are new
    work and are owed a new review."""
    reviewed = _FakeReviewed([("Ylopo", "million-stars", 42, "def456")])
    assert len(candidates(_gh(), **_reviewed_args(reviewed)).candidates) == 1


def test_another_pull_request_number_in_the_same_repo_is_admitted():
    reviewed = _FakeReviewed([("Ylopo", "million-stars", 43, "abc123")])
    assert len(candidates(_gh(), **_reviewed_args(reviewed)).candidates) == 1


def test_the_same_head_in_another_repo_is_admitted():
    reviewed = _FakeReviewed([("Ylopo", "other-repo", 42, "abc123")])
    assert len(candidates(_gh(), **_reviewed_args(reviewed)).candidates) == 1


def test_the_clause_asks_about_the_candidates_own_identity():
    """Four fields, in this order -- a transposed pair would answer False for
    every real head and silently disable the clause."""
    reviewed = _FakeReviewed()
    candidates(_gh(), **_reviewed_args(reviewed))
    assert reviewed.calls == [_DEFAULT_HEAD]


def test_an_excluded_pull_request_is_never_asked_about():
    """The clause runs after the readiness filter, so a held or red-CI pull
    request costs no database read at all."""
    reviewed = _FakeReviewed()
    candidates(_gh(labels=["hermes-hold"]), **_reviewed_args(reviewed))
    assert reviewed.calls == []


def test_an_unreadable_intake_database_admits_the_pull_request():
    """The degradation direction here is the OPPOSITE of the `labels` case,
    deliberately: skipping would leave this pull request silently unreviewed
    with no backstop, while posting costs at most a duplicate message that the
    intake idempotency key already dedups.

    The fake is seeded WITH this exact head, so the only way the pull request
    survives is the unreadable answer being treated as "proceed".
    """
    reviewed = _FakeReviewed([_DEFAULT_HEAD], unreadable=True)
    assert len(candidates(_gh(), **_reviewed_args(reviewed)).candidates) == 1


def test_an_unreadable_intake_database_is_reported_not_silent():
    """Proceeding is only safe if it is also VISIBLE: a filter that quietly
    stopped filtering is the defect `DiscoveryResult` exists to prevent."""
    result = candidates(_gh(), **_reviewed_args(_FakeReviewed(unreadable=True)))
    assert result.unverified_heads != ()
    assert result.complete is False


def test_the_degradation_report_counts_every_pull_request_it_covers():
    gh = _FakeGh([_record(number=1), _record(number=2), _record(number=3)])
    result = candidates(gh, **_reviewed_args(_FakeReviewed(unreadable=True)))
    assert len(result.candidates) == 3
    assert len(result.unverified_heads) == 1
    assert "3 pull request" in result.unverified_heads[0]


def test_the_degradation_report_carries_no_github_authored_text():
    """`unverified_heads` reaches a human through the cron's stdout, the same
    surface `post.py`'s docstring is about. The sentence is composed of this
    module's own literals and a count -- never of a record's own fields, which
    are written by whoever opened the pull request."""
    gh = _FakeGh([_record(repo="million-stars", number=42)])
    result = candidates(gh, **_reviewed_args(_FakeReviewed(unreadable=True)))
    assert "million-stars" not in result.unverified_heads[0]
    assert "42" not in result.unverified_heads[0]


def test_a_readable_intake_database_reports_no_degradation():
    result = candidates(_gh(), **_args())
    assert result.unverified_heads == ()
    assert result.complete is True


class _ExplodingReviewed:
    def already_requested(self, owner, repo, number, head_sha):
        raise RuntimeError("the review database detonated")


class _WrongShapeReviewed:
    """Answers a truthy non-bool. Without the shape check it would read as
    "already reviewed" and EXCLUDE -- the wrong degradation direction, reached
    by a dependency that is merely non-conforming."""

    def already_requested(self, owner, repo, number, head_sha):
        return "yes"


@pytest.mark.parametrize("reviewed", [_ExplodingReviewed(), _WrongShapeReviewed()],
                         ids=["raises", "answers a non-bool"])
def test_a_non_conforming_reviewed_dependency_admits_and_is_reported(reviewed):
    """`IntakeSource` answers `None` for every failure it knows about; this is
    the defense in depth against the ones it does not. Either way the cost is
    one pull request's precision, never the tick -- and never silence."""
    result = candidates(_gh(), **_reviewed_args(reviewed))
    assert len(result.candidates) == 1
    assert result.unverified_heads != ()
    assert result.complete is False


def test_the_reviewed_dependency_is_required_not_defaulted():
    """A required keyword, so a caller that forgets to wire the clause fails
    loudly instead of quietly reposting heads Boris has already reviewed --
    the same reason `DiscoveryResult` refuses to be iterable."""
    args = _args()
    del args["reviewed"]
    with pytest.raises(TypeError):
        candidates(_gh(), **args)


# ---------------------------------------------------------------------------
# IntakeSource: the production reader over `boris_intake.db`.
#
# Read-only URI mode, no import of `boris_intake` in production, and every
# failure answers `None` (= "proceed, and report it"). The fixture below is
# built from `boris_intake`'s OWN schema so a column rename there breaks these
# tests instead of silently making the production query match nothing.
# ---------------------------------------------------------------------------

_INTAKE_COLUMNS = (
    "request_id", "review_identity", "gate_fingerprint", "owner", "repo",
    "pr_number", "head_sha", "pr_author", "policy_json", "policy_fingerprint",
    "gate_snapshot_json", "requester_user_id", "requester_email",
    "source_channel", "source_message_ts", "root_thread_ts", "permalink",
    "workflow_state", "created_at", "updated_at",
)


def _intake_db(tmp_path, rows=((*_DEFAULT_HEAD, "complete"),), *,
               fingerprint="sha256:defaultpolicy"):
    from boris_intake.store import _SCHEMA

    path = tmp_path / "boris_intake.db"
    db = sqlite3.connect(str(path))
    db.executescript(_SCHEMA)
    placeholders = ",".join("?" * len(_INTAKE_COLUMNS))
    for index, (owner, repo, number, head_sha, state) in enumerate(rows):
        db.execute(
            f"INSERT INTO review_requests ({','.join(_INTAKE_COLUMNS)}) "
            f"VALUES ({placeholders})",
            (f"br_{index}", f"identity_{index}", "gate_fp", owner, repo,
             number, head_sha, "Chadtao206", "{}", fingerprint, "{}",
             "U07E3AHV686", "chad@example.com", "C0BPCKPEFCG", "1.0", "1.0",
             "https://github.com/x", state, 1.0, 1.0),
        )
    db.commit()
    db.close()
    return path


def test_intake_source_finds_a_head_a_review_was_already_requested_for(tmp_path):
    source = IntakeSource(_intake_db(tmp_path))
    assert source.already_requested(*_DEFAULT_HEAD) is True


@pytest.mark.parametrize("head", [
    ("Ylopo", "million-stars", 42, "zzz999"),   # same PR, later head
    ("Ylopo", "million-stars", 43, "abc123"),   # different PR number
    ("Ylopo", "other-repo", 42, "abc123"),      # different repo
    ("OtherOrg", "million-stars", 42, "abc123"),  # different owner
])
def test_intake_source_answers_false_for_a_head_it_has_never_seen(tmp_path, head):
    assert IntakeSource(_intake_db(tmp_path)).already_requested(*head) is False


@pytest.mark.parametrize("state", ["awaiting_gate", "reviewing", "complete",
                                   "slack_unavailable"])
def test_intake_source_ignores_the_workflow_state(tmp_path, state):
    """An in-flight request means a review is already running; a complete one
    means the verdict exists. Ignoring the state is strictly broader, and
    broader skips more -- the direction that cannot cause duplicate work."""
    path = _intake_db(tmp_path, rows=[(*_DEFAULT_HEAD, state)])
    assert IntakeSource(path).already_requested(*_DEFAULT_HEAD) is True


def test_intake_source_matches_whatever_policy_the_review_ran_under(tmp_path):
    """The policy fingerprint is deliberately NOT part of the match: it is
    part of the intake idempotency key, but discovery always posts with the
    default policy and matching without it is the broader, safer side."""
    path = _intake_db(tmp_path, fingerprint="sha256:some-other-policy")
    assert IntakeSource(path).already_requested(*_DEFAULT_HEAD) is True


def test_intake_source_matches_regardless_of_case(tmp_path):
    """GitHub resolves owner/repo case-insensitively (the same argument the
    infra ban casefolds for) and a commit SHA is case-insensitive hex, so this
    names the same head -- and matching it skips more, the safe direction."""
    source = IntakeSource(_intake_db(tmp_path))
    assert source.already_requested("ylopo", "Million-Stars", 42, "ABC123") is True


def test_intake_source_answers_none_for_a_missing_database(tmp_path):
    assert IntakeSource(tmp_path / "not-there.db").already_requested(
        *_DEFAULT_HEAD) is None


def test_intake_source_answers_none_when_the_table_is_missing(tmp_path):
    path = tmp_path / "boris_intake.db"
    sqlite3.connect(str(path)).close()
    assert IntakeSource(path).already_requested(*_DEFAULT_HEAD) is None


def test_intake_source_answers_none_for_a_file_that_is_not_a_database(tmp_path):
    path = tmp_path / "boris_intake.db"
    path.write_text("this is not a SQLite database")
    assert IntakeSource(path).already_requested(*_DEFAULT_HEAD) is None


def test_intake_source_cannot_write_to_the_review_database(tmp_path):
    """Read-only URI mode, exactly as `pr_remediation/boris_source.py` opens
    the same database: the review path's integrity does not depend on this
    reader behaving."""
    source = IntakeSource(_intake_db(tmp_path))
    assert source.already_requested(*_DEFAULT_HEAD) is True
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        source._db.execute("DELETE FROM review_requests")


def test_candidates_excludes_a_head_the_real_intake_database_holds(tmp_path):
    """The whole pipe, `candidates()` driving the real reader against a real
    SQLite file -- no fake reviewed-heads shortcut."""
    args = _reviewed_args(IntakeSource(_intake_db(tmp_path)))
    assert candidates(_gh(), **args).candidates == ()


def test_candidates_admits_everything_when_the_real_database_is_missing(tmp_path):
    args = _reviewed_args(IntakeSource(tmp_path / "not-there.db"))
    result = candidates(_gh(), **args)
    assert len(result.candidates) == 1
    assert result.unverified_heads != ()
    assert result.complete is False
