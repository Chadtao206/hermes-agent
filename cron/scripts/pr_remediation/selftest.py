"""Fail-closed preconditions for the remediation daemon.

Two of these checks reach outside this package on purpose, because a failure
in either would otherwise be invisible everywhere in this service.

The marker check ties this service's `cfg.marker` to boris_intake's own
`loop_marker`. boris_intake's gate (`gate.py::_is_loop_repost`) admits a
re-review post from this loop only when the message carries boris_intake's
configured marker literal, verbatim -- but that marker is deployed
independently, in boris_intake's own block of config.yaml, of the default
this service holds in `config.py`. Nothing else keeps the two in sync: if
they ever differ by a single character, boris_intake silently drops every
re-review this loop posts (SlackIO still gets a `ts` back -- the post really
lands, in a channel, it is simply never read as a loop repost) and the loop
stalls waiting for a verdict that will never arrive, with no exception, no
log line, nothing. An earlier attempt guarded this from inside this package
alone and only checked this service's own default against itself -- a
tautology that could never catch a real drift -- and was removed. The only
check that can catch the real failure is one that loads boris_intake's own
deployed configuration, the same file boris_intake itself reads, and compares
it against what this service is about to use.

The autofix check guards `kanban_dispatch.sh`'s independent refusal to create
any lane unless `PR_REVIEW_AUTOFIX=1` is set in the live `.env`. That gate is
outside this service's control entirely; with it closed, every remediation
round would escalate `lane_failed` with a cause invisible from here. Checked
only in live mode: shadow never dispatches a lane, so the gate has no bearing
on a shadow soak, and reporting it there would just be a red herring blocking
an otherwise-healthy rollout.
"""
from __future__ import annotations

from pathlib import Path

from boris_intake.config import DEFAULT_CONFIG as BORIS_INTAKE_DEFAULT_CONFIG
from boris_intake.config import load_config as load_boris_intake_config

from pr_remediation.config import VALID_MODES, RemediationConfig

DEFAULT_TOKEN = Path("/Users/ctao/.hermes/state/.gh_write_token")
DEFAULT_BORIS_DB = Path("/Users/ctao/.hermes/state/boris_intake.db")
DEFAULT_ENV = Path("/Users/ctao/.hermes/.env")


def _autofix_open(env_path: Path) -> bool:
    """kanban_dispatch.sh reads this same line to decide whether to allow create."""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("PR_REVIEW_AUTOFIX="):
                return line.split("=", 1)[1].strip().strip('"\'') == "1"
    except OSError:
        return False
    return False


def run_selftest(cfg: RemediationConfig, *, token_path: Path = DEFAULT_TOKEN,
                 boris_db: Path = DEFAULT_BORIS_DB,
                 env_path: Path = DEFAULT_ENV,
                 boris_intake_config_path: Path = BORIS_INTAKE_DEFAULT_CONFIG,
                 ) -> list[str]:
    failures: list[str] = []
    if not token_path.is_file():
        failures.append(f"GitHub write token is missing: {token_path}")
    else:
        mode = oct(token_path.stat().st_mode & 0o777)
        if mode != "0o600":
            failures.append(f"GitHub write token at {token_path} has mode {mode}, want 0o600")
    if not boris_db.is_file():
        failures.append(f"Boris review database is missing: {boris_db}")
    if cfg.mode not in VALID_MODES:
        failures.append(f"unknown mode {cfg.mode}")
    if not 1 <= cfg.round_cap <= 10:
        failures.append(f"round_cap {cfg.round_cap} is out of range")
    if not cfg.marker:
        failures.append("loop marker is empty; re-reviews would never be admitted")

    # The real runtime sync point: boris_intake's OWN deployed config, read
    # the same way boris_intake itself reads it (load_config fails safe to
    # loop_marker="" on a missing or malformed file, which can never equal
    # this service's non-empty default -- so an unreadable boris_intake
    # config surfaces here as a mismatch rather than being silently
    # swallowed by boris_intake's own fail-safe default).
    boris_marker = load_boris_intake_config(boris_intake_config_path).loop_marker
    if boris_marker != cfg.marker:
        failures.append(
            f"marker drift: boris_intake.loop_marker {boris_marker!r} "
            f"(from {boris_intake_config_path}) != pr_remediation.marker "
            f"{cfg.marker!r}; boris_intake would silently drop every "
            "re-review this loop posts")

    if cfg.mode == "live" and not _autofix_open(env_path):
        failures.append(
            "PR_REVIEW_AUTOFIX is not 1 in .env; kanban_dispatch.sh will refuse "
            "every remediation lane")
    return failures
