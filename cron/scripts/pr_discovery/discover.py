"""Discovery: enumerate Chad's open pull requests and filter to the ones a
Boris review is owed for.

This is the enumerate-and-filter half of the deterministic discovery cron
that replaces `pr-review-hourly` (`/Users/ctao/.hermes/scripts/pr/pr_review_gather.py`,
whose `fetch_pr_list`/`fetch_pr_detail`/`compute_roster_flags` this module's
readiness filter mirrors). A later task builds the posting half; this module
never talks to Slack or `boris_intake`.

`candidates()` is deliberately pure with respect to I/O: every GitHub read
goes through the injected `gh` dependency, so the filter logic here can be
exercised (and mutation-tested) with no network and no `gh` binary. It never
raises on a malformed or missing answer from `gh` -- an unrecognised shape or
a failed read excludes the record (or, for a whole org's listing, is skipped)
rather than propagating, matching the discipline `pr_remediation/github_io.py`
documents at length: this runs unattended, and a raise here costs the whole
tick, not just one PR.

Readiness filter -- a PR is only admitted when ALL of these hold (see
`_evaluate`): state is OPEN, not draft, author is the configured `author`,
the record's owner matches the org it was fetched under (the allowlist is
enforced by only ever asking `gh` about `orgs`, then checked again here in
case a search response ever returns something it was not asked for), the
repo is not in `banned_repos`, neither the hold label nor the loop's
ownership label is present, and CI checks concluded successfully. A PR whose
checks are still pending is EXCLUDED, same as one with failing checks -- see
`_evaluate`'s comment on `checks_state` for why "not failing" is read as
"concluded successfully", not "not (yet) failing".
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

GHX_RO = "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"
_RUN_ENV = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"}
TIMEOUT = 30

# Fixed identity constants, duplicated from `pr_remediation.config.RemediationConfig`
# (`owner_label`, `hold_label`) rather than imported: `pr_remediation` is out of
# this task's scope fence (owned by a concurrent agent), and these two label
# names are the one thing both services must agree on byte-for-byte, so pinning
# them as plain module constants here -- exactly as `RemediationConfig` pins them
# -- means neither side can drift from the other by editing a shared import.
HOLD_LABEL = "hermes-hold"
OWNER_LABEL = "hermes-owner-andrej"


@dataclass(frozen=True)
class Candidate:
    """One pull request that has cleared every readiness filter clause.

    `merge_state` and `checks_state` ride along for the poster (a later
    task) to report -- they are not re-checked by anything that consumes a
    `Candidate`. `has_hold` is always `False` on an admitted candidate (a
    held PR is excluded before a `Candidate` is ever built); it is carried
    as a field, not a bare boolean return, so a poster inspecting a
    `Candidate` never has to re-derive "were we admitted" from other fields.
    """

    owner: str
    repo: str
    number: int
    head_sha: str
    merge_state: str
    checks_state: str
    has_hold: bool


def candidates(gh, *, orgs, author, banned_repos) -> list[Candidate]:
    """Chad's open pull requests across `orgs` that pass the readiness filter.

    `gh` is a dependency implementing `open_prs(org, author) -> list[dict] |
    None` (see `GhClient` below for the production adapter, and
    `tests/cron/pr_discovery/test_discover.py`'s `_gh()` fixture for the
    fake shape tests exercise). `None` means "this org's listing could not
    be read" and is skipped, not raised -- a transient failure against one
    org must not stop the other orgs in `orgs` from being discovered, and
    must never surface as "no PRs anywhere" to a caller that cannot tell
    that apart from a genuinely quiet org.
    """
    found: list[Candidate] = []
    for org in orgs:
        raw_list = gh.open_prs(org, author)
        if not isinstance(raw_list, list):
            # None (documented failure) or any other unexpected shape a
            # non-conforming `gh` implementation might hand back -- both
            # fail this one org closed rather than raising.
            continue
        for raw in raw_list:
            candidate = _evaluate(raw, org=org, author=author,
                                   banned_repos=banned_repos)
            if candidate is not None:
                found.append(candidate)
    return found


def _evaluate(raw: object, *, org: str, author: str,
              banned_repos) -> Candidate | None:
    """One raw PR record -> `Candidate`, or `None` if any filter clause
    excludes it (including the record simply not being shaped as expected).

    Every field is shape-checked before being trusted, the same discipline
    `pr_remediation/github_io.py`'s accessors use and for the same reason:
    an unrecognised shape must fail the one record closed, not raise out of
    the whole `candidates()` pass.
    """
    if not isinstance(raw, dict):
        return None

    owner = raw.get("owner")
    if not isinstance(owner, str) or not owner:
        return None
    repo = raw.get("repo")
    if not isinstance(repo, str) or not repo:
        return None
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    head_sha = raw.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return None

    # --- org allowlist ---------------------------------------------------
    # `candidates()` only ever asks `gh.open_prs` about orgs in `orgs`, so
    # this is defense in depth against a `gh` implementation (real or
    # faked) that hands back a record for some other owner -- it must never
    # be trusted just because it arrived in this call's response.
    if owner != org:
        return None

    # --- banned repo -------------------------------------------------------
    # Exact, case-sensitive match against `f"{owner}/{repo}"`, mirroring
    # `pr_remediation/lanes.py`'s `_repo_is_excluded` exact-match half
    # (`_excluded_repos()` membership) so the two independent enforcement
    # points agree on what they reject. Deliberately NOT also mirroring that
    # function's `-infra`/`-infrastructure` suffix check or its env-driven
    # default list -- `banned_repos` here is an explicit collection the
    # caller supplies, with no suffix parameter in this function's contract;
    # see this task's report for why that is a known, narrower ban than
    # `lanes.py`'s, and what a caller must do to restore parity.
    if f"{owner}/{repo}" in banned_repos:
        return None

    # --- state -------------------------------------------------------------
    if raw.get("state") != "OPEN":
        return None

    # --- draft ---------------------------------------------------------------
    # Only an explicit `False` admits. Missing, `None`, or any other value
    # is an unrecognised answer to "is this a draft?" and must fail closed
    # the same way `GitHubIO.has_hold` fails toward "assume held" on an
    # unreadable response -- posting a draft PR is the failure this guards.
    if raw.get("draft") is not False:
        return None

    # --- author --------------------------------------------------------------
    if raw.get("author") != author:
        return None

    # --- labels: hold + loop ownership --------------------------------------
    labels = raw.get("labels")
    if not isinstance(labels, list):
        return None
    if HOLD_LABEL in labels or OWNER_LABEL in labels:
        return None

    # --- CI checks -----------------------------------------------------------
    # "CI checks are not failing" is read as "checks have CONCLUDED
    # successfully" -- not "not yet failing". A PR whose checks are still
    # running answers PENDING here, and PENDING excludes exactly like
    # FAILURE does: it is "not yet", not "no", and the PR re-qualifies on a
    # later tick once its checks conclude. Only an exact "SUCCESS" admits;
    # anything else (FAILURE, PENDING, a missing/malformed value) excludes.
    checks_state = raw.get("checks_state")
    if checks_state != "SUCCESS":
        return None

    merge_state = raw.get("merge_state")
    if not isinstance(merge_state, str) or not merge_state:
        # Informational only (see `Candidate`'s docstring) -- never gates
        # admission, so an unreadable value degrades to a visible sentinel
        # rather than excluding an otherwise-qualifying PR.
        merge_state = "UNKNOWN"

    return Candidate(owner=owner, repo=repo, number=number, head_sha=head_sha,
                      merge_state=merge_state, checks_state=checks_state,
                      has_hold=False)


# ---------------------------------------------------------------------------
# Production `gh` adapter
# ---------------------------------------------------------------------------
#
# `candidates()` above depends only on `open_prs(org, author) -> list[dict] |
# None` -- it does not care how that list is produced. `GhClient` is the
# concrete, network-touching implementation the live discovery cron wires in;
# every test in this package's suite uses a fake instead (see
# `test_discover.py`), so nothing here is on that suite's hot path.
#
# A KNOWN GAP, flagged rather than silently worked around: `ghx_ro.sh` --
# named in this task's brief as "the" read-only wrapper -- is scoped to a
# single already-known pull request (`BORIS_PR_SCOPE=owner/repo#number`; its
# `pr` verb allows only `view|diff|checks|status`, each require-scoped to
# that one PR). It has no `search`/`list` verb and cannot enumerate PRs across
# an org -- confirmed by reading the script, not assumed. That makes it
# structurally unable to answer "what are Chad's open PRs in this org", which
# is the very first question discovery has to ask.
#
# `gh search prs --json` is ALSO narrower than it looks: GitHub's search API
# backs it, and that API does not expose `headRefOid`, `mergeStateStatus`, or
# CI conclusions at all -- only `pr view`/`pr checks` (single-PR GraphQL/REST
# calls) do. So `GhClient` below is a genuine two-step adapter: a single
# directly-invoked, read-only `gh search prs` call enumerates PR numbers per
# org (the step no wrapper supports), and `ghx_ro.sh` -- scoped per PR via
# `BORIS_PR_SCOPE`, exactly as it is designed to be used -- supplies the head
# SHA, merge state and CI conclusion for each one found. This is a deliberate,
# reported deviation from "reached through a wrapper, not bare" for the one
# operation (enumeration) no existing wrapper supports; see this task's report
# for the full reasoning and the two ways to close the gap (extend
# `ghx_ro.sh` with an org-scoped search verb, or accept this hybrid).
_SEARCH_ARGS = [
    "search", "prs", "--state=open", "--json",
    "number,repository,author,isDraft,state,labels",
]

# `gh pr checks --json state` conclusions that mean "this check is not a
# pass". `SUCCESS`/`NEUTRAL`/`SKIPPED` are the only ones treated as passing;
# everything else (including states this list has never seen) falls through
# `_aggregate_checks`'s final "anything unrecognised is PENDING, not
# SUCCESS" rule -- so a new conclusion GitHub introduces later excludes by
# default rather than silently admitting.
_FAILING_CHECK_STATES = frozenset({
    "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED",
    "STARTUP_FAILURE", "STALE",
})
_PASSING_CHECK_STATES = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


class GhClient:
    """Production `open_prs` adapter. Runner-injected so nothing here shells
    out during a test; `runner` defaults to `subprocess.run` for real use."""

    def __init__(self, runner=subprocess.run):
        self._runner = runner

    def _run(self, argv: list[str], *, scope: str | None = None) -> str | None:
        env = dict(_RUN_ENV)
        if scope is not None:
            env["BORIS_PR_SCOPE"] = scope
        try:
            proc = self._runner(argv, capture_output=True, text=True,
                                timeout=TIMEOUT, env=env)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    def _search(self, org: str, author: str) -> list[dict] | None:
        out = self._run(["gh", *_SEARCH_ARGS, f"--owner={org}",
                         f"--author={author}"])
        if out is None or not out.strip():
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def _view(self, owner: str, repo: str, number: int) -> dict | None:
        out = self._run(
            [GHX_RO, "pr", "view", str(number), "-R", f"{owner}/{repo}",
             "--json", "headRefOid,mergeStateStatus"],
            scope=f"{owner}/{repo}#{number}",
        )
        if out is None or not out.strip():
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _checks(self, owner: str, repo: str, number: int) -> list | None:
        out = self._run(
            [GHX_RO, "pr", "checks", str(number), "-R", f"{owner}/{repo}",
             "--json", "name,state"],
            scope=f"{owner}/{repo}#{number}",
        )
        if out is None:
            return None
        if not out.strip():
            # `gh pr checks` exits non-zero with empty stdout when a PR has
            # no checks configured at all -- `_run` already filtered out
            # every OTHER non-zero exit, so an empty-but-successful read
            # here specifically means "no checks", not "unreadable".
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def open_prs(self, org: str, author: str) -> list[dict] | None:
        hits = self._search(org, author)
        if hits is None:
            return None
        out = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            record = _record_from_search_hit(hit, org)
            if record is None:
                continue
            detail = self._view(record["owner"], record["repo"], record["number"])
            if not isinstance(detail, dict):
                # This one PR's detail read failed -- skip only this PR, not
                # the whole org's batch (a transient per-PR hiccup must not
                # hide every other PR discovered in the same pass).
                continue
            head_sha = detail.get("headRefOid")
            if not isinstance(head_sha, str) or not head_sha:
                continue
            record["head_sha"] = head_sha
            merge_state = detail.get("mergeStateStatus")
            record["merge_state"] = merge_state if isinstance(merge_state, str) else None
            record["checks_state"] = _aggregate_checks(
                self._checks(record["owner"], record["repo"], record["number"]))
            out.append(record)
        return out


def _record_from_search_hit(hit: dict, org: str) -> dict | None:
    """One `gh search prs --json ...` result -> the partial raw record
    (everything the search API can answer; `open_prs` fills in `head_sha`,
    `merge_state` and `checks_state` from the per-PR detail read), or `None`
    if the hit cannot be trusted to name a real PR. Fails this one hit
    closed rather than raising: a malformed entry in a batch response must
    not take the rest of the batch down with it, the same discipline
    `_evaluate` itself uses one level up."""
    repository = hit.get("repository")
    if not isinstance(repository, dict):
        return None
    full = repository.get("nameWithOwner")
    if not isinstance(full, str) or "/" not in full:
        return None
    owner, _, repo = full.partition("/")
    if owner != org or not repo:
        return None
    number = hit.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    author_field = hit.get("author")
    author_login = (author_field or {}).get("login") if isinstance(
        author_field, dict) else None

    labels_raw = hit.get("labels")
    labels: list[str] = []
    if isinstance(labels_raw, list):
        for entry in labels_raw:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str):
                    labels.append(name)
            elif isinstance(entry, str):
                labels.append(entry)

    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "state": str(hit.get("state") or "").upper() or None,
        "draft": hit.get("isDraft") if isinstance(hit.get("isDraft"), bool) else None,
        "author": author_login,
        "labels": labels,
    }


def _aggregate_checks(checks: list | None) -> str | None:
    """`gh pr checks --json name,state` rows -> one of "SUCCESS"/"FAILURE"/
    "PENDING", or `None` when the read itself failed.

    Fails toward exclusion, never toward "SUCCESS": an unreadable read
    (`None` in), a malformed row, or an EMPTY list (no checks configured at
    all) are all treated as "not a pass" by `_evaluate`, which only admits
    an exact "SUCCESS" -- an unconfigured or unreadable CI signal is not
    evidence of a clean build.
    """
    if checks is None:
        return None
    if not checks:
        return None
    states = []
    for item in checks:
        if not isinstance(item, dict):
            return None
        state = item.get("state")
        if not isinstance(state, str) or not state:
            return None
        states.append(state.upper())
    if any(s in _FAILING_CHECK_STATES for s in states):
        return "FAILURE"
    if any(s not in _PASSING_CHECK_STATES for s in states):
        return "PENDING"
    return "SUCCESS"
