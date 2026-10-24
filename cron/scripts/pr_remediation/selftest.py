"""Fail-closed preconditions for the remediation daemon.

Two of these checks reach outside this package on purpose, because a failure
in either would otherwise be invisible everywhere in this service.

The gate checks tie this service to boris_intake's own deployed
configuration. boris_intake's gate (`gate.py::_is_loop_repost`) admits a
re-review post from this loop only if FOUR separate things about that
configuration hold, and every one of them is deployed independently, in
boris_intake's own block of config.yaml, of the defaults this service holds
in `config.py`. Nothing else keeps them in sync, and every one of them fails
the same way: boris_intake silently drops every re-review this loop posts
(SlackIO still gets a `ts` back -- the post really lands, in a channel, it is
simply never read as a loop repost) and the loop stalls waiting for a verdict
that will never arrive, with no exception, no log line, nothing. All four
rejection reasons are in boris_intake's `SILENT_REASONS`, so there is not even
a reply in the thread:

| drift | how the gate rejects the re-review |
|---|---|
| `loop_marker` differs by one character | `not_human` |
| `loop_bot_user_id` unset | `not_human` |
| `loop_attributed_user_id` unset | `not_human` |
| the review channel left `passive_channels` | `thread_reply_no_mention` |
| the review channel left `channels` | `channel_not_allowed` |

The `passive_channels` one is the least obvious and entirely live:
`SlackIO.post_rereview` deliberately carries no `<@bot>` mention, so gate check
1b rejects it the moment that channel stops being passive.

An earlier attempt guarded the marker from inside this package alone and only
checked this service's own default against itself -- a tautology that could
never catch a real drift -- and was removed. The only checks that can catch
the real failures are ones that load boris_intake's own deployed
configuration, the same file boris_intake itself reads, and compare it
against what this service is about to use. `pr_discovery/main.py::_gate_failures`
makes exactly this argument for the gate's *other* door and checks the same
shapes; see `_gate_failures` below for why the two are not one function.

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
    except Exception as error:
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


def _gate_failures(cfg: RemediationConfig,
                   intake_config_path: Path) -> list[str]:
    """Would a re-review this loop posts actually be admitted?

    Every one of these makes boris_intake's loop door close silently -- see the
    module docstring's table. `boris_intake`'s half of each pairing is
    YAML-deployed and this half is a fixed default in `config.py`, so nothing
    but this check keeps them equal.

    Deliberately NOT shared with `pr_discovery/main.py::_gate_failures`, which
    asks the same five questions of the same config for the *discovery* door.
    A shared helper would have to be imported by one cron package from the
    other -- coupling two separately deployed artifacts, which
    `pr_discovery/post.py`'s docstring already refuses to do for a single
    constant -- or moved into `boris_intake`, making the reviewed service carry
    its consumers' startup checks. Both packages instead read boris_intake's
    real deployed config and phrase their own findings, which is the property
    that matters: neither can pass by agreeing with itself.

    What this cannot check is a `loop_bot_user_id` that is set but names the
    wrong Slack user. Only Slack knows which user `TICKET_HUB_SLACK_BOT_TOKEN`
    posts as, and answering that would mean a network call in a startup gate
    whose whole discipline is failing closed only on proven negatives (see the
    reachability check below). `pr_discovery`'s own gate check catches the
    subset that matters in practice -- the two configured bot ids naming one
    identity and disagreeing.

    Unconditional, in every mode, unlike its `pr_discovery` counterpart: this
    service's marker check has always run in every mode, and a deployment that
    has not wired the loop door already fails it in shadow (boris_intake's
    `loop_marker` defaults to `""`, which can never equal this service's
    non-empty default). Scoping the other four to live would put two different
    mode rules inside one function for no gain.
    """
    intake = load_boris_intake_config(intake_config_path)
    failures: list[str] = []
    if intake.loop_marker != cfg.marker:
        failures.append(
            f"marker drift: boris_intake.loop_marker {intake.loop_marker!r} "
            f"(from {intake_config_path}) != pr_remediation.marker "
            f"{cfg.marker!r}; boris_intake would silently drop every "
            "re-review this loop posts")
    if not intake.loop_bot_user_id:
        failures.append(
            f"boris_intake.loop_bot_user_id is unset (from {intake_config_path}), "
            "so the gate's loop door is inert and every re-review would be "
            "dropped as NOT_HUMAN")
    if not intake.loop_attributed_user_id:
        failures.append(
            f"boris_intake.loop_attributed_user_id is unset (from "
            f"{intake_config_path}), so the gate's loop door is inert and every "
            "re-review would be dropped as NOT_HUMAN")
    if cfg.boris_channel not in intake.channels:
        failures.append(
            f"{cfg.boris_channel} is not in boris_intake.channels (from "
            f"{intake_config_path}); every re-review would be rejected as "
            "CHANNEL_NOT_ALLOWED")
    elif cfg.boris_channel not in intake.passive_channels:
        # Checked only when the channel IS allowlisted: boris_intake's own
        # config loader drops a passive channel that is not an allowlisted one,
        # so the two findings would otherwise both fire and say the same thing
        # twice.
        failures.append(
            f"{cfg.boris_channel} is not one of boris_intake.passive_channels "
            f"(from {intake_config_path}); SlackIO.post_rereview deliberately "
            "carries no bot mention, so every re-review would be rejected as "
            "THREAD_REPLY_NO_MENTION")
    return failures


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
    # the same way boris_intake itself reads it (load_config fails safe to an
    # unconfigured Config on a missing or malformed file -- every field of
    # which fails one of the checks below, so an unreadable boris_intake config
    # surfaces here rather than being silently swallowed by boris_intake's own
    # fail-safe defaults).
    failures.extend(_gate_failures(cfg, boris_intake_config_path))

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
