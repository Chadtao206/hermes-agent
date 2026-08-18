"""Resolve authoritative PR facts. Head SHA never comes from Slack.

Every read here answers in three states, never two: found, authoritatively
absent, or unreadable. See `models.GitHubReadStatus` for the incident that
bought that distinction and for why the unclassifiable default is "unreadable".
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boris_intake.models import (
    GateSnapshot,
    GateSnapshotLookup,
    GitHubReadStatus,
    LifecycleRequest,
    PRInfo,
    PRLookup,
    PublishedReview,
    SnapshotStatus,
)
from boris_intake.result import ValidationError, build_gate_snapshot, load_gate_registry

GHX_RO = "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_GATE_REGISTRY = Path(__file__).with_name("gate_registry.json")
_RUN_ENV = {
    "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
}
GH = "/opt/homebrew/bin/gh"
#: Per-call ceiling, unchanged: it is the worst case every caller already
#: budgets for, and the retry arithmetic below is stated in terms of it.
TIMEOUT = 30

# --- bounded retry for transient server-side faults ------------------------
#
# Written against the same 2026-08-17 outage as
# `pr_discovery/discover.py:840-988`, whose comments argue the shape in full.
# The shape is reimplemented rather than imported: the two packages deploy
# separately and neither may take a dependency on the other. The CONSTANTS do
# not transfer, and the two differences are the whole reason:
#
# (a) The window is per MESSAGE, not per call. `pr_discovery` opens its deadline
#     on a per-tick adapter. Here one Slack message can carry MORE refs than
#     `Limits.max_prs_per_message` (deployed: 40) -- that cap is NOT a bound on
#     the reads. `grammar.extract_pr_refs` applies no cap at all, and
#     `gate.evaluate` applies the burst cap only AFTER each ref's `fetch_pr`
#     (`gate.py`: the read, then the cap), so the real bound is the number of
#     distinct Ylopo pull-request URLs one Slack message can hold -- of the order
#     of 10**3 at Slack's 40k-character limit (~800 for realistic repo names).
#     Each costs one `fetch_pr`, plus two more calls for every admitted command.
#     That does not break the arithmetic below, and sharing the window is
#     precisely why: the budget object is threaded from `daemon.Pipeline.handle`
#     through both the gate's `pr_info` and `capture_gate_snapshot`, so a
#     thousand refs still add one window's worth of retry, not a thousand.
#
# (b) The budget is far tighter, because this blocks a live event loop.
#     `Pipeline.handle` is called synchronously from `daemon.on_request` inside
#     the asyncio loop, so time spent retrying delays every other message in the
#     channel and stalls the heartbeat write. `pr_discovery` can afford 240s on a
#     30-minute cron; this cannot.
#
# THE ARITHMETIC, and the ceiling it fits under. The channel's stated
# reaction-time expectation is ~60s, so that is what the retry may add to a
# message -- not what a message may cost, which is dominated by the un-retried
# pass and was true before this retry existed. Retries only START inside the
# window, so the added time is at most the window itself, plus one straggler
# attempt that began just inside it, plus the backoff before it:
#
#     RETRY_DEADLINE + TIMEOUT + BACKOFF_CAP = 20 + 30 + 4 = 54s < 60s
#
# and that number is per MESSAGE however many pull requests it names, which is
# the property (a) buys: a 40-ref message with a per-call window would add
# 40 * 54 = 2160s = 36 minutes of retry to one handler call, and nothing stops a
# message naming twenty times that many.
#
# Realistically nothing is near that. The common message names one pull request
# and costs 3 calls (one `pr view` in the gate, then `pr view` + two
# `pr checks`); at the measured ~2.7s per call with every one of them faulting
# once, that is 6 * 2.7 + 3 * 1.0 = ~19s, and with no faults ~8s.
#
# `test_the_retry_budget_fits_inside_the_channel_reaction_window` pins the
# inequality, so raising any of these past what the handler can absorb breaks a
# test instead of quietly slowing the channel down.
PR_ATTEMPTS = 3
SNAPSHOT_ATTEMPTS = 3
RETRY_DEADLINE = 20.0
# Short on purpose, as in `pr_discovery`: the 503s recover in about a second, so
# a long backoff would spend the window sleeping instead of retrying. Three
# attempts therefore sleep 1+2 = 3s in the worst case.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 4.0

#: GitHub's own not-found shape, and the ONLY positive evidence that a pull
#: request does not exist. Observed 2026-08-17 through `ghx_ro.sh` (which ends
#: in `exec gh "$@"`, so `gh`'s exit code and stderr pass through untouched),
#: probed against a real repository:
#:
#:   $ ghx_ro.sh pr view 999999 -R Ylopo/forge-content-service --json ...
#:   exit 1, stderr: GraphQL: Could not resolve to a PullRequest with the
#:                   number of 999999. (repository.pullRequest)
#:
#:   $ ghx_ro.sh pr view 4 -R Ylopo/no-such-repo-xyz --json ...
#:   exit 1, stderr: GraphQL: Could not resolve to a Repository with the name
#:                   'Ylopo/no-such-repo-xyz'. (repository)
#:
#: For contrast, a real one (Ylopo/forge-content-service#111) answered exit 0
#: with the JSON body, and a merged one (#1) also exit 0 -- so a CLOSED or
#: MERGED pull request is never this shape, and `PR_NOT_OPEN` keeps owning it.
#:
#: The exit code is deliberately NOT the signal: `ghx_ro.sh` exits 1 itself when
#: it cannot read the token file, which is a wiring bug and must not read as
#: "no such pull request". The unresolvable-repository shape counts because the
#: pull request cannot exist if its repository does not, and because the same
#: answer is what GitHub gives for a repository this credential cannot see --
#: which `pr_not_found`'s reply text has always covered ("or I can't see it").
_NOT_FOUND_SHAPES = (
    "could not resolve to a pullrequest",
    "could not resolve to a repository",
)
#: Anything the wrapper itself says. `refuse()` exits 3; the unreadable-token
#: path exits 1. Both are local wiring faults, so both are unavailable-but-
#: pointless-to-retry.
_WRAPPER_PREFIX = "ghx_ro:"

#: GitHub's own "this branch has no checks" shape, and the ONLY positive
#: evidence that a pull request has no CI to snapshot. Probed 2026-08-18 through
#: `ghx_ro.sh` (gh 2.95.0) against real pull requests, not remembered:
#:
#:   $ BORIS_PR_SCOPE=Ylopo/ylopo-infra#1 ghx_ro.sh pr checks 1 \
#:         -R Ylopo/ylopo-infra --json name,state,link
#:   exit 1, stdout "", stderr: no checks reported on the
#:                              'DEVOPS-163-vail-pylib' branch
#:
#:   $ ... -R Ylopo/maverickre-monorepo 1 ...
#:   exit 1, stdout "", stderr: no checks reported on the
#:                              'RET-198-eslint-first-pass-auto-fixes' branch
#:
#: For contrast, `Ylopo/Gary-Ashton#1` (11 checks) answered exit 0 with the JSON
#: array, and its `--required` read answered exit 1 with `no required checks
#: reported on the 'feat/homepage-rebuild' branch` -- which is where
#: `empty_stderr_prefix` below comes from. Note the two wordings are different
#: strings and neither is a prefix of the other: `--required` on a zero-check
#: pull request emits the PLAIN wording, so that variant's allowance can only
#: fire when checks exist and none of them are required.
#:
#: Matched positively, the same way ABSENT is: a non-zero exit AND empty stdout
#: AND this exact prefix, case-insensitively -- the same allowance
#: `_NOT_FOUND_SHAPES` gets, for the same reason: a `gh` reword of casing alone
#: (`No checks reported...`) must not silently fall through to the
#: retryable-and-un-throttled branch this replaced. Bare empty stdout is NOT
#: this case -- that is the fail-closed guard an empty rollup depends on, and
#: widening it to plain `not stdout` would admit a pull request whose required
#: checks were failing.
#:
#: Deliberately not FOUND, and deliberately not GITHUB_UNAVAILABLE. See
#: `models.SnapshotStatus.NO_CHECKS` for both halves of that decision.
_NO_CHECKS_PREFIX = "no checks reported on the "


class GitHubRetryBudget:
    """One Slack message's shared retry window.

    `sleep` and `clock` are injected for the same reason `GhClient` injects
    them: the retry is time-dependent, and a suite that really slept through its
    backoffs (or really waited out `RETRY_DEADLINE`) is a suite people stop
    running. Every test drives both by hand.

    One instance per message, which is what makes the deadline meaningful: it is
    opened by the first read that uses it, so it bounds one MESSAGE rather than
    one call.
    """

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self._sleep = sleep
        self._clock = clock
        # Opened lazily rather than in the constructor: the pipeline builds this
        # before the gate's cheap local checks have even run, and a window that
        # started ticking during wiring would hand the reads a shorter one than
        # the arithmetic above assumes.
        self._deadline: float | None = None

    def open_window(self) -> None:
        if self._deadline is None:
            self._deadline = self._clock() + RETRY_DEADLINE

    def window_closed(self) -> bool:
        """Has this message's window closed?

        Checked before a retry is STARTED, never before a first attempt: a
        closed window must stop retrying, not stop reading -- refusing first
        attempts would turn a slow message into a message whose pull requests
        were never looked at, reported as if GitHub had failed on each of them.
        """
        return self._deadline is not None and self._clock() >= self._deadline

    def pause(self, attempt: int) -> None:
        self._sleep(min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_CAP))


@dataclass(frozen=True)
class _Read:
    """One classified wrapper call. `value` is the parsed JSON when FOUND.

    `retryable` is the second axis of A.1's classification, and this private type
    is the ONLY place it lives: `_read_json` is its one consumer, and it is the
    one place a retry can still be started. The public lookups deliberately do
    not restate it -- see `models.PRLookup`.
    """

    status: GitHubReadStatus
    value: Any = None
    detail: str = ""
    retryable: bool = False


def _attempt(
    command: list[str],
    env: dict,
    runner,
    allowed_codes: tuple[int, ...],
    empty_stderr_prefix: str | None,
    no_checks_prefix: str | None = None,
) -> _Read:
    """One actual spawn, classified.

    Order matters. The not-found shape is matched on stderr BEFORE the exit code
    is looked at, because the exit code cannot carry it: `gh` and the wrapper's
    own unreadable-token refusal both exit 1. Everything that is not a positive
    not-found match and not a local wiring refusal is unavailable-and-retryable,
    including answers that arrive but cannot be trusted (not JSON, wrong shape),
    because a truncated body is a symptom of the same faults.

    EVERY `detail` this returns is a fixed literal, and that is a boundary, not a
    style: `_Read.detail` reaches `PRLookup.detail`, then the gate's rejection,
    then `slack_io.reply_rejection`, then `chat.postMessage` -- and a
    GITHUB_UNAVAILABLE reply is delivered on every post, cooldown or not. `gh`
    stderr carries author-controlled text (`no checks reported on the
    '<branch>' branch`, and the branch name is the pull request author's), so
    interpolating stderr, or a caught exception's message, into a detail would
    put text an outsider chose into a guaranteed-delivered Slack message. That
    is the prompt-injection boundary this package's docstrings claim; see
    `gate.py`. `test_github.py` drives these functions with a hostile branch name
    and fails if any detail is not drawn from the closed vocabulary.
    """
    try:
        proc = runner(
            command, capture_output=True, text=True, timeout=TIMEOUT, env=env
        )
    except Exception:
        return _Read(
            GitHubReadStatus.UNAVAILABLE,
            detail="the GitHub call did not complete",
            retryable=True,
        )
    stderr = str(getattr(proc, "stderr", "") or "")
    if proc.returncode != 0 and any(
        shape in stderr.lower() for shape in _NOT_FOUND_SHAPES
    ):
        # A non-zero exit is required as well as the shape, even though the exit
        # code cannot identify this case on its own: `gh` never answers a body
        # AND a resolution error, so a zero exit that mentions one is a warning
        # about something else, and absent is the one classification that must
        # never be reached by accident.
        return _Read(GitHubReadStatus.ABSENT, detail="GitHub reports no such PR")
    if proc.returncode == 3 or (
        proc.returncode != 0 and stderr.lstrip().startswith(_WRAPPER_PREFIX)
    ):
        # A scope/verb refusal or an unreadable credential. Retrying either
        # cannot succeed -- it is a wiring bug, not an outage, and it will still
        # be one a second from now.
        #
        # The non-zero exit is required for the same reason the ABSENT branch
        # above requires one: a body that arrived is not a refusal. `ghx_ro.sh`
        # writes nothing to stderr on the success path today, but that wrapper
        # deploys separately from this package, so one added log line there would
        # otherwise discard every successful read and blame GitHub for it.
        detail = (
            "the GitHub wrapper refused the call"
            if proc.returncode == 3
            else "the GitHub credential could not be read"
        )
        return _Read(GitHubReadStatus.UNAVAILABLE, detail=detail)
    stdout = str(getattr(proc, "stdout", "") or "")
    if (
        no_checks_prefix
        and proc.returncode != 0
        and not stdout.strip()
        and stderr.strip().lower().startswith(no_checks_prefix)
    ):
        # GitHub answered, and the answer is "this branch has no checks". Not a
        # failure, not an empty rollup: terminal, never retried, and never
        # FOUND. Checked before the allowed-exit-code test below so tightening
        # `allowed_codes` could not turn this truthful answer back into a lie.
        # Matched case-insensitively, like `_NOT_FOUND_SHAPES` above: `_Read`
        # only ever carries fixed literals into `detail`, so lower-casing the
        # comparison cannot leak stderr, and skipping it would let a `gh`
        # reword of casing alone reinstate the pre-fix misclassification.
        return _Read(GitHubReadStatus.NO_CHECKS, detail="the PR has no CI checks")
    if proc.returncode not in allowed_codes:
        return _Read(
            GitHubReadStatus.UNAVAILABLE,
            detail="GitHub answered with an error",
            retryable=True,
        )
    if (
        empty_stderr_prefix
        and not stdout.strip()
        and stderr.strip().startswith(empty_stderr_prefix)
    ):
        # Three parts, all required. Empty stdout ALONE must never answer `[]`:
        # an allowed exit with a truncated body would then build a snapshot with
        # zero checks, `apply_gate_policy` would find nothing blocking and
        # nothing pending, and a pull request whose required checks were failing
        # would be publishable as READY_FOR_HUMAN_REVIEW. Both check reads
        # depend on this guard; see `test_github.py`.
        return _Read(GitHubReadStatus.FOUND, value=[])
    try:
        return _Read(GitHubReadStatus.FOUND, value=json.loads(stdout))
    except (TypeError, ValueError):
        return _Read(
            GitHubReadStatus.UNAVAILABLE,
            detail="GitHub's answer could not be parsed",
            retryable=True,
        )


def _read_json(
    command: list[str],
    scope: str,
    runner,
    *,
    token_path: Path | None,
    attempts: int,
    budget: GitHubRetryBudget | None,
    allowed_codes: tuple[int, ...] = (0,),
    empty_stderr_prefix: str | None = None,
    no_checks_prefix: str | None = None,
) -> _Read:
    """One wrapper read, retried up to `attempts` times inside `budget`.

    Two things ride in the environment and neither is optional: the PR scope the
    wrapper checks every argv against, and the credential, which travels as a
    PATH and never as a value (the wrapper reads that 0600 file itself).
    """
    env = {
        "BORIS_PR_SCOPE": scope,
        **({"BORIS_GH_TOKEN_PATH": str(token_path)} if token_path else {}),
        **_RUN_ENV,
    }
    if budget is not None:
        budget.open_window()
    read = _Read(GitHubReadStatus.UNAVAILABLE, detail="no attempt was made")
    for attempt in range(1, attempts + 1):
        read = _attempt(
            command,
            env,
            runner,
            allowed_codes,
            empty_stderr_prefix,
            no_checks_prefix,
        )
        # `retryable` is only ever set on an UNAVAILABLE read, so this one test
        # returns every found, every absent, every no-checks and every
        # unavailable-but-pointless-to-retry answer without a second attempt --
        # and a no-checks answer therefore costs one spawn and none of the
        # message's window.
        if not read.retryable:
            return read
        if attempt >= attempts or budget is None or budget.window_closed():
            break
        budget.pause(attempt)
    # Every attempt spent and still nothing trustworthy: answer UNAVAILABLE,
    # undiluted. This is the line that keeps the retry from re-becoming the
    # defect it was added for -- an unreadable answer must never be reported as
    # an authoritative one.
    return read


def fetch_pr(
    owner: str,
    repo: str,
    number: int,
    runner=subprocess.run,
    token_path: Path | None = None,
    budget: GitHubRetryBudget | None = None,
) -> PRLookup:
    """Look a pull request up in three states. Never raises.

    `budget` is what buys the retry: without one this makes a single attempt,
    exactly as it did before the retry existed, so a caller that has no message
    to bound the window against (`GitHubIO.current_pr`, whose reconciler retries
    with its own backoff) is not silently given an unbudgeted loop.
    """
    read = _read_json(
        [
            GHX_RO,
            "pr",
            "view",
            str(number),
            "-R",
            f"{owner}/{repo}",
            "--json",
            "headRefOid,author,state,title,headRefName",
        ],
        f"{owner}/{repo}#{number}",
        runner,
        token_path=token_path,
        attempts=PR_ATTEMPTS if budget is not None else 1,
        budget=budget,
    )
    if read.status is GitHubReadStatus.ABSENT:
        return PRLookup(GitHubReadStatus.ABSENT, detail=read.detail)
    if read.status is GitHubReadStatus.UNAVAILABLE:
        return PRLookup(GitHubReadStatus.UNAVAILABLE, detail=read.detail)
    data = read.value
    if not isinstance(data, dict):
        return PRLookup(
            GitHubReadStatus.UNAVAILABLE,
            detail="GitHub's answer was not a pull request object",
        )
    sha = str(data.get("headRefOid") or "").lower()
    if not _SHA.match(sha):
        # An answer arrived but carries no usable head SHA. Unreadable, NOT
        # absent: the head SHA is the one fact this service refuses to take from
        # anywhere else, so a body without one has told us nothing.
        return PRLookup(
            GitHubReadStatus.UNAVAILABLE,
            detail="GitHub's answer had no usable head SHA",
        )
    return PRLookup(
        GitHubReadStatus.FOUND,
        info=PRInfo(
            head_sha=sha,
            author=str((data.get("author") or {}).get("login") or ""),
            state=str(data.get("state") or "").upper(),
            title=str(data.get("title") or ""),
            head_ref=str(data.get("headRefName") or ""),
        ),
    )


def _unreadable_snapshot(read: _Read, what: str) -> GateSnapshotLookup:
    """A failed read, restated as a failed capture.

    An ABSENT read lands here too: the gate proved this pull request existed
    moments ago, so GitHub answering "no such pull request" now is a race (or a
    repository that has just become invisible), and the honest thing to report is
    still that the snapshot could not be captured. A NO_CHECKS read does NOT land
    here -- it is an answer, not a failure, and has its own state.
    """
    return GateSnapshotLookup(
        SnapshotStatus.UNAVAILABLE,
        detail=f"{what}: {read.detail}" if read.detail else what,
    )


def fetch_gate_snapshot(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    captured_at: str,
    runner=subprocess.run,
    token_path: Path | None = None,
    budget: GitHubRetryBudget | None = None,
) -> GateSnapshotLookup:
    """Capture the exact-head Gate Snapshot, in four states.

    HEAD_MOVED and NO_CHECKS are not failures and neither is ever retried: see
    `SnapshotStatus`.
    """
    scope = f"{owner}/{repo}#{number}"
    repository = f"{owner}/{repo}"
    attempts = SNAPSHOT_ATTEMPTS if budget is not None else 1
    head = _read_json(
        [GHX_RO, "pr", "view", str(number), "-R", repository, "--json", "headRefOid"],
        scope,
        runner,
        token_path=token_path,
        attempts=attempts,
        budget=budget,
    )
    if head.status is not GitHubReadStatus.FOUND or not isinstance(head.value, dict):
        return _unreadable_snapshot(head, "the pull request head could not be read")
    current_head = str(head.value.get("headRefOid") or "").lower()
    if not _SHA.fullmatch(current_head):
        return GateSnapshotLookup(
            SnapshotStatus.UNAVAILABLE,
            detail="GitHub's answer had no usable head SHA",
        )
    if current_head != expected_head_sha.lower():
        # Terminal and correct: the pull request moved under the request. A
        # retry cannot change it, and calling it an outage would both lie and
        # slow down the commonest legitimate path through here.
        return GateSnapshotLookup(
            SnapshotStatus.HEAD_MOVED,
            detail=f"the head moved to {current_head[:7]} after the request",
        )

    checks = _read_json(
        [
            GHX_RO,
            "pr",
            "checks",
            str(number),
            "-R",
            repository,
            "--json",
            "name,state,link",
        ],
        scope,
        runner,
        token_path=token_path,
        attempts=attempts,
        budget=budget,
        allowed_codes=(0, 1),
        no_checks_prefix=_NO_CHECKS_PREFIX,
    )
    if checks.status is GitHubReadStatus.NO_CHECKS:
        # GitHub answered, and there is nothing to snapshot. Terminal, and the
        # request stays rejected: a zero-check snapshot would have nothing
        # blocking and nothing pending, so a pull request whose CI never ran
        # could be published READY_FOR_HUMAN_REVIEW. Whether a CI-less repository
        # should be reviewable is Chad's decision to make deliberately.
        return GateSnapshotLookup(
            SnapshotStatus.NO_CHECKS, detail="the PR has no CI checks to capture"
        )
    if checks.status is not GitHubReadStatus.FOUND:
        return _unreadable_snapshot(checks, "the PR checks could not be read")
    required = _read_json(
        [
            GHX_RO,
            "pr",
            "checks",
            str(number),
            "-R",
            repository,
            "--required",
            "--json",
            "name,state,link",
        ],
        scope,
        runner,
        token_path=token_path,
        attempts=attempts,
        budget=budget,
        allowed_codes=(0, 1),
        empty_stderr_prefix="no required checks reported on the ",
    )
    if required.status is not GitHubReadStatus.FOUND:
        return _unreadable_snapshot(
            required, "the required PR checks could not be read"
        )
    if not isinstance(checks.value, list) or not isinstance(required.value, list):
        return GateSnapshotLookup(
            SnapshotStatus.UNAVAILABLE,
            detail="GitHub's check rollup was not a list",
        )
    if not all(isinstance(item, dict) for item in checks.value + required.value):
        return GateSnapshotLookup(
            SnapshotStatus.UNAVAILABLE,
            detail="GitHub's check rollup held a non-object row",
        )
    required_names = {str(item.get("name") or "") for item in required.value}
    all_names = {str(item.get("name") or "") for item in checks.value}
    if not required_names <= all_names:
        # A required check that the full rollup does not mention means one of the
        # two answers is incomplete. Fail closed, and say so honestly.
        return GateSnapshotLookup(
            SnapshotStatus.UNAVAILABLE,
            detail="a required check is missing from GitHub's rollup",
        )
    rows = [
        dict(item, required=str(item.get("name") or "") in required_names)
        for item in checks.value
    ]
    if not rows:
        # F2: refused STRUCTURALLY, not by relying on how `gh` happens to fail.
        # The stderr shape above is the ONLY thing that made an empty rollup
        # unreachable before this guard existed -- real `gh` errors instead of
        # printing `[]` for a zero-check pull request, but that is a
        # behavioural accident of an external tool, not a guarantee. An exit 0
        # `[]` on both checks reads is well-formed JSON, passes every guard
        # above (empty stdout was already excluded from the FOUND `[]`
        # allowance, but a genuinely empty JSON array is not empty stdout), and
        # would otherwise reach `build_gate_snapshot([])`, which succeeds: zero
        # checks means nothing blocking and nothing pending, so the pull
        # request would publish READY_FOR_HUMAN_REVIEW with CI that never ran.
        # This makes the safety property independent of an English string.
        return GateSnapshotLookup(
            SnapshotStatus.NO_CHECKS, detail="the PR has no CI checks to capture"
        )
    try:
        return GateSnapshotLookup(
            SnapshotStatus.CAPTURED,
            snapshot=build_gate_snapshot(
                rows, captured_at, load_gate_registry(_GATE_REGISTRY)
            ),
        )
    except ValidationError:
        return GateSnapshotLookup(
            SnapshotStatus.UNAVAILABLE,
            # The validation message is deliberately NOT interpolated. It is
            # composed from the rollup GitHub returned, so its wording is not
            # ours to promise, and this detail is delivered into Slack. Closed
            # vocabulary wins over a better error message on the one path where
            # the two conflict.
            detail="the check rollup did not validate",
        )


class GitHubIO:
    def __init__(self, token_path: Path, runner=subprocess.run):
        self._token_path = Path(token_path)
        self._runner = runner

    def _token(self) -> str:
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError("GitHub credential is unavailable") from error
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("GitHub credential is malformed")
        return token

    def _api(self, args: list[str], *, payload: dict | None = None) -> object:
        kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": 30,
            "env": {**_RUN_ENV, "GH_TOKEN": self._token()},
        }
        if payload is not None:
            kwargs["input"] = json.dumps(payload, separators=(",", ":"))
        try:
            process = self._runner([GH, "api", *args], **kwargs)
        except Exception as error:
            raise RuntimeError("GitHub lifecycle call failed") from error
        if process.returncode != 0:
            detail = str(process.stderr or "").strip()[:300]
            raise RuntimeError(f"GitHub lifecycle call failed: {detail}")
        try:
            return json.loads(process.stdout or "null")
        except ValueError as error:
            raise RuntimeError(
                "GitHub lifecycle call returned malformed JSON"
            ) from error

    def current_pr(self, request: LifecycleRequest) -> PRInfo | None:
        """Deliberately still two-state, and deliberately not retried here.

        `reconciler.py` calls this at four points and every one of them raises
        `RuntimeError` on `None`, which `reconciler.tick` catches and reschedules
        with a capped exponential backoff -- so `None` there already means
        "retryable", and narrowing the three-state result back to it preserves
        today's behaviour exactly. Widening those four call sites would trade a
        loop that already recovers for a much larger diff on a live lifecycle.
        No budget is passed for the same reason: the reconciler's own retry owns
        that, and an inline retry here would block its tick per request.
        """
        lookup = fetch_pr(
            request.owner,
            request.repo,
            request.pr_number,
            runner=self._runner,
            token_path=self._token_path,
        )
        return lookup.info

    def capture_gate_snapshot(
        self, request: LifecycleRequest, captured_at: str
    ) -> GateSnapshot | None:
        """Two-state for the same reason as `current_pr`.

        The reconciler distinguishes a moved head for itself, by comparing
        `current_pr` against the request's own head SHA before it ever asks for a
        snapshot, so collapsing HEAD_MOVED into `None` here loses nothing it did
        not already know.
        """
        return fetch_gate_snapshot(
            request.owner,
            request.repo,
            request.pr_number,
            request.head_sha,
            captured_at,
            runner=self._runner,
            token_path=self._token_path,
        ).snapshot

    def find_review(self, request: LifecycleRequest) -> PublishedReview | None:
        raw = self._api([
            "--paginate",
            "--slurp",
            f"repos/{request.owner}/{request.repo}/pulls/{request.pr_number}/reviews?per_page=100",
        ])
        if not isinstance(raw, list):
            raise RuntimeError("GitHub review lookup returned a non-list")
        rows = [
            item
            for page in raw
            for item in (page if isinstance(page, list) else [page])
        ]
        marker = re.compile(
            rf"<!-- boris-review request_id={re.escape(request.request_id)} "
            r"result_hash=sha256:[0-9a-f]{64} -->"
        )
        matches = [
            item
            for item in rows
            if isinstance(item, dict) and marker.search(str(item.get("body") or ""))
        ]
        if len(matches) > 1:
            raise RuntimeError("multiple marker-matched GitHub reviews found")
        if not matches:
            return None
        match = matches[0]
        review_id = str(match.get("id") or "")
        url = str(match.get("html_url") or "")
        if not review_id or not url:
            raise RuntimeError("marker-matched GitHub review is missing identity")
        return PublishedReview(review_id, url, str(match.get("body") or ""))

    def publish_review(self, request: LifecycleRequest, body: str) -> PublishedReview:
        raw = self._api(
            [
                "--method",
                "POST",
                f"repos/{request.owner}/{request.repo}/pulls/{request.pr_number}/reviews",
                "--input",
                "-",
            ],
            payload={"event": "COMMENT", "body": body},
        )
        if not isinstance(raw, dict):
            raise RuntimeError("GitHub review publication returned a non-object")
        review_id = str(raw.get("id") or "")
        url = str(raw.get("html_url") or "")
        if not review_id or not url:
            raise RuntimeError("GitHub review publication returned no identity")
        return PublishedReview(review_id, url, str(raw.get("body") or body))
