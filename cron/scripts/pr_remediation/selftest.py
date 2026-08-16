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

The Slack reachability check exists because the failure it catches is
otherwise invisible everywhere, the same class as the marker check above: a
token that cannot reach the channel it is meant to post to fails at
`chat.postMessage` with `not_in_channel`, and `SlackIO._post` turns that into
a bare `None` -- no exception, no log line a human is watching, nothing.
Discovered exactly that way, by hand, against the real deployment: Son of
Chad (`TICKET_HUB_SLACK_BOT_TOKEN`) cannot be a member of `#pr-curator`
because that channel lives in a different Slack workspace, and cross-
workspace channel membership does not exist -- no scope or invite fixes it.
`SlackIO` now uses two tokens for exactly this reason (see slack_io.py's
module docstring); this check is what would have caught the mismatch before
a human had to find it by hand.

Two constraints shape it, both load-bearing under a KeepAlive supervisor:

1. It fails closed only on a *proven* negative, never on an unknown.
   `conversations.info` either answers definitively -- the channel is not
   visible to this token at all (`channel_not_found`), or it is visible and
   the token's bot is positively not a member (`is_member: false`) -- or it
   does not. A raised exception, a malformed response, or any Slack error
   that is not itself proof of a membership problem (rate limiting, a scope
   issue, an auth problem, a transient outage) is logged and treated as
   inconclusive, not as a failure. Slack being unreachable at startup is not
   the failure this check exists to catch, and refusing to start over a
   network blip would turn a transient outage into a KeepAlive crash loop --
   strictly worse than the silent-vanish failure this check exists to
   prevent.
2. It never posts. `chat.postMessage` would prove membership too, but at the
   cost of a probe message landing in a real team channel on every daemon
   start. `conversations.info` is read-only and answers the same question.

Checked only in live mode, the same scoping as the autofix gate: shadow never
posts, so a broken pairing has no bearing on a shadow soak -- which also
means a shadow soak, however long, can never surface a broken channel. Going
live is the only thing that asks this question.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from boris_intake.config import DEFAULT_CONFIG as BORIS_INTAKE_DEFAULT_CONFIG
from boris_intake.config import load_config as load_boris_intake_config

from pr_remediation.config import VALID_MODES, RemediationConfig

DEFAULT_TOKEN = Path("/Users/ctao/.hermes/state/.gh_write_token")
DEFAULT_BORIS_DB = Path("/Users/ctao/.hermes/state/boris_intake.db")
DEFAULT_ENV = Path("/Users/ctao/.hermes/.env")

_LOG = logging.getLogger(__name__)

# Read-only: reveals membership without posting anything. See the module
# docstring's constraint 2.
_MEMBERSHIP_METHOD = "conversations.info"

# The only Slack errors that are themselves proof a token cannot reach a
# channel -- not proof of anything else (a scope problem, an auth problem, a
# rate limit) that merely looks the same from here. See constraint 1.
_DEFINITIVE_UNREACHABLE_ERRORS = {"channel_not_found"}


def _autofix_open(env_path: Path) -> bool:
    """kanban_dispatch.sh reads this same line to decide whether to allow create,
    via `grep ... | tail -1` -- last match wins, so the operator's current .env
    value (not an earlier stale duplicate) is authoritative. The sibling parser
    in ylopo_pr_readiness/gather.py::mode_live mirrors the same rule. Do not
    return on the first match here: that would silently disagree with the gate
    this check exists to reflect."""
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    value = None
    for line in lines:
        if line.startswith("PR_REVIEW_AUTOFIX="):
            value = line.split("=", 1)[1].strip().strip('"\'')
    return value == "1"


def _channel_membership(caller: Callable[..., Any], token: str,
                        channel: str) -> bool | None:
    """Ask Slack, read-only, whether `token`'s bot is a member of `channel`.

    True: it is. False: Slack gave a *definitive* negative -- the channel is
    not visible to this token at all, or it answered and the bot is
    positively not a member. None: anything else, and None must never be
    read as a failure -- see the module docstring's constraint 1.
    """
    try:
        response = caller(token, _MEMBERSHIP_METHOD, channel=channel)
    except Exception as error:  # noqa: BLE001 - inconclusive, not a failure
        _LOG.info("pr_remediation selftest: reachability check for %s "
                  "raised %r; treating as inconclusive", channel, error)
        return None
    if not isinstance(response, dict):
        _LOG.info("pr_remediation selftest: reachability check for %s "
                  "returned a non-dict response %r; treating as "
                  "inconclusive", channel, response)
        return None
    if response.get("ok"):
        channel_info = response.get("channel")
        is_member = (channel_info.get("is_member")
                    if isinstance(channel_info, dict) else None)
        if is_member is False:
            return False
        if is_member is True:
            return True
        _LOG.info("pr_remediation selftest: reachability check for %s did "
                  "not report is_member; treating as inconclusive", channel)
        return None
    error = response.get("error")
    if error in _DEFINITIVE_UNREACHABLE_ERRORS:
        return False
    _LOG.info("pr_remediation selftest: reachability check for %s got %r; "
              "not a definitive membership answer, treating as "
              "inconclusive", channel, error)
    return None


def _slack_reachability_failures(cfg: RemediationConfig, review_token: str,
                                 notify_token: str,
                                 caller: Callable[..., Any] | None,
                                 ) -> list[str]:
    """Would every post through either token/channel pairing actually land?

    Checks both pairings this service relies on: the review token against
    the review channel (Son of Chad, `#boris-reviews`), and the notify token
    against the notify channel (the main Hermes bot, `#pr-curator`). Read-
    only and never posts -- see the module docstring.

    `caller` is None when nothing was wired in to make the check with (the
    daemon always supplies the real one; only a caller that hasn't been
    updated for the two-token contract would leave it unset). That is a
    wiring gap, not a proven-bad channel, so it is treated the same as any
    other inconclusive result: nothing to check, nothing failed.
    """
    if caller is None:
        return []
    failures: list[str] = []
    for label, token, channel in (
        ("review", review_token, cfg.boris_channel),
        ("notify", notify_token, cfg.notify_channel),
    ):
        if not token:
            # The separate missing-token check in daemon.main() already
            # fails closed on this; an empty token has nothing for a
            # reachability probe to add.
            continue
        if _channel_membership(caller, token, channel) is False:
            failures.append(
                f"{label} token cannot reach {channel}: Slack reports the "
                "channel is not visible to it, or it is not a member -- "
                "every post through this pairing would fail with "
                "not_in_channel and vanish silently")
    return failures


def run_selftest(cfg: RemediationConfig, *, token_path: Path = DEFAULT_TOKEN,
                 boris_db: Path = DEFAULT_BORIS_DB,
                 env_path: Path = DEFAULT_ENV,
                 boris_intake_config_path: Path = BORIS_INTAKE_DEFAULT_CONFIG,
                 review_token: str = "", notify_token: str = "",
                 slack_caller: Callable[..., Any] | None = None,
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

    # Same mode-scoping as the autofix gate above, and for the same reason:
    # shadow never posts, so this has no bearing on a shadow soak. See the
    # module docstring's third section.
    if cfg.mode == "live":
        failures.extend(_slack_reachability_failures(
            cfg, review_token, notify_token, slack_caller))
    return failures
