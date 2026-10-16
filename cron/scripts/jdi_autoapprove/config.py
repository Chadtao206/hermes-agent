"""Load and validate the ``jdi_autoapprove`` block from Hermes config.yaml.

Fails safe in every direction: a missing block, an unparseable file, or an
unrecognised mode all resolve to ``off``. Deliberately mirrors
``boris_intake.config`` in shape so ``boris_intake.identity.is_trusted`` can
consume this Config unchanged — it reads only ``team_id`` and ``email_domains``.

The channel list is NOT defaulted to anything. An auto-approver that falls back
to a built-in channel list on a typo'd config would approve PRs in channels the
operator did not name; an empty list is the only safe fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

HERMES_HOME = Path("/Users/ctao/.hermes")
DEFAULT_CONFIG = HERMES_HOME / "config.yaml"
VALID_MODES = ("off", "shadow", "live")
DEFAULT_TOKEN_PATH = HERMES_HOME / "state" / ".gh_token"

DEFAULT_BODY = (
    "Auto-approved on posting to the intake channel. This is a policy stamp, "
    "not a line-by-line review."
)


@dataclass(frozen=True)
class Limits:
    # Per-message burst cap: the rate check runs once per MESSAGE, so without
    # this one paste of N links would approve N PRs on a single check. Matches
    # the figure boris_intake settled on from 30d of real traffic.
    max_prs_per_message: int = 25
    per_user_hourly: int = 30
    per_user_daily: int = 60
    # Runaway breaker. Observed volume across the three channels is ~9/day, so
    # 100 is >10x headroom while still bounding a loop or a link flood. On trip
    # the daemon approves nothing further that day and says so in the audit.
    global_daily: int = 100


@dataclass(frozen=True)
class Config:
    mode: str = "off"
    team_id: str = "T02PND103"
    channels: tuple[str, ...] = ()
    email_domains: tuple[str, ...] = ("ylopo.com", "avenga.com", "maverickre.com")
    orgs: tuple[str, ...] = ("Ylopo",)
    reaction: str = "blinkapproved"
    failure_reaction: str = "warning"
    approval_body: str = DEFAULT_BODY
    skip_if_already_approved: bool = True
    github_token_path: Path = DEFAULT_TOKEN_PATH
    limits: Limits = field(default_factory=Limits)


def _tuple(raw, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        vals = tuple(str(v).strip() for v in raw if str(v).strip())
        return vals or fallback
    return fallback


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return Config()
    block = data.get("jdi_autoapprove")
    if not isinstance(block, dict):
        return Config()

    mode = str(block.get("mode", "off")).strip().lower()
    if mode not in VALID_MODES:
        mode = "off"

    # No fallback: an unreadable/absent channel list means approve nowhere.
    channels = _tuple(block.get("channels"), ())
    if not channels:
        mode = "off"

    raw_limits = block.get("limits") if isinstance(block.get("limits"), dict) else {}
    limits = Limits(**{k: int(v) for k, v in raw_limits.items()
                       if k in Limits.__dataclass_fields__})

    token_path = block.get("github_token_path")
    body = str(block.get("approval_body") or DEFAULT_BODY).strip()

    return Config(
        mode=mode,
        team_id=str(block.get("team_id") or Config.team_id),
        channels=channels,
        email_domains=_tuple(block.get("email_domains"), Config.email_domains),
        orgs=_tuple(block.get("orgs"), Config.orgs),
        reaction=str(block.get("reaction") or Config.reaction).strip().strip(":"),
        failure_reaction=str(
            block.get("failure_reaction") or Config.failure_reaction).strip().strip(":"),
        approval_body=body,
        skip_if_already_approved=bool(block.get("skip_if_already_approved", True)),
        github_token_path=Path(token_path) if token_path else DEFAULT_TOKEN_PATH,
        limits=limits,
    )
