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

Fail-closed, but never silently. `candidates()` returns a `DiscoveryResult`,
not a bare list, precisely because "nothing qualified" and "we could not
look" are different facts a caller must be able to tell apart: an
enumeration that fails for every org would otherwise read as a healthy,
quiet tick forever. See `DiscoveryResult` for the three ways a result can be
incomplete and why each is reported per-org rather than collapsed into one
bit.

Readiness filter -- a PR is only admitted when ALL of these hold (see
`_evaluate`): state is OPEN, not draft, author is the configured `author`,
the record's owner matches the org it was fetched under (the allowlist is
enforced by only ever asking `gh` about `orgs`, then checked again here in
case a search response ever returns something it was not asked for), the
repo is neither in `banned_repos` nor named by `banned_repo_suffixes`,
neither the hold label nor the loop's ownership label is present, and CI
checks concluded successfully. A PR whose checks are still pending is
EXCLUDED, same as one with failing checks -- see `_evaluate`'s comment on
`checks_state` for why "not failing" is read as "concluded successfully",
not "not (yet) failing".
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Every GitHub read this module makes goes through this command-allowlisted
# wrapper -- `search prs`, `pr view` and `pr checks` scoped to one org via
# `DISCOVERY_ORG_SCOPE`, everything else refused with exit 3. There is no bare
# `gh` invocation left in this file, and a test pins argv[0] of all three
# calls.
#
# What that is worth, stated exactly, because it is easy to overclaim: this
# cron is `no_agent: true` deterministic Python whose argv are the constants
# below, so there is no model here that could be induced into a write, and no
# attacker is stopped who can already run commands as this user. The wrapper
# stops a FUTURE EDIT to this file from adding a write call silently. That is
# the whole claim.
GHX_DISCOVERY = "/Users/ctao/.hermes/scripts/pr/ghx_discovery.sh"
HOME = "/Users/ctao"
# The read-only machine credential, 0600 on disk -- the same file `ghx_ro.sh`
# defaults to, and the file `ghx_discovery.sh` reads once `_run` points it here
# via `DISCOVERY_GH_TOKEN_PATH`. Discovery only ever reads, so it deliberately
# does NOT reach for `pr_remediation`'s `.gh_write_token`: all three of this
# adapter's reads run on this one credential.
DEFAULT_TOKEN_PATH = Path("/Users/ctao/.hermes/state/.gh_token")
# `subprocess.run(env=...)` REPLACES the environment rather than extending it,
# so anything the wrapper and `gh` need has to be named here explicitly. `HOME`
# is not optional decoration: without it `gh` writes
# `.local/state/gh/device-id` into whatever directory the cron happens to be
# running in (fixed for the sibling adapter in `pr_remediation/github_io.py` by
# commit 203c17b0a6, and pinned by a test there and here so it cannot regress a
# third time). The org scope and the credential's PATH are added per-call in
# `_run`; the credential's VALUE appears in neither this dict nor argv, because
# the wrapper reads it from the 0600 file itself.
_RUN_ENV = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            "HOME": HOME}
TIMEOUT = 30

# Fixed identity constants, duplicated from `pr_remediation.config.RemediationConfig`
# (`owner_label`, `hold_label`) rather than imported: `pr_remediation` is out of
# this task's scope fence (owned by a concurrent agent), and these two label
# names are the one thing both services must agree on byte-for-byte, so pinning
# them as plain module constants here -- exactly as `RemediationConfig` pins them
# -- means neither side can drift from the other by editing a shared import.
HOLD_LABEL = "hermes-hold"
OWNER_LABEL = "hermes-owner-andrej"

# Repository-name suffixes that are banned outright, mirroring
# `pr_remediation/lanes.py`'s `_EXCLUDED_REPO_SUFFIXES` byte-for-byte. Without
# it, discovery's ban is strictly narrower than the lane dispatcher's, so it
# would post PRs the loop then refuses -- a wasted Boris review and a stranded
# verdict, which is exactly the failure this service exists to remove.
#
# Deliberately a literal rather than an import of `lanes._EXCLUDED_REPO_SUFFIXES`:
# this package also runs from its deployed copy under `~/.hermes/scripts/`,
# where `pr_remediation` is not importable. `tests/cron/pr_discovery/test_discover.py`
# imports `lanes.py`'s real constant AND its `_excluded_repos()` and asserts
# both implementations agree case-by-case, so neither side can drift away from
# the other unnoticed -- the same test-side-import / runtime-side-literal
# arrangement `lanes.MAX_BRIEF_CHARS` uses against `_CTX_MAX_BODY_BYTES`.
#
# Matching is exact and case-SENSITIVE, here and in `lanes.py` alike:
# `Ylopo/YLOPO-INFRA` bypasses both bans. That is recorded, asserted parity
# (see the drift-guard test), not an accident of this implementation.
BANNED_REPO_SUFFIXES = ("-infra", "-infrastructure")


@dataclass(frozen=True)
class Candidate:
    """One pull request that has cleared every readiness filter clause.

    `merge_state` and `checks_state` ride along for the poster (a later
    task) to report -- they are not re-checked by anything that consumes a
    `Candidate`. In particular `merge_state` never gates admission: a
    `DIRTY` (conflicted) PR is still admitted, because "mergeable" is not
    one of the readiness clauses the brief specifies and inventing it here
    would silently narrow the filter past its spec.

    `has_hold` is structurally always `False` on an admitted candidate (a
    held PR is excluded by `_evaluate` before a `Candidate` is ever built).
    It is spec-mandated and kept deliberately: carried as a field rather
    than left implicit, a poster inspecting a `Candidate` never has to
    re-derive "were we admitted" from other fields, and a future filter that
    ever admits a held PR has an existing place to say so.
    """

    owner: str
    repo: str
    number: int
    head_sha: str
    merge_state: str
    checks_state: str
    has_hold: bool


@dataclass(frozen=True)
class Listing:
    """One org's enumerated pull requests, plus whether that enumeration was
    complete.

    `open_prs` hands this back rather than a bare list because a listing that
    is exactly `SEARCH_LIMIT` long is indistinguishable from a truncated one
    from the outside, and truncation quietly drops real PRs. The flag rides
    with the records so a caller cannot consume the records without the
    provenance being right there next to them.
    """

    records: tuple[dict, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class DiscoveryResult:
    """What one discovery pass found, and every way it might be incomplete.

    Returned instead of a bare `list[Candidate]` for one reason: an empty
    list is otherwise the answer to BOTH "nothing qualified" and "we could
    not look" -- and the second one, left unreported, is a permanent silent
    outage that reads as a healthy quiet tick. Three separate incompleteness
    channels, each per-org where an org is implicated:

    * `failed_orgs`  -- `gh.open_prs` could not read that org's listing at
      all (credential, network, malformed response). Other orgs in the same
      pass still produce candidates; the failure is recorded, not raised and
      not swallowed.
    * `truncated_orgs` -- the listing came back at the search limit, so an
      unknown number of that org's PRs were never seen. The candidates that
      WERE found are still real and still usable.
    * `config_errors` -- a parameter this function was called with cannot be
      trusted (see `candidates()`). Nothing is enumerated at all in that
      case: an unreadable ban list or org list means we do not know what we
      are allowed to post, and "assume the worst" is the same discipline
      `pr_remediation/github_io.py::has_hold` applies to an unreadable label
      response.

    Deliberately NOT iterable and NOT a list subclass. A caller that treats
    a result as the candidate list (`for c in candidates(...)`) fails loudly
    with a `TypeError` rather than quietly iterating something that might be
    empty because discovery was broken. `complete` is the one-line "was this
    the whole truth" a reporter can lead with.
    """

    candidates: tuple[Candidate, ...] = ()
    failed_orgs: tuple[str, ...] = ()
    truncated_orgs: tuple[str, ...] = ()
    config_errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True iff every configured org was enumerated in full."""
        return not (self.failed_orgs or self.truncated_orgs
                    or self.config_errors)


def candidates(gh, *, orgs, author, banned_repos,
               banned_repo_suffixes=BANNED_REPO_SUFFIXES) -> DiscoveryResult:
    """Chad's open pull requests across `orgs` that pass the readiness filter.

    `gh` is a dependency implementing `open_prs(org, author) -> Listing |
    None` (see `GhClient` below for the production adapter, and
    `tests/cron/pr_discovery/test_discover.py`'s `_gh()` fixture for the
    fake shape tests exercise). `None` -- or any other unrecognised shape --
    means "this org's listing could not be read": it is recorded in
    `DiscoveryResult.failed_orgs` and skipped, never raised, because a
    transient failure against one org must not stop the other orgs from
    being discovered. It is equally never allowed to surface as a plain
    empty list, which a caller cannot tell apart from a genuinely quiet org.

    `banned_repos` is an exact-match collection of `owner/name` strings and
    `banned_repo_suffixes` a tuple of repo-name suffixes; together they
    reproduce `pr_remediation/lanes.py::_repo_is_excluded`'s full signature,
    so discovery never posts a PR the lane dispatcher would then refuse.
    Pass `banned_repo_suffixes=()` to disable the suffix half deliberately.

    TRAP FOR THE WIRING TASK: do not write
    `banned_repos=os.environ.get("PR_REVIEW_EXCLUDED_REPOS")`. That variable
    is a SPACE-SEPARATED STRING, so `in` against it is SUBSTRING matching,
    not membership: `Ylopo/ylopo-inf` (a repo that does not exist) would be
    "banned" because it is a substring of `Ylopo/ylopo-infra`, and a repo
    whose name merely appears inside another entry would be banned by
    accident. Unset, the same expression is `None`, which the shipped
    version raised `TypeError` on -- one whole tick lost to a config
    default. Both shapes are rejected below as config errors rather than
    being quietly obeyed; split the string yourself, exactly as
    `lanes._excluded_repos()` does.
    """
    config_errors: list[str] = []
    org_list = _string_seq(orgs)
    if org_list is None:
        config_errors.append(
            f"orgs is not a sequence of org names: {type(orgs).__name__}")
    elif not org_list:
        # An empty org list enumerates nothing, which would otherwise be
        # reported as a healthy pass that simply found no PRs -- the exact
        # ambiguity this return type exists to remove.
        config_errors.append("orgs is empty, so nothing was enumerated")
    if not isinstance(author, str) or not author:
        config_errors.append("author is not a non-empty string")
    banned_list = _string_seq(banned_repos)
    if banned_list is None:
        config_errors.append(
            "banned_repos is not a collection of 'owner/name' strings "
            f"(got {type(banned_repos).__name__}); a bare string would mean "
            "substring matching, and None would mean no ban at all")
    suffix_list = _string_seq(banned_repo_suffixes)
    if suffix_list is None:
        config_errors.append(
            "banned_repo_suffixes is not a sequence of suffix strings: "
            f"{type(banned_repo_suffixes).__name__}")
    if config_errors:
        # Fail the whole pass closed: with an untrustworthy ban list or org
        # list we cannot know what we are allowed to post, so we post
        # nothing -- and say why, loudly enough for a reporter to surface.
        return DiscoveryResult(config_errors=tuple(config_errors))

    banned = frozenset(banned_list)
    suffixes = tuple(suffix_list)
    found: list[Candidate] = []
    failed: list[str] = []
    truncated: list[str] = []
    for org in org_list:
        listing = gh.open_prs(org, author)
        if not isinstance(listing, Listing) or not isinstance(
                listing.records, (list, tuple)):
            # None (the documented failure answer) or any other shape a
            # non-conforming `gh` implementation might hand back -- both fail
            # this one org closed, and both are REPORTED rather than being
            # folded into "this org had no PRs".
            failed.append(org)
            continue
        if listing.truncated:
            truncated.append(org)
        for raw in listing.records:
            candidate = _evaluate(raw, org=org, author=author,
                                   banned_repos=banned,
                                   banned_repo_suffixes=suffixes)
            if candidate is not None:
                found.append(candidate)
    return DiscoveryResult(candidates=tuple(found), failed_orgs=tuple(failed),
                           truncated_orgs=tuple(truncated))


def _string_seq(value) -> tuple[str, ...] | None:
    """`value` as a tuple of non-empty strings, or `None` if it is not
    recognisably a sequence of them.

    A `str` (or `bytes`) is rejected rather than iterated: a string here is
    always a caller mistake, and iterating one silently degrades an exact
    membership test into a character-by-character or substring match. `None`
    is likewise rejected rather than defaulted -- see the trap in
    `candidates()`'s docstring for the two ways this exact mistake is
    waiting to be made by whoever wires this up.
    """
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Iterable):
        return None
    try:
        items = list(value)
    except Exception:
        return None
    if any(not isinstance(item, str) or not item for item in items):
        return None
    return tuple(items)


def _evaluate(raw: object, *, org: str, author: str,
              banned_repos, banned_repo_suffixes=BANNED_REPO_SUFFIXES) -> Candidate | None:
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
    # The FULL signature of `pr_remediation/lanes.py`'s `_repo_is_excluded`:
    # exact, case-sensitive membership OR a banned name suffix. Both halves
    # are needed for the two independent enforcement points to agree --
    # exact match alone admits `Ylopo/platform-infra` and
    # `Ylopo/billing-infrastructure`, which the lane dispatcher then refuses,
    # stranding the verdict a Boris review already paid for.
    full = f"{owner}/{repo}"
    if full in banned_repos or full.endswith(banned_repo_suffixes):
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
    # These two labels are the kill switches: `hermes-hold` is a human
    # standing the automation down, `hermes-owner-andrej` says the
    # remediation loop already owns this PR and a discovery post would put
    # two agents on one branch. So an unreadable label list is NOT allowed to
    # normalise into "no labels" -- anything but a list of strings excludes
    # the record, matching `github_io.has_hold`'s "an unknown state is a
    # hold" rather than inverting it.
    labels = raw.get("labels")
    if not isinstance(labels, list):
        return None
    if not all(isinstance(name, str) for name in labels):
        return None
    # Matching is exact and case-sensitive on purpose (`Hermes-Hold` does NOT
    # hold): both `github_io.has_hold` and `lanes.py` compare label names
    # exactly, and a discovery filter that matched more loosely than the
    # component enforcing the label would disagree with it about which PRs
    # are held. Parity, not oversight.
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
# `candidates()` above depends only on `open_prs(org, author) -> Listing |
# None` -- it does not care how that listing is produced. `GhClient` is the
# concrete, network-touching implementation the live discovery cron wires in;
# every test in this package's suite uses a fake instead (see
# `test_discover.py`), so nothing here is on that suite's hot path.
#
# Why this is a two-step read. `gh search prs --json` is narrower than it
# looks: GitHub's search API backs it, and that API does not expose
# `headRefOid`, `mergeStateStatus` or CI conclusions at all -- only `pr view`
# and `pr checks` (single-PR GraphQL/REST calls) do. So enumeration and detail
# are genuinely two different calls: `search prs` gives the PR numbers in an
# org, then one `pr view` and one `pr checks` per number fill in the rest.
#
# Why all three go through `ghx_discovery.sh` and not `ghx_ro.sh`. The latter
# pins every read to a single already-known pull request
# (`BORIS_PR_SCOPE=owner/repo#number`, `pr` verb limited to
# `view|diff|checks|status`, each require-scoped to that PR). That is exactly
# the property a Boris review lane needs, and exactly the property that makes
# enumeration impossible to express -- it has no `search` verb and could not
# usefully gain one without widening the review-lane boundary for a different
# consumer's benefit. `ghx_discovery.sh` is its sibling, scoped to an org
# instead of a PR.
#
# The cost of that, stated rather than buried: the two detail reads used to
# inherit `ghx_ro.sh`'s per-PR pin and now carry a per-org one. Nothing that
# was doing work is lost -- this module chooses those PR numbers itself, out
# of an enumeration already scoped to the same org, so the tighter pin was
# restating a decision made a few lines earlier in this same file. What it
# buys is one wrapper to audit for the whole module, and no bare `gh` left in
# it to grow a write call.
#
# `gh search prs` defaults to `--limit 30`. Chad runs 22-23 open PRs in scope
# today, so the default is already close enough to bite, and it bites
# SILENTLY: a capped listing looks exactly like a short one. 200 matches the
# cron this service retires (`pr_review_gather.py:259`), and
# `open_prs` reports a listing that comes back AT the limit as truncated
# rather than pretending it saw everything.
SEARCH_LIMIT = 200

# Every field named here is load-bearing for the readiness filter, and losing
# one is not equally harmful in each direction: drop `state`, `isDraft` or
# `author` and the filter fails CLOSED (the record is excluded), but drop
# `labels` and it fails OPEN -- both kill switches would be evaluated against
# a record that simply has no labels. `test_discover.py` pins this list field
# by field for that reason.
_SEARCH_ARGS = [
    "search", "prs", "--state=open", f"--limit={SEARCH_LIMIT}", "--json",
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
    """Production `open_prs` adapter. Runner- and token-path-injected so
    nothing here shells out or reads the real credential during a test;
    `runner` defaults to `subprocess.run` and `token_path` to the read-only
    machine token for real use."""

    def __init__(self, runner=subprocess.run, token_path=DEFAULT_TOKEN_PATH):
        self._runner = runner
        self._token_path = Path(token_path)

    def _token(self) -> str | None:
        """The read-only GitHub credential, or `None` if it cannot be used.

        Read per call, not cached at construction, so a rotated token is
        picked up by the next tick without a restart. Any failure (missing
        file, permissions, non-UTF-8 bytes) and an empty-after-strip file are
        both `None`: `_run` then refuses to spawn the wrapper at all, exactly
        as `pr_remediation/github_io.py::_run` does, rather than making the
        fail-closed guarantee depend on how `gh` reacts to an empty
        `GH_TOKEN`. The wrapper checks the same file again for itself, so the
        guarantee does not depend on this adapter being the only caller.
        """
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        return token or None

    def _run(self, argv: list[str], *, org: str) -> str | None:
        """One `ghx_discovery.sh` call's stdout, or `None` if it cannot be
        trusted to have answered.

        Two things ride in the environment, and neither is optional. The org
        scope is what the wrapper checks `--owner=`/`-R` against; without it
        the wrapper refuses every verb, so a missing scope is a dead tick, not
        a wide one. The credential travels as a PATH, never as a value: the
        wrapper reads that 0600 file itself and exports `GH_TOKEN` immediately
        before `exec gh`, so a `GH_TOKEN` set here would be overwritten by it
        anyway. Passing the path (rather than relying on the wrapper's own
        default) keeps a test's or a deployment's token override applying to
        all three reads instead of silently to none.

        The credential is still read locally first: a missing, unreadable or
        empty token file refuses to spawn anything at all, exactly as
        `pr_remediation/github_io.py::_run` does. The wrapper refuses that same
        case a second time (exit 1) rather than letting `gh` run
        unauthenticated -- where a search would answer an empty result set
        indistinguishable from "this org has no open PRs". Both refusals are
        tested; neither depends on the other.
        """
        if self._token() is None:
            return None
        env = dict(_RUN_ENV)
        env["DISCOVERY_ORG_SCOPE"] = org
        env["DISCOVERY_GH_TOKEN_PATH"] = str(self._token_path)
        try:
            proc = self._runner(argv, capture_output=True, text=True,
                                timeout=TIMEOUT, env=env)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    def _search(self, org: str, author: str) -> list[dict] | None:
        # `--owner=` is load-bearing twice over: it is the qualifier that
        # keeps the search inside this org, AND it is what the wrapper
        # compares `DISCOVERY_ORG_SCOPE` against -- a search that lost it is
        # refused rather than quietly widened to all of GitHub.
        out = self._run([GHX_DISCOVERY, *_SEARCH_ARGS, f"--owner={org}",
                         f"--author={author}"], org=org)
        if out is None or not out.strip():
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def _view(self, owner: str, repo: str, number: int) -> dict | None:
        out = self._run(
            [GHX_DISCOVERY, "pr", "view", str(number), "-R", f"{owner}/{repo}",
             "--json", "headRefOid,mergeStateStatus"],
            org=owner,
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
            [GHX_DISCOVERY, "pr", "checks", str(number), "-R",
             f"{owner}/{repo}", "--json", "name,state"],
            org=owner,
        )
        if out is None or not out.strip():
            # `gh pr checks` exits 8 while checks are pending, 1 when any
            # check failed, and 1 again when the PR has no checks at all --
            # every one of those is non-zero, so `_run` has ALREADY turned
            # them into None above and nothing here ever sees them. (An
            # earlier comment claimed the opposite and returned `[]` for the
            # no-checks case; that branch was unreachable in production and
            # only ever exercised by a `_Proc(0, "")` `gh` never emits.)
            # A zero-exit-but-empty read is a shape `gh` is not known to
            # produce, and is treated as unreadable rather than as a
            # verified-empty check list -- `_aggregate_checks` must never be
            # handed something that could be mistaken for "checks ran and
            # passed". Both cases answer None, which excludes.
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def open_prs(self, org: str, author: str) -> Listing | None:
        hits = self._search(org, author)
        if hits is None:
            return None
        # A listing that comes back at exactly the limit we asked for cannot
        # be told apart from one GitHub truncated, so it is reported as
        # truncated. Over-reporting on the exact boundary is the safe
        # direction: the alternative is silently dropping PRs.
        truncated = len(hits) >= SEARCH_LIMIT
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
        return Listing(records=tuple(out), truncated=truncated)


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

    # Labels decide both kill switches, so ONLY the shape `gh search prs
    # --json labels` actually returns -- a list of objects each carrying a
    # non-empty string `name` -- is recognised. Every other shape (key
    # missing, `null`, a dict, a list of scalars, an entry whose `name` is
    # missing or not a string) rejects the whole hit rather than normalising
    # to `[]`, because `[]` means "we read the labels and there were none",
    # which is a positive claim this function would have no basis to make.
    # Identical discipline to `github_io.has_hold`, which answers "assume
    # held" for exactly this set of shapes; the version this replaced
    # inverted it, and one deletion in `_SEARCH_ARGS` above would have been
    # enough to disable `hermes-hold` and `hermes-owner-andrej` entirely.
    labels_raw = hit.get("labels")
    if not isinstance(labels_raw, list):
        return None
    labels: list[str] = []
    for entry in labels_raw:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            return None
        labels.append(name)

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

    Note which arm actually runs in production. `gh pr checks` exits 8 while
    checks are pending and 1 when any check failed, and `_run` turns every
    non-zero exit into `None` before a single row is parsed -- so the `None`
    arm below, not the FAILURE/PENDING taxonomy, is what excludes red and
    pending CI on the live path. The taxonomy still earns its place: it is
    the only thing that distinguishes those cases for a reporter, and it is
    what runs when a caller supplies rows from anywhere other than this
    adapter's own `_checks`.
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
