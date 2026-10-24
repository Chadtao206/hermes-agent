#!/usr/bin/env python
"""Remediation loop daemon. Polls Boris verdicts and advances loops.

Runs under a launchd KeepAlive supervisor, which sets the shape of both
failure paths here. Startup failures (a missing write token, a missing Slack
token for either identity, a marker gone out of sync with boris_intake, a
missing autofix gate in live mode, a Slack token/channel pairing that
`run_selftest` can prove is unreachable in live mode) must fail closed:
`main` refuses to build any dependency and exits nonzero rather than
starting degraded, because a daemon that starts anyway would sit there
posting nothing, dispatching nothing, and telling nobody why. That includes a
precondition check that itself raises (an exotic config error the checked
loaders don't catch, e.g. a non-UTF-8 byte) -- `main` reports it the same way
as any other precondition failure rather than letting it escape as a raw
traceback for KeepAlive to loop on. Once running,
the opposite must hold: a single bad tick -- one exploding adapter, one
malformed row -- must not take the whole process down, because KeepAlive
would just restart it into the same failure a moment later. `_run` is the
seam that keeps that promise: every tick is wrapped, and the loop goes on.
"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from typing import Callable

# The directory holding the `pr_remediation` package this file belongs to,
# derived from __file__ rather than named, so the imports below always resolve
# the copy that is actually running -- the repo checkout under pytest, the
# deployed tree under launchd, which starts this as a plain script
# (`python .../pr_remediation/daemon.py`) and so puts only the package's own
# directory on sys.path. APPENDED, never prepended: an appended entry can only
# supply a module nothing earlier on the path already provides, so it can never
# rebind a package a test runner has pointed somewhere else. See
# `_bootstrap_sys_path` for why that distinction is the whole of this module's
# path discipline.
_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.append(_PACKAGE_ROOT)

from pr_remediation.boris_source import BorisSource
from pr_remediation.config import RemediationConfig, load_config
from pr_remediation.github_io import GitHubIO
from pr_remediation.lanes import LaneIO
from pr_remediation.loop import LoopDeps, tick
from pr_remediation.selftest import DEFAULT_BORIS_DB, DEFAULT_TOKEN, run_selftest
from pr_remediation.slack_io import SlackIO
from pr_remediation.store import Store

STATE_DB = Path("/Users/ctao/.hermes/state/pr_remediation.db")
HEARTBEAT = Path("/Users/ctao/.hermes/state/pr_remediation_heartbeat")
KANBAN_DB = Path("/Users/ctao/.hermes/kanban.db")

# Deployed-only roots. `slack_read` lives under DEPLOYED_SCRIPTS and nowhere in
# the repo, which is why it is imported inside functions below rather than at
# module scope.
DEPLOYED_SCRIPTS = "/Users/ctao/.hermes/scripts"
HERMES_AGENT = "/Users/ctao/.hermes/hermes-agent"

#: Which `.env` `parse_env` reads. None means "slack_read's own ENV_PATH", so
#: that location stays defined in exactly one place rather than being copied
#: here where it could drift out of sync with the module that owns it.
ENV_PATH: Path | None = None


def _bootstrap_sys_path() -> None:
    """Put the deployed helper roots on sys.path. Called from `main()` only.

    This used to run at module scope, prepending DEPLOYED_SCRIPTS, and that
    silently disabled a sibling service's regression tests. DEPLOYED_SCRIPTS
    holds a deployed copy of `boris_intake`; the repo holds its own under
    `cron/scripts`. Prepending therefore rebound the whole `boris_intake`
    package for the rest of the process the moment pytest merely *collected*
    `tests/cron/pr_remediation/test_daemon.py` -- so in any combined run, the
    boris_intake tests were exercising the deployed copy, and a defect
    introduced in the repo's copy could not fail them.

    Importing this module must have no global effect, so the mutation happens
    here, where the process has already committed to being the daemon. It also
    appends rather than prepends: an appended entry can only ever supply a
    module nothing earlier on the path provides, so even called from a test it
    cannot rebind anything.
    """
    for root in (DEPLOYED_SCRIPTS, HERMES_AGENT):
        if root not in sys.path:
            sys.path.append(root)


def parse_env(path: Path | None = None) -> dict[str, str]:
    """The deployed `.env`, read through `slack_read`.

    A wrapper rather than a re-export because `slack_read` exists only under
    DEPLOYED_SCRIPTS: importing it at module scope is precisely what forced the
    sys.path mutation this module no longer takes at import time. Keeping the
    name at module scope keeps the seam the daemon tests already patch.
    """
    from slack_read import ENV_PATH as SLACK_READ_ENV_PATH
    from slack_read import parse_env as _parse_env

    return _parse_env(SLACK_READ_ENV_PATH if path is None else path)


def slack_caller(token: str, method: str, **params):
    """The deployed `slack_call`, used both for `run_selftest`'s reachability
    check and, via `build_deps`, for every real post and reaction once the
    daemon is up.

    A wrapper for the same reason `parse_env` is one: `slack_read` lives only
    under DEPLOYED_SCRIPTS, so importing it at module scope is exactly what
    forced this module's sys.path mutation out of import time. Keeping the
    name at module scope keeps the seam tests can patch.
    """
    from slack_read import slack_call as _slack_call

    return _slack_call(token, method, **params)


def build_deps(cfg: RemediationConfig, review_token: str,
               notify_token: str) -> LoopDeps:
    """Wire the real adapters. A seam so tests can wire fakes instead.

    Two tokens, not one: `review_token` (Son of Chad,
    TICKET_HUB_SLACK_BOT_TOKEN) owns the review-thread work SlackIO performs,
    `notify_token` (the main Hermes bot, SLACK_BOT_TOKEN) owns the
    notifications -- the two channels live in different Slack workspaces, so
    one identity cannot cover both. See slack_io.py's module docstring for
    why the split has to be by call, not by config.
    """
    return LoopDeps(
        source=BorisSource(DEFAULT_BORIS_DB, cfg),
        github=GitHubIO(cfg, DEFAULT_TOKEN),
        slack=SlackIO(cfg, review_token, notify_token, slack_caller),
        lanes=LaneIO(KANBAN_DB),
        store=Store(STATE_DB),
        cfg=cfg,
        now=time.time,
    )


def _run(deps: LoopDeps, *, should_stop: Callable[[], bool],
         sleep: Callable[[float], None] = time.sleep,
         heartbeat: Path = HEARTBEAT) -> None:
    """The poll loop. A tick that raises is logged and the loop goes on --
    this is what stands between one bad verdict and a KeepAlive restart loop
    that would just walk into the same exception again."""
    while not should_stop():
        try:
            tick(deps)
        except Exception as error:
            sys.stderr.write(f"pr_remediation: tick error: {error}\n")
            sys.stderr.flush()
        heartbeat.write_text(f"{time.time():.0f}", encoding="utf-8")
        sleep(deps.cfg.poll_seconds)


def main() -> int:
    _bootstrap_sys_path()
    cfg = load_config()
    if cfg.mode == "off":
        sys.stderr.write("pr_remediation: mode=off, exiting\n")
        return 0

    # Both Slack identities come from the same `.env`, read once here so the
    # selftest's reachability check (live mode only) can probe the exact
    # tokens the daemon is about to build SlackIO with -- not a value it
    # reads separately and could drift from.
    env = parse_env(ENV_PATH)
    review_token = env.get("TICKET_HUB_SLACK_BOT_TOKEN", "")
    notify_token = env.get("SLACK_BOT_TOKEN", "")

    try:
        failures = run_selftest(cfg, review_token=review_token,
                                notify_token=notify_token,
                                slack_caller=slack_caller)
    except Exception as error:
        # load_boris_intake_config only catches OSError/yaml.YAMLError; something
        # like a UnicodeDecodeError from a non-UTF-8 config file would otherwise
        # propagate straight out of main() as a raw traceback. Under a KeepAlive
        # supervisor that just loops the same crash forever with no clean signal
        # -- report it the same way every other precondition failure is reported.
        sys.stderr.write(f"pr_remediation: selftest crashed: {error}\n")
        return 1
    if failures:
        for failure in failures:
            sys.stderr.write(f"pr_remediation: selftest: {failure}\n")
        return 1

    if not review_token:
        sys.stderr.write("pr_remediation: TICKET_HUB_SLACK_BOT_TOKEN is missing\n")
        return 1
    if not notify_token:
        sys.stderr.write("pr_remediation: SLACK_BOT_TOKEN is missing\n")
        return 1

    deps = build_deps(cfg, review_token, notify_token)
    sys.stderr.write(f"pr_remediation: started, mode={cfg.mode}\n")
    sys.stderr.flush()

    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    _run(deps, should_stop=lambda: stopping)
    return 0


if __name__ == "__main__":
    sys.exit(main())
