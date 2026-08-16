#!/usr/bin/env python
"""Remediation loop daemon. Polls Boris verdicts and advances loops.

Runs under a launchd KeepAlive supervisor, which sets the shape of both
failure paths here. Startup failures (a missing write token, a marker gone
out of sync with boris_intake, a missing autofix gate in live mode) must fail
closed: `main` refuses to build any dependency and exits nonzero rather than
starting degraded, because a daemon that starts anyway would sit there
posting nothing, dispatching nothing, and telling nobody why. Once running,
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

sys.path.insert(0, "/Users/ctao/.hermes/scripts")
sys.path.insert(0, "/Users/ctao/.hermes/hermes-agent")

from slack_read import ENV_PATH, parse_env, slack_call  # noqa: E402

from pr_remediation.boris_source import BorisSource  # noqa: E402
from pr_remediation.config import RemediationConfig, load_config  # noqa: E402
from pr_remediation.github_io import GitHubIO  # noqa: E402
from pr_remediation.lanes import LaneIO  # noqa: E402
from pr_remediation.loop import LoopDeps, tick  # noqa: E402
from pr_remediation.selftest import DEFAULT_BORIS_DB, DEFAULT_TOKEN, run_selftest  # noqa: E402
from pr_remediation.slack_io import SlackIO  # noqa: E402
from pr_remediation.store import Store  # noqa: E402

STATE_DB = Path("/Users/ctao/.hermes/state/pr_remediation.db")
HEARTBEAT = Path("/Users/ctao/.hermes/state/pr_remediation_heartbeat")
KANBAN_DB = Path("/Users/ctao/.hermes/kanban.db")


def build_deps(cfg: RemediationConfig, slack_token: str) -> LoopDeps:
    """Wire the real adapters. A seam so tests can wire fakes instead."""
    return LoopDeps(
        source=BorisSource(DEFAULT_BORIS_DB, cfg),
        github=GitHubIO(cfg, DEFAULT_TOKEN),
        slack=SlackIO(cfg, slack_token, slack_call),
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
        except Exception as error:  # noqa: BLE001 - a bad tick must not kill the loop
            sys.stderr.write(f"pr_remediation: tick error: {error}\n")
            sys.stderr.flush()
        heartbeat.write_text(f"{time.time():.0f}")
        sleep(deps.cfg.poll_seconds)


def main() -> int:
    cfg = load_config()
    if cfg.mode == "off":
        sys.stderr.write("pr_remediation: mode=off, exiting\n")
        return 0

    failures = run_selftest(cfg)
    if failures:
        for failure in failures:
            sys.stderr.write(f"pr_remediation: selftest: {failure}\n")
        return 1

    env = parse_env(ENV_PATH)
    token = env.get("TICKET_HUB_SLACK_BOT_TOKEN", "")
    if not token:
        sys.stderr.write("pr_remediation: TICKET_HUB_SLACK_BOT_TOKEN is missing\n")
        return 1

    deps = build_deps(cfg, token)
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
