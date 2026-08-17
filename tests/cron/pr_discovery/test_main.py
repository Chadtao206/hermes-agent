"""Tests for one discovery tick: what it selects, what it posts, and what it
refuses to say nothing about.

Two invariants dominate this file.

*A paused tick is structurally incapable of posting.* Shadow does not branch
around the post call -- it never reaches the code that holds one. That is
pinned three ways: by behaviour (`test_shadow_mode_posts_nothing`), by a
poster that detonates if it is so much as touched
(`test_shadow_never_reaches_the_poster_at_all`), and by the single place the
mode is asked (`test_the_mode_is_asked_in_exactly_one_place`).

*An empty tick is not automatically a healthy one.* `candidates == () and
complete` is the only shape that means "nothing to post"; a failed org, a
truncated listing, a config error or a refused post all mean this tick did
not see, or did not do, what it was for -- and each has to reach a human.

Nothing here touches the network, Slack, `gh` or the real config file: the
Slack transport is a recording double, the ledger is either an in-memory
double or a real SQLite ledger under `tmp_path`, and every selftest path is
pointed at `tmp_path` files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pr_discovery import main as main_module
from pr_discovery.discover import Candidate, DiscoveryResult, IntakeSource
from pr_discovery.main import (
    AUTHOR,
    DEFAULT_INTAKE_DB,
    MAX_POSTS_PER_TICK,
    DiscoveryConfig,
    DiscoveryDeps,
    RunResult,
    _consumer,
    _perform,
    _rehearse,
    banned_repos,
    build_deps,
    load_config,
    run,
    run_selftest,
)
from pr_discovery.post import DISCOVERY_MARKER, Poster
from pr_discovery.state import Ledger

MARKER = "[swept this one up. v1]"


def _c(number=1, *, owner="Ylopo", repo="x", head_sha="abc123"):
    return Candidate(owner=owner, repo=repo, number=number, head_sha=head_sha,
                     merge_state="CLEAN", checks_state="SUCCESS",
                     has_hold=False)


class _Slack:
    """The real `Poster`, wired to a recording transport.

    Deliberately not a hand-written fake poster: composing the message is the
    thing most worth not faking, so the double replaces only `slack_call`.

    `calls` is every ATTEMPT; `posted` is only the text Slack accepted. The
    two are kept apart on purpose -- an assertion reading `posted == []` is
    meant to say "nothing landed in the intake channel", and a `posted` that
    recorded refused attempts too would have read as a much stronger claim
    than it was making.
    """

    def __init__(self, *, fails=False, fail_calls=()):
        self.calls: list[tuple[str, str, dict]] = []
        self.posted: list[str] = []
        self._fails = fails
        self._fail_calls = set(fail_calls)
        self._poster = Poster("tok", self._call)

    def _call(self, token, method, **params):
        self.calls.append((token, method, params))
        if self._fails or len(self.calls) in self._fail_calls:
            return {"ok": False, "error": "not_in_channel"}
        self.posted.append(params["text"])
        return {"ok": True, "ts": f"{len(self.calls)}.0"}

    def post(self, candidate):
        return self._poster.post(candidate)


class _ExplodingSlack:
    """A poster that detonates on any use at all.

    `posted` is per-INSTANCE. As a class attribute it was one list shared by
    every instance for the whole session, so a leak in one test could have
    been read as another test's evidence.
    """

    def __init__(self):
        self.posted: list[str] = []

    def post(self, candidate):
        raise AssertionError("a paused tick reached the poster")


class _FakeLedger:
    """Mirrors `state.Ledger`'s two methods, including `record_posted`'s
    keyword-only `now` (see `test_the_real_ledger_answers_the_same_calls`,
    which runs the same tick against the real one so this double cannot
    drift into a shape production does not have)."""

    def __init__(self, has=()):
        self._has = {tuple(row) for row in has}
        self.records: list[tuple] = []

    def already_posted(self, owner, repo, number, head_sha):
        return (owner, repo, number, head_sha) in self._has

    def record_posted(self, owner, repo, number, head_sha, *, now):
        self.records.append((owner, repo, number, head_sha, now))
        self._has.add((owner, repo, number, head_sha))
        return True


class _Discovery:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result


def _deps(*, candidates=(), ledger_has=(), slack_fails=False, fail_calls=(),
          result=None, slack=None):
    """One tick's dependencies.

    `mode` is deliberately NOT a field here -- it is `run`'s own keyword, so
    there is exactly one place a tick's mode can come from and no way for a
    deps-borne mode to disagree with the one the caller asked for.
    """
    return DiscoveryDeps(
        discover=_Discovery(result if result is not None
                            else DiscoveryResult(candidates=tuple(candidates))),
        ledger=_FakeLedger(ledger_has),
        slack=slack if slack is not None else _Slack(fails=slack_fails,
                                                     fail_calls=fail_calls),
        now=lambda: 1000.0,
    )


# ---------------------------------------------------------------------------
# Mode discipline
# ---------------------------------------------------------------------------

def test_shadow_mode_posts_nothing():
    deps = _deps(candidates=[_c(1), _c(2)])
    run(deps, mode="shadow")
    assert deps.slack.posted == []


def test_shadow_never_reaches_the_poster_at_all():
    """Not "posts nothing" but "never touches the thing that could post":
    a poster that raises on any use survives a whole shadow tick."""
    deps = _deps(candidates=[_c(1)], slack=_ExplodingSlack())
    result = run(deps, mode="shadow")
    assert result.selected == ("Ylopo/x#1@abc123",)


def test_shadow_does_not_record_to_the_ledger():
    deps = _deps(candidates=[_c(1)])
    run(deps, mode="shadow")
    assert deps.ledger.records == []


def test_shadow_reports_what_a_live_tick_would_have_posted():
    deps = _deps(candidates=[_c(1), _c(2)])
    result = run(deps, mode="shadow")
    assert result.selected == ("Ylopo/x#1@abc123", "Ylopo/x#2@abc123")
    assert result.posted == ()
    assert "would post 2" in result.summary()


def test_the_mode_is_asked_in_exactly_one_place():
    """`_consumer` is the whole of this module's mode-awareness, exactly as
    `pr_remediation/loop.py::_consumer` is for the loop: a branch added to
    `_perform` cannot leak into a paused tick, because a paused tick never
    reaches `_perform`."""
    assert _consumer("live") is _perform
    assert _consumer("shadow") is _rehearse
    assert _consumer("off") is _rehearse


def test_off_does_not_even_enumerate():
    """`off` is the hard kill switch: it stops the work, not merely the
    writes, so it costs no GitHub reads at all."""
    deps = _deps(candidates=[_c(1)])
    result = run(deps, mode="off")
    assert deps.discover.calls == 0
    assert deps.slack.posted == [] and deps.ledger.records == []
    assert result.enumerated is False


def test_shadow_and_live_both_enumerate():
    for mode in ("shadow", "live"):
        deps = _deps(candidates=[_c(1)])
        assert run(deps, mode=mode).enumerated is True
        assert deps.discover.calls == 1


def test_an_unknown_mode_refuses_to_run():
    deps = _deps(candidates=[_c(1)])
    with pytest.raises(ValueError):
        run(deps, mode="Live")
    assert deps.discover.calls == 0
    assert deps.slack.posted == [] and deps.ledger.records == []


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def test_live_posts_one_message_per_pr():
    deps = _deps(candidates=[_c(1), _c(2)])
    run(deps, mode="live")
    assert len(deps.slack.posted) == 2


def test_message_carries_link_and_marker():
    deps = _deps(candidates=[_c(1)])
    run(deps, mode="live")
    text = deps.slack.posted[0]
    assert "https://github.com/Ylopo/x/pull/1" in text
    assert DISCOVERY_MARKER in text


def test_already_posted_head_is_skipped():
    deps = _deps(candidates=[_c(1)], ledger_has=[("Ylopo", "x", 1, "abc123")])
    result = run(deps, mode="live")
    assert deps.slack.posted == []
    assert result.skipped == ("Ylopo/x#1@abc123",)


def test_a_moved_head_re_qualifies():
    """The ledger keys on the head, not the PR: a PR that has moved since we
    posted it is a new identity and gets a fresh review."""
    deps = _deps(candidates=[_c(1, head_sha="def456")],
                 ledger_has=[("Ylopo", "x", 1, "abc123")])
    run(deps, mode="live")
    assert len(deps.slack.posted) == 1


def test_ledger_records_only_after_a_successful_post():
    """A failed post must re-queue, not be recorded as done."""
    deps = _deps(candidates=[_c(1)], slack_fails=True)
    result = run(deps, mode="live")
    assert deps.ledger.records == []
    assert result.failed_posts == ("Ylopo/x#1@abc123",)
    assert result.posted == ()


def test_a_refused_post_is_an_attempt_and_not_a_post():
    """Slack refusing the call means nothing landed in the intake channel --
    so the attempt is on `calls` and NOT on `posted`, which is what every
    "posted == []" assertion in this file is claiming."""
    deps = _deps(candidates=[_c(1)], slack_fails=True)
    result = run(deps, mode="live")
    assert len(deps.slack.calls) == 1
    assert deps.slack.posted == []
    assert result.posted == ()


def test_a_successful_post_is_recorded_with_the_ticks_clock():
    deps = _deps(candidates=[_c(1)])
    run(deps, mode="live")
    assert deps.ledger.records == [("Ylopo", "x", 1, "abc123", 1000.0)]


def test_a_failed_post_does_not_stop_the_next_pr():
    """One message per PR means one PR's failure is one PR's failure."""
    deps = _deps(candidates=[_c(1), _c(2)], fail_calls=(1,))
    result = run(deps, mode="live")
    assert result.failed_posts == ("Ylopo/x#1@abc123",)
    assert result.posted == ("Ylopo/x#2@abc123",)
    assert deps.ledger.records == [("Ylopo", "x", 2, "abc123", 1000.0)]


def test_the_per_tick_cap_is_ten():
    """The literal, because every other cap assertion here reads the constant
    and so moves with it: raising the cap to 40 changes what a first live tick
    does to the review fleet and nothing in this file notices.

    Ten is a ceiling on how many Boris review lanes one tick may start at
    once, which is why it is a small number rather than a fraction of Chad's
    hourly intake quota (`boris_intake.limits.per_user_hourly`, 120 in the
    deployed config -- not the binding constraint at any plausible cap).
    """
    assert MAX_POSTS_PER_TICK == 10


def test_the_per_tick_cap_defers_the_rest_rather_than_dropping_them():
    over = MAX_POSTS_PER_TICK + 2
    deps = _deps(candidates=[_c(n) for n in range(1, over + 1)])
    result = run(deps, mode="live")
    assert len(result.posted) == MAX_POSTS_PER_TICK
    assert len(result.deferred) == 2
    assert len(deps.ledger.records) == MAX_POSTS_PER_TICK
    assert "deferred" in result.summary()


def test_a_deferred_pr_is_not_recorded_so_it_comes_back_next_tick():
    over = MAX_POSTS_PER_TICK + 1
    deps = _deps(candidates=[_c(n) for n in range(1, over + 1)])
    result = run(deps, mode="live")
    assert result.deferred == (f"Ylopo/x#{over}@abc123",)
    assert not deps.ledger.already_posted("Ylopo", "x", over, "abc123")


def test_the_real_ledger_answers_the_same_calls(tmp_path):
    """The doubles above could drift from `state.Ledger`'s real signature, so
    one whole tick runs against the real one."""
    ledger = Ledger(tmp_path / "discovery.db")
    slack = _Slack()
    deps = DiscoveryDeps(
        discover=_Discovery(DiscoveryResult(candidates=(_c(1),))),
        ledger=ledger, slack=slack, now=lambda: 1000.0)
    assert run(deps, mode="live").posted == ("Ylopo/x#1@abc123",)
    assert ledger.already_posted("Ylopo", "x", 1, "abc123") is True

    again = DiscoveryDeps(
        discover=_Discovery(DiscoveryResult(candidates=(_c(1),))),
        ledger=ledger, slack=slack, now=lambda: 2000.0)
    assert run(again, mode="live").skipped == ("Ylopo/x#1@abc123",)
    assert len(slack.posted) == 1


# ---------------------------------------------------------------------------
# The three failure channels, and the silence rule
# ---------------------------------------------------------------------------

def test_a_clean_empty_tick_is_the_only_silent_one():
    result = run(_deps(), mode="live")
    assert result.complete is True
    assert result.summary() == ""


def test_a_tick_that_had_already_posted_everything_is_silent_too():
    """The steady state, not news: every candidate is already in the ledger
    at this head, nothing failed, and the enumeration was complete."""
    deps = _deps(candidates=[_c(1)], ledger_has=[("Ylopo", "x", 1, "abc123")])
    result = run(deps, mode="live")
    assert result.skipped == ("Ylopo/x#1@abc123",)
    assert result.summary() == ""


def test_an_org_that_could_not_be_enumerated_is_never_silent():
    deps = _deps(result=DiscoveryResult(failed_orgs=("Ylopo",)))
    result = run(deps, mode="live")
    assert result.failed_orgs == ("Ylopo",)
    assert result.complete is False
    assert "Ylopo" in result.summary()


def test_a_truncated_listing_is_never_silent():
    deps = _deps(result=DiscoveryResult(candidates=(_c(1),),
                                        truncated_orgs=("Ylopo",)))
    result = run(deps, mode="live")
    assert result.truncated_orgs == ("Ylopo",)
    assert result.complete is False
    assert "truncat" in result.summary()


def test_a_config_error_is_never_silent():
    deps = _deps(result=DiscoveryResult(config_errors=("orgs is empty",)))
    result = run(deps, mode="live")
    assert result.config_errors == ("orgs is empty",)
    assert result.complete is False
    assert "orgs is empty" in result.summary()


def test_a_config_error_stops_the_tick_from_posting_anything():
    """An untrustworthy parameter means we do not know what we are allowed to
    post, so we post nothing -- even if the same result somehow carried
    candidates."""
    deps = _deps(result=DiscoveryResult(candidates=(_c(1),),
                                        config_errors=("banned_repos is a str",)))
    result = run(deps, mode="live")
    assert deps.slack.posted == []
    assert result.selected == ()


def test_a_failed_post_is_never_silent():
    deps = _deps(candidates=[_c(1)], slack_fails=True)
    assert "Ylopo/x#1@abc123" in run(deps, mode="live").summary()


def test_a_result_that_is_not_a_discoveryresult_is_a_fault_not_a_quiet_tick():
    """The shape `candidates()` returned before `5fa528f23d`. A tick that
    accepted it would post nothing and report a healthy run forever."""
    deps = _deps(result=[])
    result = run(deps, mode="live")
    assert deps.slack.posted == []
    assert result.config_errors and result.complete is False
    assert result.summary() != ""


def test_a_tick_that_could_not_check_the_intake_database_is_never_silent():
    """The fourth incompleteness channel. Proceeding past an unreadable review
    database is only safe if it is also visible: a filter that quietly stopped
    filtering is exactly what a silent empty channel would hide."""
    deps = _deps(result=DiscoveryResult(
        candidates=(_c(1),),
        unverified_heads=("1 pull request(s) went unchecked",)))
    result = run(deps, mode="live")
    assert result.unverified_heads == ("1 pull request(s) went unchecked",)
    assert result.complete is False
    assert "went unchecked" in result.summary()


def test_an_unreadable_intake_database_does_not_stop_the_post():
    """The asymmetry at tick level. A config error stops the tick dead (see
    `test_a_config_error_stops_the_tick_from_posting_anything`); an unreadable
    review database must NOT, because skipping would leave the pull request
    silently unreviewed with no backstop, while posting costs at most a
    duplicate message the intake idempotency key already dedups."""
    deps = _deps(result=DiscoveryResult(
        candidates=(_c(1),), unverified_heads=("could not be checked",)))
    result = run(deps, mode="live")
    assert result.posted == ("Ylopo/x#1@abc123",)
    assert deps.slack.posted != []


def test_a_config_error_still_reports_the_unchecked_heads_alongside_it():
    """A config error takes the early exit out of `_plan`, which has to carry
    every other channel out with it -- one degradation must not swallow the
    report of another."""
    deps = _deps(result=DiscoveryResult(
        config_errors=("banned_repos is a str",),
        unverified_heads=("2 pull request(s) went unchecked",)))
    summary = run(deps, mode="live").summary()
    assert "banned_repos is a str" in summary
    assert "2 pull request(s) went unchecked" in summary


def test_a_failed_pr_read_is_never_silent():
    """The fifth incompleteness channel. A PR dropped because its `gh pr
    view` could not be read is indistinguishable from one that legitimately
    did not qualify unless this reaches a human too."""
    deps = _deps(result=DiscoveryResult(
        candidates=(_c(1),),
        failed_pr_reads=("1 pull request(s) could not be read",)))
    result = run(deps, mode="live")
    assert result.failed_pr_reads == ("1 pull request(s) could not be read",)
    assert result.complete is False
    assert "could not be read" in result.summary()


def test_a_failed_pr_read_does_not_stop_the_post_of_what_was_found():
    """Unlike a config error, a per-PR read failure is a gap in what was
    SEEN, not a reason to distrust what WAS found -- the candidates that did
    come back are still posted."""
    deps = _deps(result=DiscoveryResult(
        candidates=(_c(1),),
        failed_pr_reads=("1 pull request(s) could not be read",)))
    result = run(deps, mode="live")
    assert result.posted == ("Ylopo/x#1@abc123",)
    assert deps.slack.posted != []


def test_a_config_error_still_reports_the_failed_pr_reads_alongside_it():
    """Mirrors `test_a_config_error_still_reports_the_unchecked_heads_alongside_it`
    for the fifth channel -- the early exit out of `_plan` must carry this
    one out too."""
    deps = _deps(result=DiscoveryResult(
        config_errors=("banned_repos is a str",),
        failed_pr_reads=("3 pull request(s) could not be read",)))
    summary = run(deps, mode="live").summary()
    assert "banned_repos is a str" in summary
    assert "3 pull request(s) could not be read" in summary


def test_the_summary_names_every_channel_at_once():
    deps = _deps(result=DiscoveryResult(candidates=(_c(1),),
                                        failed_orgs=("Foo",),
                                        truncated_orgs=("Bar",),
                                        unverified_heads=("quux went unchecked",),
                                        config_errors=("baz is wrong",),
                                        failed_pr_reads=("quux2 could not be read",)))
    summary = run(deps, mode="live").summary()
    assert "Foo" in summary and "Bar" in summary and "baz is wrong" in summary
    assert "quux went unchecked" in summary
    assert "quux2 could not be read" in summary


def test_run_result_defaults_are_a_complete_quiet_tick():
    assert RunResult(mode="live").complete is True
    assert RunResult(mode="live").summary() == ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_reads_the_mode_from_its_own_block(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_discovery:\n  mode: live\n")
    assert load_config(path) == DiscoveryConfig(mode="live")


def test_config_fails_safe_to_shadow(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_discovery:\n  mode: LIVE-ish\n")
    assert load_config(path).mode == "shadow"
    (tmp_path / "empty.yaml").write_text("")
    assert load_config(tmp_path / "empty.yaml").mode == "shadow"
    assert load_config(tmp_path / "nope.yaml").mode == "shadow"


def test_the_excluded_repo_list_is_split_not_handed_over_as_a_string(monkeypatch):
    """`candidates()` names this trap in its own docstring: a bare string
    turns the exact-membership ban into a substring match, so `Ylopo/ylopo-inf`
    would be "banned" for being a substring of `Ylopo/ylopo-infra`."""
    monkeypatch.setenv("PR_REVIEW_EXCLUDED_REPOS", "Ylopo/a Ylopo/b")
    assert banned_repos() == ("Ylopo/a", "Ylopo/b")


def test_the_default_ban_list_has_not_drifted_from_lanes(monkeypatch):
    """Discovery must never post a pull request the lane dispatcher would
    then refuse -- that strands a review Boris has already paid for.

    `banned_repos()` deliberately returns the SPLIT list as-typed (its
    caller, `candidates()`, casefolds internally -- see `discover.py`), while
    `lanes._excluded_repos()` casefolds its own entries directly. Casefold
    here too so this drift guard compares the same two ban lists
    case-insensitively rather than tripping on a formatting difference
    neither side's actual matching behaviour cares about."""
    from pr_remediation import lanes

    monkeypatch.delenv("PR_REVIEW_EXCLUDED_REPOS", raising=False)
    assert frozenset(r.casefold() for r in banned_repos()) == lanes._excluded_repos()


def test_build_deps_wires_the_already_reviewed_clause(tmp_path, monkeypatch):
    """`candidates()` takes `reviewed` as a REQUIRED keyword, so a tick that
    forgot it raises -- but nothing except this test pins that the production
    wiring supplies a real `IntakeSource`, rather than something that answers
    "never reviewed" to everything."""
    seen: dict = {}

    def _record_call(gh, **kwargs):
        seen.update(kwargs)
        return DiscoveryResult()

    monkeypatch.setattr(main_module, "candidates", _record_call)
    deps = build_deps("tok", ledger_db=tmp_path / "ledger.db",
                      token_path=tmp_path / "token")
    deps.discover()
    assert isinstance(seen["reviewed"], IntakeSource)
    assert seen["reviewed"].path == DEFAULT_INTAKE_DB


def test_the_discovered_author_has_not_drifted_from_pr_remediation():
    """`discover._evaluate` compares `AUTHOR` against GitHub's canonical
    `author.login` case-SENSITIVELY, so a case typo -- `chadtao206` for
    `Chadtao206` -- excludes every record ever enumerated and produces
    clean-looking empty ticks forever, with nothing failed and nothing
    deferred to say otherwise. The other shared constants (the ban list, the
    marker, the intake database) all have a drift guard; this one had none,
    and mutating it survived the whole suite.

    Pinned against `pr_remediation`'s own login, which is the same person's
    GitHub identity read by the loop that picks up whatever this cron's posts
    turn into. Compared case-sensitively on purpose: a case-insensitive
    comparison here would pass under exactly the mutation this exists to
    catch."""
    from pr_remediation.config import RemediationConfig

    assert AUTHOR == RemediationConfig().chad_github_login


def test_the_wired_review_database_is_the_one_boris_intake_writes():
    """A path that pointed anywhere else would answer "never reviewed" for
    every head forever -- the clause silently disabled, with no degradation
    reported because the file would open perfectly well. Compared against
    `boris_intake`'s own constant rather than a restated literal."""
    from boris_intake.selftest import DB_PATH

    assert DEFAULT_INTAKE_DB == Path(DB_PATH)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def _boom(*args, **kwargs):
    raise AssertionError("a tick was built after a precondition failed")


class _Recorder:
    """Records the mode each stage of `main()` was actually asked to run in.

    `main()`'s whole job is joining `cfg.mode` to `run()` and `run_selftest()`,
    and that join is one keyword on one line each. Everything either function
    then does with the mode is pinned thoroughly elsewhere in this file -- but
    an assertion that reads only the exit code and stdout cannot tell the real
    line from `run(deps, mode="live")` with `cfg.mode` dropped on the floor,
    which is a shadow-configured deployment posting for real. Nor from
    `run_selftest(mode="shadow")` on a live tick, which silently retires the
    live-only gate checks (marker drift, inert door, CHANNEL_NOT_ALLOWED,
    MENTION_REQUIRED) in exactly the deployment they exist for.
    """

    def __init__(self, *, failures=(), result=None):
        self.selftest: list[str] = []
        self.run: list[str] = []
        self._failures = tuple(failures)
        self._result = result

    def selftest_stub(self, *, mode, **kwargs):
        self.selftest.append(mode)
        return list(self._failures)

    def crashing_selftest_stub(self, *, mode, **kwargs):
        self.selftest.append(mode)
        raise RuntimeError("boris_intake is not importable from here")

    def run_stub(self, deps, *, mode):
        self.run.append(mode)
        return self._result if self._result is not None else RunResult(mode=mode)


def _wire(monkeypatch, *, mode, token="tok", failures=(), result=None,
          selftest_crashes=False, run=None):
    """Point `main()` at doubles for one tick, recording the mode each is
    handed. Nothing here touches the real config, Slack or GitHub."""
    import pr_discovery.main as module

    recorder = _Recorder(failures=failures, result=result)
    monkeypatch.setattr(module, "load_config",
                        lambda *a, **k: DiscoveryConfig(mode=mode))
    monkeypatch.setattr(module, "run_selftest",
                        recorder.crashing_selftest_stub if selftest_crashes
                        else recorder.selftest_stub)
    monkeypatch.setattr(module, "slack_token", lambda: token)
    monkeypatch.setattr(module, "build_deps", lambda token, **k: token)
    monkeypatch.setattr(module, "run",
                        recorder.run_stub if run is None else run)
    return module, recorder


def test_main_runs_no_tick_when_a_precondition_fails(monkeypatch):
    module, calls = _wire(monkeypatch, mode="live",
                          failures=["token missing"], run=_boom)
    monkeypatch.setattr(module, "slack_token", _boom)
    monkeypatch.setattr(module, "build_deps", _boom)
    assert module.main() == 1
    assert calls.selftest == ["live"] and calls.run == []


def test_main_runs_no_tick_when_a_precondition_check_crashes(monkeypatch):
    """Under a cron, an exploding check has to fail closed like any other
    precondition rather than escaping as a traceback."""
    module, calls = _wire(monkeypatch, mode="live", run=_boom,
                          selftest_crashes=True)
    monkeypatch.setattr(module, "slack_token", _boom)
    monkeypatch.setattr(module, "build_deps", _boom)
    assert module.main() == 1
    assert calls.selftest == ["live"] and calls.run == []


def test_main_refuses_a_live_tick_without_the_bot_token(monkeypatch):
    module, calls = _wire(monkeypatch, mode="live", token="", run=_boom)
    monkeypatch.setattr(module, "build_deps", _boom)
    assert module.main() == 1
    assert calls.selftest == ["live"] and calls.run == []


def test_main_reports_the_tick_on_stdout_and_exits_zero(monkeypatch, capsys):
    module, calls = _wire(monkeypatch, mode="live", result=RunResult(
        mode="live", posted=("Ylopo/x#1@abc123",)))
    assert module.main() == 0
    assert "posted 1: Ylopo/x#1@abc123" in capsys.readouterr().out
    assert calls.selftest == ["live"] and calls.run == ["live"]


def test_main_says_nothing_at_all_on_a_quiet_healthy_tick(monkeypatch, capsys):
    """Empty stdout is what makes the deterministic cron silent."""
    module, calls = _wire(monkeypatch, mode="shadow", token="")
    assert module.main() == 0
    assert capsys.readouterr().out == ""
    assert calls.selftest == ["shadow"] and calls.run == ["shadow"]


def test_main_runs_the_tick_in_the_mode_the_config_asks_for(monkeypatch):
    """The pin Task 6's whole safety story rests on: "deploy in shadow first"
    is only true if the mode YAML declares is the mode both stages are handed.

    Asserted on the kwarg each stage RECEIVES, not on what the tick did with
    it: a hardcoded `mode="live"` leaves a shadow deployment posting for real
    with every other test in this file still green, because every one of them
    passes `run()` its mode directly.
    """
    for mode in ("off", "shadow", "live"):
        module, calls = _wire(monkeypatch, mode=mode)
        assert module.main() == 0
        assert calls.selftest == [mode], mode
        assert calls.run == [mode], mode


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

def _intake_config(tmp_path, **overrides):
    block = {
        "discovery_marker": MARKER,
        "discovery_bot_user_id": "U0B9TK6B41M",
        "loop_bot_user_id": "U0B9TK6B41M",
        "loop_attributed_user_id": "U07E3AHV686",
        "channels": ["C0BPCKPEFCG"],
        "passive_channels": ["C0BPCKPEFCG"],
    }
    block.update(overrides)
    lines = ["boris_intake:"]
    for key, value in block.items():
        if isinstance(value, list):
            lines.append(f"  {key}:")
            lines.extend(f"    - {item}" for item in value)
        else:
            lines.append(f"  {key}: '{value}'")
    path = tmp_path / "intake.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def _token(tmp_path, mode=0o600):
    path = tmp_path / ".gh_token"
    path.write_text("ghp_xxx\n")
    path.chmod(mode)
    return path


def test_selftest_passes_on_a_correctly_deployed_tick(tmp_path):
    assert run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path),
        config_path=tmp_path / "absent.yaml") == []


def test_selftest_fails_closed_on_a_missing_token(tmp_path):
    failures = run_selftest(
        mode="shadow", token_path=tmp_path / "nope",
        intake_config_path=_intake_config(tmp_path),
        config_path=tmp_path / "absent.yaml")
    assert any("missing" in f for f in failures)


def test_selftest_fails_closed_on_a_world_readable_token(tmp_path):
    failures = run_selftest(
        mode="shadow", token_path=_token(tmp_path, 0o644),
        intake_config_path=_intake_config(tmp_path),
        config_path=tmp_path / "absent.yaml")
    assert any("0o644" in f for f in failures)


def test_selftest_catches_a_marker_drift(tmp_path):
    """boris_intake's marker is YAML-deployed and this one is a literal, so
    nothing but this check keeps them equal -- and a drift makes every post
    land in the channel and be silently dropped by the gate."""
    failures = run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path,
                                          discovery_marker="[swept it. v2]"),
        config_path=tmp_path / "absent.yaml")
    assert any("marker" in f for f in failures)


def test_selftest_catches_an_inert_discovery_door(tmp_path):
    for override in ({"discovery_bot_user_id": ""},
                     {"loop_attributed_user_id": ""}):
        failures = run_selftest(
            mode="live", token_path=_token(tmp_path),
            intake_config_path=_intake_config(tmp_path, **override),
            config_path=tmp_path / "absent.yaml")
        assert failures, override


def test_selftest_catches_a_discovery_identity_that_is_not_the_posting_one(tmp_path):
    """This cron posts with the loop's token, so the two configured bot ids
    name one Slack user; a disagreement means every post is dropped."""
    failures = run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path,
                                          discovery_bot_user_id="U0SOMEONE"),
        config_path=tmp_path / "absent.yaml")
    assert any("loop_bot_user_id" in f for f in failures)


def test_selftest_catches_a_channel_the_gate_would_reject(tmp_path):
    failures = run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path, channels=["C0000000000"],
                                          passive_channels=["C0000000000"]),
        config_path=tmp_path / "absent.yaml")
    assert any("C0BPCKPEFCG" in f and "CHANNEL_NOT_ALLOWED" in f
               for f in failures)


def test_selftest_catches_a_channel_that_is_allowed_but_not_passive(tmp_path):
    """A non-passive channel demands an @mention this cron deliberately does
    not send, so every post would be rejected with MENTION_REQUIRED."""
    failures = run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(
            tmp_path, channels=["C0BPCKPEFCG", "C0BANB70BHR"],
            passive_channels=["C0BANB70BHR"]),
        config_path=tmp_path / "absent.yaml")
    assert any("passive" in f for f in failures)


def test_a_shadow_tick_is_not_blocked_by_gate_configuration(tmp_path):
    """Shadow posts nothing, so a gate that would reject our posts has no
    bearing on a shadow soak -- and refusing to run one would block the very
    comparison shadow exists for. Same mode-scoping as pr_remediation's
    autofix and reachability checks."""
    assert run_selftest(
        mode="shadow", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path, discovery_marker="drifted",
                                          discovery_bot_user_id=""),
        config_path=tmp_path / "absent.yaml") == []


def test_selftest_catches_a_config_key_that_looks_configurable_but_is_not(tmp_path):
    """`channel` and `marker` are module constants, not YAML settings. An
    operator who writes a different value into the block would otherwise get
    silence and no effect."""
    config = tmp_path / "config.yaml"
    config.write_text("pr_discovery:\n  mode: live\n  channel: C0DIFFERENT\n")
    failures = run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path), config_path=config)
    assert any("C0DIFFERENT" in f for f in failures)


def test_selftest_accepts_a_config_block_that_restates_the_constants(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pr_discovery:\n  mode: live\n"
                      "  channel: C0BPCKPEFCG\n"
                      f"  marker: '{MARKER}'\n"
                      "  interval_minutes: 30\n")
    assert run_selftest(
        mode="live", token_path=_token(tmp_path),
        intake_config_path=_intake_config(tmp_path), config_path=config) == []
