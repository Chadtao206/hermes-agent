import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake import github
from boris_intake.github import GitHubRetryBudget, fetch_pr
from boris_intake.models import (
    GateSnapshot,
    GitHubReadStatus,
    NormalizedEvent,
    ReviewCommand,
    ReviewPolicy,
    SnapshotStatus,
)
from boris_intake.store import Store


def fake_runner(payload, returncode=0):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")
    return run


# --- the real stderr shapes, and the machinery to drive time by hand ---------
#
# Probed through `ghx_ro.sh` against `Ylopo/forge-content-service` on
# 2026-08-17, not remembered: see `github._NOT_FOUND_SHAPES` for the transcript.
# A test that guessed this string would pass while production classified every
# outage as a missing pull request, which is the defect this file now guards.
NOT_FOUND_STDERR = (
    "GraphQL: Could not resolve to a PullRequest with the number of 999999. "
    "(repository.pullRequest)\n"
)
NO_REPOSITORY_STDERR = (
    "GraphQL: Could not resolve to a Repository with the name "
    "'Ylopo/no-such-repo-xyz'. (repository)\n"
)
#: The 2026-08-17 fault itself, as `gh` surfaced it.
FAULT_STDERR = "GraphQL: No server is currently available to service your request.\n"
REFUSED_STDERR = "ghx_ro: REFUSED out of scope repo 'Ylopo/other' (scope Ylopo/repo#5)\n"
NO_TOKEN_STDERR = "ghx_ro: cannot read token file /Users/ctao/.hermes/state/.gh_token\n"
OPEN_PR_JSON = json.dumps(
    {"headRefOid": "a" * 40, "author": {"login": "zjenkins"}, "state": "OPEN"}
)


def proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class Clock:
    """A hand-driven monotonic clock. Nothing in this file ever really sleeps:
    a suite that waited out a 20s deadline is a suite people stop running."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Runner:
    """Scripted spawns. The last answer repeats, so `Runner(fault)` faults
    forever and `Runner(fault, ok)` faults once. `cost` is the wall clock each
    spawn burns, which is how a test closes a retry window without sleeping."""

    def __init__(self, *answers, clock: Clock | None = None, cost: float = 0.0):
        self.answers = list(answers)
        self.clock = clock
        self.cost = cost
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.clock is not None and self.cost:
            self.clock.advance(self.cost)
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def spawns(self) -> int:
        return len(self.calls)


def budget_on(clock: Clock) -> GitHubRetryBudget:
    return GitHubRetryBudget(sleep=clock.sleep, clock=clock)


def test_parses_open_pr():
    lookup = fetch_pr("Ylopo", "repo", 1, runner=fake_runner(
        {"headRefOid": "a" * 40, "author": {"login": "zjenkins"}, "state": "OPEN"}))
    assert lookup.found
    assert lookup.info.head_sha == "a" * 40
    assert lookup.info.author == "zjenkins"
    assert lookup.info.state == "OPEN"


def test_a_nonzero_exit_is_unavailable_not_absent():
    lookup = fetch_pr("Ylopo", "repo", 1, runner=fake_runner({}, returncode=1))
    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is True


def test_garbage_output_is_unavailable_not_absent():
    def run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")
    lookup = fetch_pr("Ylopo", "repo", 1, runner=run)
    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is True


def test_a_short_sha_is_unavailable_not_absent():
    lookup = fetch_pr("Ylopo", "repo", 1, runner=fake_runner(
        {"headRefOid": "abc", "author": {"login": "x"}, "state": "OPEN"}))
    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.info is None


def test_fetch_gate_snapshot_pins_head_and_marks_required_checks():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "view" in cmd:
            payload = {"headRefOid": "a" * 40}
        elif "--required" in cmd:
            payload = [{"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"}]
        else:
            payload = [
                {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"},
                {"name": "optional-lint", "state": "SUCCESS", "link": "https://ci/lint"},
            ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    capture = github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    )

    assert capture.captured
    required = {check.check_name: check.required for check in capture.snapshot.checks}
    assert required == {"optional-lint": False, "unit-tests": True}
    assert len(calls) == 3


def test_fetch_gate_snapshot_fails_closed_when_head_changed():
    capture = github.fetch_gate_snapshot(
        "Ylopo",
        "repo",
        1,
        "a" * 40,
        "2026-08-14T12:00:00Z",
        runner=fake_runner({"headRefOid": "b" * 40}),
    )

    assert capture.status is SnapshotStatus.HEAD_MOVED
    assert capture.snapshot is None


def test_fetch_gate_snapshot_accepts_check_failure_exit_with_valid_json():
    def run(cmd, **kwargs):
        if "view" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40}),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="[]", stderr="checks failed")

    assert github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    ).captured


def test_fetch_gate_snapshot_treats_no_required_checks_exit_as_empty():
    def run(cmd, **kwargs):
        if "view" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40}),
                stderr="",
            )
        if "--required" in cmd:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="no required checks reported on the 'feature' branch\n",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([
                {"name": "optional-lint", "state": "SUCCESS", "link": "https://ci/lint"}
            ]),
            stderr="",
        )

    capture = github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    )

    assert capture.captured
    assert [
        (check.check_name, check.required) for check in capture.snapshot.checks
    ] == [("optional-lint", False)]


def test_fetch_gate_snapshot_fails_closed_when_required_check_is_missing_from_rollup():
    def run(cmd, **kwargs):
        if "view" in cmd:
            payload = {"headRefOid": "a" * 40}
        elif "--required" in cmd:
            payload = [{"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"}]
        else:
            payload = []
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    capture = github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    )
    assert capture.status is SnapshotStatus.UNAVAILABLE


def lifecycle_request(tmp_path):
    store = Store(tmp_path / "review.db")
    command = ReviewCommand(
        owner="Ylopo",
        repo="example",
        pr_number=42,
        head_sha="a" * 40,
        pr_author="octocat",
        requester_user_id="U1",
        requester_email="a@ylopo.com",
        channel_id="C1",
        message_ts="1.0",
        root_thread_ts="1.0",
        permalink="https://ylopo.slack.com/archives/C1/p10",
        policy=ReviewPolicy(False),
    )
    event = NormalizedEvent(
        "C1", "channel", "U1", "1.0", None, "discarded", False, None
    )
    snapshot = GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())
    return store.admit_event(event, (command,), (snapshot,), 1_000_000.0).admitted[0]


def test_lifecycle_review_lookup_matches_only_the_hidden_request_marker(tmp_path):
    request = lifecycle_request(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        payload = [
            [
                {
                    "id": 1,
                    "html_url": "https://github/reviews/1",
                    "body": "ordinary comment",
                },
                {
                    "id": 2,
                    "html_url": "https://github/reviews/2",
                    "body": (
                        "ready\n<!-- boris-review request_id="
                        f"{request.request_id} result_hash=sha256:{'a' * 64} -->"
                    ),
                },
            ]
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    found = github.GitHubIO(token, runner=run).find_review(request)

    assert found.review_id == "2"
    assert found.url == "https://github/reviews/2"
    assert found.body.startswith("ready")
    assert calls[0][0] == [
        "/opt/homebrew/bin/gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/Ylopo/example/pulls/42/reviews?per_page=100",
    ]
    assert calls[0][1]["env"]["GH_TOKEN"] == "secret"


def test_lifecycle_publication_posts_comment_review_with_body_on_json_input(tmp_path):
    request = lifecycle_request(tmp_path)
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        payload = {
            "id": 7,
            "html_url": "https://github/reviews/7",
            "body": "rendered body",
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    published = github.GitHubIO(token, runner=run).publish_review(
        request, "rendered body"
    )

    assert published.review_id == "7"
    assert seen["command"] == [
        "/opt/homebrew/bin/gh",
        "api",
        "--method",
        "POST",
        "repos/Ylopo/example/pulls/42/reviews",
        "--input",
        "-",
    ]
    assert json.loads(seen["kwargs"]["input"]) == {
        "event": "COMMENT",
        "body": "rendered body",
    }
    assert "rendered body" not in seen["command"]


# ---------------------------------------------------------------------------
# Classification: "GitHub was unreachable" is not "that PR does not exist".
#
# On 2026-08-17 GitHub answered HTTP 503 on roughly half of all calls for
# several hours. `fetch_pr` returned `None`, the gate turned that into
# PR_NOT_FOUND -- a terminal rejection asserting the pull request does not
# exist -- and `Ylopo/forge-content-service#111` sat open the whole time while
# the remediation loop waited for a verdict that could never arrive.
# ---------------------------------------------------------------------------


def test_githubs_own_not_found_shape_is_the_only_thing_that_reads_as_absent():
    lookup = fetch_pr(
        "Ylopo", "repo", 999999,
        runner=Runner(proc(returncode=1, stderr=NOT_FOUND_STDERR)),
    )

    assert lookup.status is GitHubReadStatus.ABSENT
    assert lookup.retryable is False


def test_an_unresolvable_repository_is_absent_too():
    """The pull request cannot exist if its repository does not -- and the same
    answer covers a repository this credential cannot see, which is what
    `pr_not_found`'s reply text has always said ("or I can't see it")."""
    lookup = fetch_pr(
        "Ylopo", "no-such-repo-xyz", 4,
        runner=Runner(proc(returncode=1, stderr=NO_REPOSITORY_STDERR)),
    )

    assert lookup.status is GitHubReadStatus.ABSENT


def test_the_outage_that_caused_this_reads_as_unavailable_not_absent():
    """The exact 2026-08-17 answer. `absent` here is the lie that stranded three
    remediation loops for hours."""
    lookup = fetch_pr(
        "Ylopo", "forge-content-service", 111,
        runner=Runner(proc(returncode=1, stderr=FAULT_STDERR)),
    )

    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is True
    assert lookup.info is None


def test_a_runner_that_raises_is_unavailable_and_retryable():
    lookup = fetch_pr(
        "Ylopo", "repo", 5, runner=Runner(TimeoutError("timed out after 30s"))
    )

    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is True


def test_an_answer_with_no_usable_head_sha_is_unavailable_not_absent():
    lookup = fetch_pr(
        "Ylopo", "repo", 5,
        runner=Runner(proc(stdout=json.dumps({"headRefOid": "not-a-sha"}))),
    )

    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is True


def test_an_empty_body_is_unavailable_not_absent():
    lookup = fetch_pr("Ylopo", "repo", 5, runner=Runner(proc(stdout="")))

    assert lookup.status is GitHubReadStatus.UNAVAILABLE


def test_a_wrapper_scope_refusal_is_unavailable_but_never_retried():
    """Exit 3 from `ghx_ro.sh::refuse`. Unavailable, because no answer was
    obtained -- but a scope refusal is a wiring bug, and retrying it burns the
    message's window on a call that cannot start succeeding."""
    clock = Clock()
    runner = Runner(proc(returncode=3, stderr=REFUSED_STDERR))

    lookup = fetch_pr(
        "Ylopo", "repo", 5, runner=runner, budget=budget_on(clock)
    )

    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is False
    assert runner.spawns == 1
    assert clock.sleeps == []


def test_an_unreadable_credential_is_unavailable_but_never_retried():
    """The wrapper's own exit 1 -- which is why the exit code cannot be the
    not-found signal: `gh` uses 1 for a missing pull request too."""
    clock = Clock()
    runner = Runner(proc(returncode=1, stderr=NO_TOKEN_STDERR))

    lookup = fetch_pr(
        "Ylopo", "repo", 5, runner=runner, budget=budget_on(clock)
    )

    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert lookup.retryable is False
    assert runner.spawns == 1


# ---------------------------------------------------------------------------
# The bounded retry, and the one wall-clock window it shares per message.
# ---------------------------------------------------------------------------


def test_a_transient_fault_is_retried_and_the_identical_call_then_succeeds():
    clock = Clock()
    runner = Runner(
        proc(returncode=1, stderr=FAULT_STDERR),
        proc(returncode=1, stderr=FAULT_STDERR),
        proc(stdout=OPEN_PR_JSON),
    )

    lookup = fetch_pr(
        "Ylopo", "repo", 5, runner=runner, budget=budget_on(clock)
    )

    assert lookup.found
    assert lookup.info.head_sha == "a" * 40
    assert runner.spawns == 3
    # Doubling from 1s, so three attempts sleep 1+2 and no more.
    assert clock.sleeps == [1.0, 2.0]


def test_the_attempt_budget_is_spent_not_exceeded():
    clock = Clock()
    runner = Runner(proc(returncode=1, stderr=FAULT_STDERR))

    lookup = fetch_pr(
        "Ylopo", "repo", 5, runner=runner, budget=budget_on(clock)
    )

    assert lookup.status is GitHubReadStatus.UNAVAILABLE
    assert runner.spawns == 3
    assert clock.sleeps == [1.0, 2.0]


def test_without_a_budget_a_read_is_single_shot():
    """`GitHubIO.current_pr` passes no budget, and the reconciler's own backoff
    owns the retry there. A caller that forgets one must get today's behaviour,
    not an unbudgeted loop inside a live event handler."""
    runner = Runner(proc(returncode=1, stderr=FAULT_STDERR))

    assert fetch_pr("Ylopo", "repo", 5, runner=runner).unavailable
    assert runner.spawns == 1


def test_one_budget_bounds_every_read_of_one_message_together():
    """The window is per MESSAGE. A per-call window would let a 40-link paste
    spend forty of them inside one synchronous handler call."""
    clock = Clock()
    budget = budget_on(clock)
    # 8s per spawn: the first read's three attempts run the 20s window out.
    first = Runner(
        proc(returncode=1, stderr=FAULT_STDERR), clock=clock, cost=8.0
    )
    fetch_pr("Ylopo", "repo", 5, runner=first, budget=budget)
    assert first.spawns == 3

    second = Runner(
        proc(returncode=1, stderr=FAULT_STDERR), clock=clock, cost=8.0
    )
    lookup = fetch_pr("Ylopo", "repo", 6, runner=second, budget=budget)

    # Same budget object, window already closed: one attempt, no retry.
    assert second.spawns == 1
    assert lookup.status is GitHubReadStatus.UNAVAILABLE


def test_a_closed_window_stops_retries_and_never_a_first_attempt():
    """A closed window must stop retrying, not stop reading. Refusing the first
    attempt would report GitHub as unavailable for pull requests nobody ever
    looked at."""
    clock = Clock()
    budget = budget_on(clock)
    budget.open_window()
    clock.advance(github.RETRY_DEADLINE + 1.0)
    runner = Runner(proc(stdout=OPEN_PR_JSON))

    lookup = fetch_pr("Ylopo", "repo", 5, runner=runner, budget=budget)

    assert lookup.found
    assert runner.spawns == 1


def test_the_retry_budget_fits_inside_the_channel_reaction_window():
    """The arithmetic that chose these numbers, pinned so raising one cannot
    quietly slow the whole channel down.

    `Pipeline.handle` runs synchronously inside the daemon's asyncio loop, so
    everything this retry spends delays every other message in the channel and
    the heartbeat write. The channel's stated reaction-time expectation is ~60s,
    and that is the ceiling on what the RETRY may ADD -- not on what a message
    costs, which is dominated by the un-retried pass and was true before this
    retry existed. Retries only START inside the window, so the added time is
    the window, plus one straggler attempt begun just inside it, plus its
    backoff.
    """
    reaction_window = 60.0
    added_worst = github.RETRY_DEADLINE + github.TIMEOUT + github.BACKOFF_CAP
    assert added_worst < reaction_window, added_worst

    # And that figure is per MESSAGE however many links it names, which is what
    # sharing the window buys: a 40-ref paste with per-call windows would add
    # this much to one handler call instead.
    assert 40 * added_worst > 30 * 60

    # Three attempts, not two: at the measured ~50% fault rate two still loses
    # a read whenever two faults land in a row. Not four: the deadline would be
    # spent on backoff.
    assert github.PR_ATTEMPTS == 3 and github.SNAPSHOT_ATTEMPTS == 3
    worst_sleep_per_read = sum(
        min(github.BACKOFF_BASE * 2 ** (attempt - 1), github.BACKOFF_CAP)
        for attempt in range(1, github.PR_ATTEMPTS)
    )
    assert worst_sleep_per_read <= 3.0, worst_sleep_per_read


# ---------------------------------------------------------------------------
# The Gate Snapshot capture: three states, and the head-mismatch case that must
# not be swept up in the outage one.
# ---------------------------------------------------------------------------


def snapshot(runner, *, expected="a" * 40, budget=None):
    return github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, expected, "2026-08-14T12:00:00Z",
        runner=runner, budget=budget,
    )


def test_a_moved_head_is_terminal_never_retried_and_never_an_outage():
    clock = Clock()
    runner = Runner(proc(stdout=json.dumps({"headRefOid": "b" * 40})))

    capture = snapshot(runner, budget=budget_on(clock))

    assert capture.status is SnapshotStatus.HEAD_MOVED
    assert capture.retryable is False
    assert "moved" in capture.detail
    # One spawn: a retry cannot un-move a head, and this is the commonest
    # legitimate reason a capture refuses.
    assert runner.spawns == 1
    assert clock.sleeps == []


def test_an_unreadable_head_is_an_outage_and_is_retried():
    clock = Clock()
    runner = Runner(
        proc(returncode=1, stderr=FAULT_STDERR),
        proc(returncode=1, stderr=FAULT_STDERR),
        proc(stdout=json.dumps({"headRefOid": "a" * 40})),
        proc(stdout=json.dumps([])),
        proc(stdout=json.dumps([])),
    )

    capture = snapshot(runner, budget=budget_on(clock))

    assert capture.captured
    assert runner.spawns == 5
    assert clock.sleeps == [1.0, 2.0]


def test_a_head_read_that_never_answers_is_unavailable_not_moved():
    clock = Clock()
    runner = Runner(proc(returncode=1, stderr=FAULT_STDERR))

    capture = snapshot(runner, budget=budget_on(clock))

    assert capture.status is SnapshotStatus.UNAVAILABLE
    assert capture.snapshot is None
    assert runner.spawns == 3


def test_an_unusable_head_sha_is_unavailable_not_moved():
    capture = snapshot(Runner(proc(stdout=json.dumps({"headRefOid": "abc"}))))

    assert capture.status is SnapshotStatus.UNAVAILABLE


def test_a_checks_read_that_never_answers_is_unavailable():
    clock = Clock()
    runner = Runner(
        proc(stdout=json.dumps({"headRefOid": "a" * 40})),
        TimeoutError("timed out"),
    )

    capture = snapshot(runner, budget=budget_on(clock))

    assert capture.status is SnapshotStatus.UNAVAILABLE
    # One head read, then the checks read spends its own three attempts.
    assert runner.spawns == 4


def test_the_snapshot_reads_share_the_messages_window_too():
    clock = Clock()
    runner = Runner(
        proc(returncode=1, stderr=FAULT_STDERR), clock=clock, cost=8.0
    )
    budget = budget_on(clock)
    fetch_pr("Ylopo", "repo", 1, runner=runner, budget=budget)
    assert runner.spawns == 3

    later = Runner(
        proc(returncode=1, stderr=FAULT_STDERR), clock=clock, cost=8.0
    )
    capture = snapshot(later, budget=budget)

    assert capture.status is SnapshotStatus.UNAVAILABLE
    assert later.spawns == 1
