#!/usr/bin/env python
"""One discovery tick: enumerate, select, and -- live only -- post.

*Routing decides; only a live tick acts.* The mode is not asked inside the
selection at all: `_plan` works out what a live tick would post and performs
none of it, and `_consumer` then names the one consumer this mode allows --
`_perform` (live) or `_rehearse` (paused). That single line is the whole of
this module's mode-awareness, which is the structural invariant
`pr_remediation/loop.py` arrived at after per-branch mode gates leaked three
separate times: a gate inside a branch is one deleted line away from a paused
tick posting, while a paused tick that never reaches `_perform` cannot post
whatever is added to it later. `off` goes one step further than `shadow` and
stops the work rather than merely the writes -- `_observes` is why an `off`
tick costs no GitHub reads at all.

*An empty tick is not automatically a healthy one.* `DiscoveryResult` reports
five separate incompleteness channels and this module surfaces all five,
because the shipped code this service replaces returned `[]` for a permanent
credential outage, which read exactly like a clean run for as long as anyone
cared to look. Only "nothing selected, nothing deferred, nothing failed, and
the enumeration was complete" produces an empty summary; every other shape
says something. Empty stdout is what makes a deterministic Hermes cron
silent, so silence here is a deliberate claim that there was nothing to say,
not the absence of a code path.

*The ledger is written only after Slack accepts.* A post that failed must
come back on the next tick, so the row goes in after the `ts` comes back and
never before. The intake gate's own dedup sits underneath that as a partial
backstop, not as the mechanism, and it is narrower than a per-head one: its
durable key is `(review_identity, gate_fingerprint)`, and the fingerprint is
a hash of the pull request's whole CI check list including each check's run
URL, so a CI re-run at an unchanged head admits as a NEW review rather than a
duplicate (see `pr_discovery/state.py`'s module docstring). Where it does hold
it only downgrades a second post to a duplicate, which still costs a real
message and a reject reply that a human reads.

Nothing in the posted message comes from this module -- see `post.py`, whose
docstring carries the security argument for why the message is a URL and a
marker and nothing else.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# The directory holding the `pr_discovery` package this file belongs to, so
# the imports below resolve the copy that is actually running: the repo
# checkout under pytest, the deployed tree under the cron runner, which starts
# this as a plain script and so puts only the package's own directory on
# sys.path. APPENDED, never prepended -- an appended entry can only supply a
# module nothing earlier on the path already provides, so it can never rebind a
# package a test runner has pointed somewhere else (the mistake
# `pr_remediation/daemon.py::_bootstrap_sys_path` documents at length).
_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.append(_PACKAGE_ROOT)

try:
    import yaml
    from yaml import YAMLError
except ModuleNotFoundError:  # PyYAML left upstream's dependency set; hermes_yaml wraps ruamel
    import hermes_yaml as yaml
    from ruamel.yaml.error import YAMLError

from pr_discovery.discover import (
    DEFAULT_TOKEN_PATH, DiscoveryResult, GhClient, IntakeSource, candidates,
)
from pr_discovery.post import (
    DISCOVERY_MARKER, INTAKE_CHANNEL, Poster, slug,
)
from pr_discovery.state import Ledger

DEFAULT_CONFIG = Path("/Users/ctao/.hermes/config.yaml")
DEFAULT_LEDGER_DB = Path("/Users/ctao/.hermes/state/pr_discovery.db")
#: `boris_intake`'s own database, read read-only by `IntakeSource` so this cron
#: never asks Boris to review a head Boris was already asked about. A path
#: pointing anywhere else would open fine and answer "never reviewed" for every
#: head forever -- the clause silently disabled with no degradation reported --
#: so `tests/cron/pr_discovery/test_main.py` pins it against `boris_intake`'s
#: own constant rather than a restated literal.
DEFAULT_INTAKE_DB = Path("/Users/ctao/.hermes/state/boris_intake.db")
#: `slack_read` (the deployed Slack transport) lives only here.
DEPLOYED_SCRIPTS = "/Users/ctao/.hermes/scripts"
VALID_MODES = ("off", "shadow", "live")

#: Whose pull requests this cron discovers, and where. Fixed constants rather
#: than YAML for the same reason `pr_remediation/config.py` fixes its channels
#: and marker: they are identity, not tuning, and a typo in either would send
#: reviews at the wrong person's work.
ORGS = ("Ylopo",)
AUTHOR = "Chadtao206"

#: Restated from `pr_remediation/lanes.py::_DEFAULT_EXCLUDED_REPOS` (a literal
#: rather than an import: this package also runs from its deployed copy, where
#: `pr_remediation` is not importable). `tests/cron/pr_discovery/test_main.py`
#: imports the real one and asserts they agree, so neither can drift.
_DEFAULT_EXCLUDED_REPOS = "Ylopo/ylopo-infra Ylopo/ylopo-github-actions-workflows"

#: How many pull requests one tick may post. Not a safety net against a bug --
#: a bound on a stampede. Every post the gate admits starts a Boris review
#: lane, each of which spends a model budget and ends in a review a human
#: reads, so what this caps is CONCURRENT REVIEWS: the first live tick meets a
#: whole backlog at once, and without a cap that backlog enters review in a
#: single burst rather than over an afternoon. Ten is small for that reason,
#: not for a quota one -- Chad's hourly intake limit
#: (`boris_intake.limits.per_user_hourly`) is 120 in the deployed config, and
#: at the planned 30-minute interval this cron asks for at most 20 an hour, so
#: the quota is not the binding constraint at any cap worth choosing. Nothing
#: is lost by deferring: an unposted pull request is simply not recorded, so it
#: is selected again on the next tick.
MAX_POSTS_PER_TICK = 10


@dataclass(frozen=True)
class DiscoveryConfig:
    """The only thing this service reads from YAML.

    `channel` and `marker` are deliberately NOT configurable -- they are
    module constants in `post.py`, because both must match `boris_intake`'s
    own deployed configuration byte-for-byte or every post is silently
    dropped, and a second place to write them is a second place for them to
    drift. `run_selftest` fails closed if the deployed block declares either
    of them differently, so an operator who believes they are settings finds
    out at startup rather than never.
    """

    mode: str = "shadow"


@dataclass(frozen=True)
class DiscoveryDeps:
    """Everything a tick touches, injected so a test wires fakes instead.

    `discover` is a zero-argument callable answering a `DiscoveryResult`;
    `slack` is a `post.Poster`; `ledger` is a `state.Ledger`. The mode is not
    here on purpose -- it is `run`'s own keyword, so a tick has exactly one
    mode and no way for two sources of it to disagree.
    """

    discover: Callable[[], object]
    ledger: object
    slack: object
    now: Callable[[], float]


@dataclass(frozen=True)
class Plan:
    """What a live tick would do, decided without doing any of it.

    `selected` holds candidates (the consumer needs the identity fields);
    everything else holds rendered slugs, which is all a report needs.
    """

    selected: tuple = ()
    skipped: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    failed_orgs: tuple[str, ...] = ()
    truncated_orgs: tuple[str, ...] = ()
    config_errors: tuple[str, ...] = ()
    unverified_heads: tuple[str, ...] = ()
    failed_pr_reads: tuple[str, ...] = ()
    enumerated: bool = False


@dataclass(frozen=True)
class RunResult:
    """What this tick did, and every way it might not be the whole story."""

    mode: str
    selected: tuple[str, ...] = ()
    posted: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed_posts: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    failed_orgs: tuple[str, ...] = ()
    truncated_orgs: tuple[str, ...] = ()
    config_errors: tuple[str, ...] = ()
    unverified_heads: tuple[str, ...] = ()
    failed_pr_reads: tuple[str, ...] = ()
    enumerated: bool = False

    @property
    def complete(self) -> bool:
        """True iff the enumeration behind this tick was the whole truth.

        Mirrors `DiscoveryResult.complete` exactly, and deliberately says
        nothing about posting: a failed post is a fault, but it is not a gap
        in what we SAW. Both reach a human through `summary`.
        """
        return not (self.failed_orgs or self.truncated_orgs
                    or self.config_errors or self.unverified_heads
                    or self.failed_pr_reads)

    @property
    def faults(self) -> tuple[str, ...]:
        """Every actionable sentence this tick owes a human."""
        out: list[str] = []
        if self.failed_posts:
            out.append(
                f"⚠️ {len(self.failed_posts)} post(s) failed and were NOT "
                f"recorded, so they return next tick: "
                + ", ".join(self.failed_posts))
        if self.deferred:
            out.append(
                f"⚠️ {len(self.deferred)} deferred over the per-tick cap of "
                f"{MAX_POSTS_PER_TICK} (not recorded; they return next tick): "
                + ", ".join(self.deferred))
        if self.failed_orgs:
            out.append(
                f"⚠️ {len(self.failed_orgs)} org(s) could not be enumerated at "
                f"all, so their pull requests were not seen this tick: "
                + ", ".join(self.failed_orgs))
        if self.truncated_orgs:
            out.append(
                "⚠️ listing truncated at the search limit for: "
                + ", ".join(self.truncated_orgs)
                + " — an unknown number of pull requests were never seen")
        if self.unverified_heads:
            # Reported like every other channel, but it did NOT withhold
            # anything -- these pull requests went out. Worded so a reader is
            # not left looking for pull requests that were held back.
            out.append(
                "⚠️ the already-reviewed check could not be applied, so this "
                "tick may repeat a review Boris has already done: "
                + "; ".join(self.unverified_heads))
        if self.failed_pr_reads:
            # Unlike `unverified_heads`, this one DID withhold: the pull
            # requests it counts never reached `selected` at all, and are
            # indistinguishable from ones that genuinely did not qualify.
            out.append(
                "⚠️ a per-PR read failed, so a pull request may have been "
                "silently dropped from consideration rather than genuinely "
                "not qualifying: " + "; ".join(self.failed_pr_reads))
        if self.config_errors:
            out.append(
                "⚠️ discovery could not be trusted, so nothing was posted: "
                + "; ".join(self.config_errors))
        return tuple(out)

    def summary(self) -> str:
        """The operator-facing report, or "" when there is genuinely nothing
        to say.

        Empty stdout is how a deterministic Hermes cron stays silent, so this
        returns "" only for the one shape that means a healthy quiet tick:
        nothing posted, nothing a live tick would have posted, and no fault.
        A tick whose candidates were all already posted at this head is quiet
        too -- that is the steady state, not news.
        """
        lines: list[str] = []
        if self.posted:
            lines.append(f"posted {len(self.posted)}: " + ", ".join(self.posted))
        if self.mode != "live" and self.selected:
            lines.append(f"{self.mode}: would post {len(self.selected)}: "
                         + ", ".join(self.selected))
        lines.extend(self.faults)
        if not lines:
            return ""
        if self.skipped:
            lines.append(f"({len(self.skipped)} already posted at this head, "
                         f"skipped)")
        return "\n".join(lines)


def _plan(deps: DiscoveryDeps) -> Plan:
    """What a live tick would post. Reads; never posts and never records."""
    result = deps.discover()
    if not isinstance(result, DiscoveryResult):
        # The shape `candidates()` returned before 5fa528f23d, or any other
        # non-conforming answer. Treated as a fault rather than iterated:
        # iterating it is exactly how a broken enumeration used to read as a
        # quiet healthy tick.
        return Plan(enumerated=True, config_errors=(
            f"discovery answered {type(result).__name__}, not a "
            "DiscoveryResult; nothing could be trusted from this pass",))
    if result.config_errors:
        # An untrustworthy parameter means we do not know what we are allowed
        # to post, so we post nothing -- even if candidates came back anyway.
        return Plan(enumerated=True, config_errors=result.config_errors,
                    failed_orgs=result.failed_orgs,
                    truncated_orgs=result.truncated_orgs,
                    unverified_heads=result.unverified_heads,
                    failed_pr_reads=result.failed_pr_reads)

    selected: list = []
    skipped: list[str] = []
    deferred: list[str] = []
    for candidate in result.candidates:
        if deps.ledger.already_posted(candidate.owner, candidate.repo,
                                      candidate.number, candidate.head_sha):
            # Already asked for a review of this exact head. The gate would
            # admit a repost as a duplicate, but a duplicate still costs a
            # message and a reject reply, which is the noise this ledger
            # exists to prevent.
            skipped.append(slug(candidate))
        elif len(selected) >= MAX_POSTS_PER_TICK:
            deferred.append(slug(candidate))
        else:
            selected.append(candidate)
    return Plan(selected=tuple(selected), skipped=tuple(skipped),
                deferred=tuple(deferred), failed_orgs=result.failed_orgs,
                truncated_orgs=result.truncated_orgs,
                unverified_heads=result.unverified_heads,
                failed_pr_reads=result.failed_pr_reads, enumerated=True)


def _from_plan(mode: str, plan: Plan, *, posted: tuple[str, ...] = (),
               failed_posts: tuple[str, ...] = ()) -> RunResult:
    return RunResult(
        mode=mode,
        selected=tuple(slug(candidate) for candidate in plan.selected),
        posted=posted, skipped=plan.skipped, failed_posts=failed_posts,
        deferred=plan.deferred, failed_orgs=plan.failed_orgs,
        truncated_orgs=plan.truncated_orgs, config_errors=plan.config_errors,
        unverified_heads=plan.unverified_heads,
        failed_pr_reads=plan.failed_pr_reads, enumerated=plan.enumerated)


def _perform(deps: DiscoveryDeps, plan: Plan, mode: str) -> RunResult:
    """Post what the plan selected. Reached only in live mode.

    One message per pull request, so one failure is one pull request's
    failure and the rest of the plan still goes out. The ledger row is
    written only after Slack hands back a `ts`: a post that did not land must
    be selected again next tick, and recording it first would silently retire
    a review that was never requested.
    """
    posted: list[str] = []
    failed: list[str] = []
    for candidate in plan.selected:
        if deps.slack.post(candidate):
            deps.ledger.record_posted(candidate.owner, candidate.repo,
                                      candidate.number, candidate.head_sha,
                                      now=deps.now())
            posted.append(slug(candidate))
        else:
            failed.append(slug(candidate))
    return _from_plan(mode, plan, posted=tuple(posted),
                      failed_posts=tuple(failed))


def _rehearse(deps: DiscoveryDeps, plan: Plan, mode: str) -> RunResult:
    """Report what a live tick would have done, and do none of it.

    The kill switch, honoured structurally: a paused tick decides and stops,
    so nothing added to `_perform` later can leak into it. It touches neither
    `deps.slack` nor `deps.ledger`'s writer -- there is no branch here that
    could, which is the difference between this and a mode check inside the
    posting loop.
    """
    return _from_plan(mode, plan)


def _consumer(mode: str) -> Callable[[DiscoveryDeps, Plan, str], RunResult]:
    """Which consumer this mode allows: the one place the mode is asked.

    `pr_remediation/loop.py::_consumer`'s counterpart, and kept in the same
    shape on purpose. `_perform` acts; `_rehearse` records and stops.
    """
    return _perform if mode == "live" else _rehearse


def _observes(mode: str) -> bool:
    """Whether this tick looks at GitHub at all.

    `off` is the hard kill switch, so it stops the work and not merely the
    writes: no org is enumerated. `shadow` keeps reading, because rehearsing
    a decision taken without the facts would not be rehearsing the decision a
    live tick takes.
    """
    return mode != "off"


def run(deps: DiscoveryDeps, *, mode: str) -> RunResult:
    """One tick.

    An unrecognised mode raises rather than defaulting: `load_config` already
    coerces anything unknown to `shadow`, so a bad value reaching here is a
    programming error, and guessing which mode was meant is how a tick that
    was supposed to be paused ends up posting.
    """
    if mode not in VALID_MODES:
        raise ValueError(
            f"unknown discovery mode {mode!r}; expected one of {VALID_MODES}")
    plan = _plan(deps) if _observes(mode) else Plan()
    return _consumer(mode)(deps, plan, mode)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def _declared_constant_failures(config_path: Path) -> list[str]:
    """Catch a `pr_discovery` block that declares a setting this service does
    not have. `channel` and `marker` are constants in `post.py`; a YAML value
    that differs from them has no effect at all, which is worse than being
    rejected -- the operator gets the behaviour they did not ask for and no
    sign that anything was ignored.
    """
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError, UnicodeDecodeError):
        # A config we cannot read is `load_config`'s problem (it fails safe to
        # shadow); there is nothing to compare here.
        return []
    block = data.get("pr_discovery") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return []
    failures = []
    for key, constant in (("channel", INTAKE_CHANNEL),
                          ("marker", DISCOVERY_MARKER)):
        declared = block.get(key)
        if declared is not None and str(declared) != constant:
            failures.append(
                f"config.yaml pr_discovery.{key} is {str(declared)!r}, but "
                f"{key} is a fixed constant ({constant!r}) and the YAML value "
                "has no effect; make it match or remove it")
    return failures


def _gate_failures(intake_config_path: Path) -> list[str]:
    """Would a post this cron sends actually be admitted?

    Every one of these makes `boris_intake`'s discovery door close silently:
    the message really lands in the channel, and is then dropped with no
    exception, no log line and nobody waiting on it. `boris_intake`'s half of
    each pairing is YAML-deployed and this half is a literal, so nothing but
    this check keeps them equal -- the same argument
    `pr_remediation/selftest.py` makes for its own marker-drift check, and the
    same reason it loads boris_intake's real deployed configuration rather
    than comparing a default against itself.
    """
    try:
        from boris_intake.config import load_config as load_intake_config
    except Exception as error:
        return [f"boris_intake is not importable ({error}), so the gate's "
                "discovery door cannot be verified"]
    cfg = load_intake_config(intake_config_path)
    failures = []
    if cfg.discovery_marker != DISCOVERY_MARKER:
        failures.append(
            f"marker drift: boris_intake.discovery_marker "
            f"{cfg.discovery_marker!r} (from {intake_config_path}) != "
            f"{DISCOVERY_MARKER!r}; every discovery post would be dropped as "
            "NOT_HUMAN")
    if not cfg.discovery_bot_user_id:
        failures.append(
            "boris_intake.discovery_bot_user_id is unset, so the gate's "
            "discovery door is inert and every post would be dropped as "
            "NOT_HUMAN")
    if not cfg.loop_attributed_user_id:
        failures.append(
            "boris_intake.loop_attributed_user_id is unset, so the gate's "
            "discovery door is inert and every post would be dropped as "
            "NOT_HUMAN")
    if (cfg.loop_bot_user_id and cfg.discovery_bot_user_id
            and cfg.loop_bot_user_id != cfg.discovery_bot_user_id):
        # This cron posts with TICKET_HUB_SLACK_BOT_TOKEN -- the same
        # identity `pr_remediation` posts its re-reviews with, because that is
        # the only identity that can reach the intake channel at all. So the
        # two configured bot ids describe one Slack user, and a disagreement
        # means one of them is wrong. Checked only when both are set, since an
        # unset loop id is a deployment that has not wired the loop yet.
        failures.append(
            f"boris_intake.discovery_bot_user_id {cfg.discovery_bot_user_id!r} "
            f"!= loop_bot_user_id {cfg.loop_bot_user_id!r}, but both name the "
            "identity TICKET_HUB_SLACK_BOT_TOKEN posts as; one is wrong, and a "
            "wrong one drops every discovery post as NOT_HUMAN")
    if INTAKE_CHANNEL not in cfg.channels:
        failures.append(
            f"{INTAKE_CHANNEL} is not in boris_intake.channels; every post "
            "would be rejected as CHANNEL_NOT_ALLOWED")
    elif INTAKE_CHANNEL not in cfg.passive_channels:
        failures.append(
            f"{INTAKE_CHANNEL} is not one of boris_intake.passive_channels; a "
            "bare link there is rejected as MENTION_REQUIRED, and this cron "
            "deliberately posts no mention")
    return failures


def run_selftest(*, mode: str, token_path: Path = DEFAULT_TOKEN_PATH,
                 intake_config_path: Path | None = None,
                 config_path: Path = DEFAULT_CONFIG) -> list[str]:
    """Fail-closed preconditions for a tick, as a list of sentences.

    The token check runs in every mode: `GhClient` answers `None` for an
    unreadable credential, which `candidates()` reports as a failed org --
    correct, but indistinguishable tick after tick from GitHub being down,
    for as long as nobody goes looking. Its siblings both check this file at
    startup (`pr_remediation/selftest.py`, `boris_intake/selftest.py`) and
    discovery is the one that did not.

    The gate-compatibility checks are live-only, the same mode-scoping
    `pr_remediation/selftest.py` gives its autofix and reachability checks and
    for the same reason: a paused tick posts nothing, so a gate that would
    reject our posts has no bearing on a shadow soak -- and refusing to run
    one would block the comparison a shadow soak exists to produce.
    """
    from boris_intake.config import DEFAULT_CONFIG as INTAKE_DEFAULT_CONFIG

    failures: list[str] = []
    token_path = Path(token_path)
    if not token_path.is_file():
        failures.append(f"GitHub token is missing: {token_path}")
    else:
        file_mode = oct(token_path.stat().st_mode & 0o777)
        if file_mode != "0o600":
            failures.append(
                f"GitHub token at {token_path} has mode {file_mode}, want 0o600")
    failures.extend(_declared_constant_failures(Path(config_path)))
    if mode == "live":
        failures.extend(_gate_failures(
            Path(intake_config_path if intake_config_path is not None
                 else INTAKE_DEFAULT_CONFIG)))
    return failures


# ---------------------------------------------------------------------------
# Configuration and wiring
# ---------------------------------------------------------------------------

def load_config(path: Path | None = None) -> DiscoveryConfig:
    """The `pr_discovery` block. Fails safe to shadow in every direction.

    Only `mode` is read. See `DiscoveryConfig` for why `channel` and `marker`
    are not, and `_declared_constant_failures` for what happens if the
    deployed block declares them anyway.
    """
    path = Path(path) if path is not None else DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError, UnicodeDecodeError):
        return DiscoveryConfig()
    if not isinstance(data, dict):
        return DiscoveryConfig()
    block = data.get("pr_discovery")
    if not isinstance(block, dict):
        return DiscoveryConfig()
    mode = str(block.get("mode", "shadow")).strip().lower()
    if mode not in VALID_MODES:
        mode = "shadow"
    return DiscoveryConfig(mode=mode)


def banned_repos() -> tuple[str, ...]:
    """The excluded `owner/name` list, SPLIT.

    `PR_REVIEW_EXCLUDED_REPOS` is a space-separated string, and handing it to
    `candidates(banned_repos=...)` unsplit would make the ban a substring
    test -- see that function's docstring, which names this exact trap. Read
    live off the environment rather than cached at import, matching
    `lanes._excluded_repos()`.
    """
    raw = os.environ.get("PR_REVIEW_EXCLUDED_REPOS") or _DEFAULT_EXCLUDED_REPOS
    return tuple(raw.split())


def slack_caller(token: str, method: str, **params):
    """The deployed `slack_call`. A wrapper because `slack_read` exists only
    under DEPLOYED_SCRIPTS, so importing it at module scope would make this
    module unimportable anywhere else -- and it keeps a seam tests can patch.
    """
    from slack_read import slack_call as _slack_call

    return _slack_call(token, method, **params)


def slack_token() -> str:
    """Son of Chad's token (`TICKET_HUB_SLACK_BOT_TOKEN`).

    The same identity `pr_remediation` posts review-thread work under, and it
    has to be: the gate admits a discovery post only from
    `cfg.discovery_bot_user_id`, and that is this bot's user id. The main
    Hermes bot cannot reach the intake channel at all (different workspace).
    """
    from slack_read import ENV_PATH, parse_env

    return parse_env(ENV_PATH).get("TICKET_HUB_SLACK_BOT_TOKEN", "")


def build_deps(token: str, *, ledger_db: Path = DEFAULT_LEDGER_DB,
               token_path: Path = DEFAULT_TOKEN_PATH,
               intake_db: Path = DEFAULT_INTAKE_DB,
               caller=None) -> DiscoveryDeps:
    """Wire the real adapters. A seam so tests wire fakes instead."""
    gh = GhClient(token_path=token_path)
    reviewed = IntakeSource(intake_db)
    return DiscoveryDeps(
        discover=lambda: candidates(gh, orgs=ORGS, author=AUTHOR,
                                    banned_repos=banned_repos(),
                                    reviewed=reviewed),
        ledger=Ledger(ledger_db),
        slack=Poster(token, caller if caller is not None else slack_caller),
        now=time.time,
    )


def main(argv=None) -> int:
    """Run one tick and report it.

    Exit code is about whether the tick could run, not about what it found: a
    failed org or a refused post is reported on stdout (which the cron
    delivers) and still exits 0, while a precondition failure exits nonzero
    without touching Slack or GitHub. A quiet, healthy tick prints nothing,
    which is what makes a deterministic Hermes cron silent.
    """
    if DEPLOYED_SCRIPTS not in sys.path:
        sys.path.append(DEPLOYED_SCRIPTS)
    cfg = load_config()
    try:
        failures = run_selftest(mode=cfg.mode)
    except Exception as error:
        # A precondition check that itself explodes (boris_intake missing from
        # the deployed tree, an exotic config error the checked loaders do not
        # catch) must be reported the same way any other precondition failure
        # is, not escape as a raw traceback from an unattended cron.
        sys.stderr.write(f"pr_discovery: selftest crashed: {error}\n")
        return 1
    if failures:
        for failure in failures:
            sys.stderr.write(f"pr_discovery: selftest: {failure}\n")
        return 1
    token = slack_token()
    if cfg.mode == "live" and not token:
        sys.stderr.write("pr_discovery: TICKET_HUB_SLACK_BOT_TOKEN is missing\n")
        return 1
    result = run(build_deps(token), mode=cfg.mode)
    summary = result.summary()
    if summary:
        sys.stdout.write(summary + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
