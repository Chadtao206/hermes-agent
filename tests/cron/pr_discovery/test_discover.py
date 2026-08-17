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

import pytest

from pr_discovery.discover import (
    BANNED_REPO_SUFFIXES,
    SEARCH_LIMIT,
    Candidate,
    GhClient,
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

    def __init__(self, records, *, truncated=False):
        self._listing = Listing(records=tuple(records), truncated=truncated)
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
                        author="Chadtao206", banned_repos=frozenset())
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
# admitted by both and the case-sensitivity hole both share.
_PARITY_REPOS = [
    "Ylopo/ylopo-infra",                      # banned: exact + suffix
    "Ylopo/ylopo-github-actions-workflows",   # banned: exact only
    "Ylopo/platform-infra",                   # banned: suffix only
    "Ylopo/billing-infrastructure",           # banned: suffix only
    "Ylopo/million-stars",                    # admitted by both
    "Ylopo/infra-tools",                      # suffix is a SUFFIX, not a prefix
    "Ylopo/YLOPO-INFRA",                      # bypasses BOTH (case-sensitive)
]


def _lanes_excludes(full: str) -> bool:
    from pr_remediation import lanes
    return (full in lanes._excluded_repos()
            or full.endswith(lanes._EXCLUDED_REPO_SUFFIXES))


def _discovery_excludes(full: str) -> bool:
    from pr_remediation import lanes
    owner, _, repo = full.partition("/")
    result = candidates(_FakeGh([_record(owner=owner, repo=repo)]),
                        orgs=[owner], author="Chadtao206",
                        banned_repos=lanes._excluded_repos())
    return result.candidates == ()


@pytest.mark.parametrize("full", _PARITY_REPOS)
def test_infra_ban_agrees_with_lanes(full):
    assert _discovery_excludes(full) == _lanes_excludes(full), full


def test_suffix_constant_has_not_drifted_from_lanes():
    from pr_remediation import lanes
    assert BANNED_REPO_SUFFIXES == lanes._EXCLUDED_REPO_SUFFIXES


def test_case_sensitivity_hole_is_shared_recorded_behaviour():
    """Recorded, not endorsed: an upper-cased repo name bypasses BOTH bans.
    Asserted so it stays a known shared property rather than a coincidence
    -- if `lanes.py` ever case-folds, this fails and discovery follows."""
    assert _lanes_excludes("Ylopo/YLOPO-INFRA") is False
    assert _discovery_excludes("Ylopo/YLOPO-INFRA") is False


# ---------------------------------------------------------------------------
# Multi-org / multi-PR aggregation
# ---------------------------------------------------------------------------

def test_aggregates_admitted_prs_across_orgs():
    class _TwoOrgGh:
        def open_prs(self, org, author):
            return Listing(records=(_record(owner=org, repo="repo-a",
                                            number=1),))

    result = candidates(_TwoOrgGh(), orgs=["Ylopo", "AcmeCo"],
                        author="Chadtao206", banned_repos=frozenset())
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


class _ScriptedRunner:
    """Routes a `gh`/`ghx_ro.sh` invocation to a canned response keyed by
    which verb it is (search / pr view / pr checks), and records every call
    made so a test can assert on scoping (BORIS_PR_SCOPE) and on the
    environment the credential rides in."""

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


def _token_file(tmp_path, content="ghp_test_token"):
    path = tmp_path / ".gh_token"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _client(runner, tmp_path, token="ghp_test_token"):
    return GhClient(runner=runner, token_path=_token_file(tmp_path, token))


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
    # BORIS_PR_SCOPE must pin exactly the PR the per-PR calls concern.
    scoped_envs = [env for argv, env in runner.calls
                   if argv[0] == "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"]
    assert all(env.get("BORIS_PR_SCOPE") == "Ylopo/million-stars#7"
               for env in scoped_envs)
    assert len(scoped_envs) == 2  # one `pr view`, one `pr checks`


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
    assert client.open_prs("Ylopo", "Chadtao206").records[0]["checks_state"] is None
    result = candidates(client, **_args())
    assert result.candidates == ()


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
    return next((argv, env) for argv, env in runner.calls if argv[0] == "gh")


def test_search_call_carries_the_credential_in_its_environment(tmp_path):
    """Without this the bare `gh search` runs unauthenticated and exits 4 on
    every tick, so `candidates()` returns nothing, forever, silently."""
    runner = _ScriptedRunner(search=_Proc(0, "[]"))
    _client(runner, tmp_path, token="ghp_secret_value").open_prs("Ylopo", "Chadtao206")
    _, env = _search_call(runner)
    assert env.get("GH_TOKEN") == "ghp_secret_value"


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
    branch for `pr_remediation/github_io.py` (203c17b0a6); pinned here for
    both the bare `gh search` and the two `ghx_ro.sh` calls so it cannot
    regress a third time."""
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


def test_ghx_calls_are_pointed_at_the_same_token_file(tmp_path):
    """`ghx_ro.sh` sources its own credential from BORIS_GH_TOKEN_PATH; both
    halves of the two-step read must use one and the same file, or a
    configured token path silently applies to only half the adapter."""
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
    ghx_envs = [env for argv, env in runner.calls
                if argv[0] == "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"]
    assert ghx_envs and all(env.get("BORIS_GH_TOKEN_PATH") == str(path)
                            for env in ghx_envs)


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
