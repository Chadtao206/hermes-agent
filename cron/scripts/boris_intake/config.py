"""Load and validate the ``boris_intake`` block from Hermes config.yaml.

Fails safe in every direction: a missing block, an unparseable file, or an
unrecognised mode all resolve to ``off``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
    from yaml import YAMLError
except ModuleNotFoundError:  # PyYAML left upstream's dependency set; hermes_yaml wraps ruamel
    import hermes_yaml as yaml
    from ruamel.yaml.error import YAMLError

HERMES_HOME = Path("/Users/ctao/.hermes")
DEFAULT_CONFIG = HERMES_HOME / "config.yaml"
VALID_MODES = ("off", "shadow", "live")

DEFAULT_CHANNELS = ("C08K8FQKX9S", "C07BU8AAPB3", "C09339EGZ9U")
DEFAULT_DOMAINS = ("ylopo.com", "avenga.com", "maverickre.com")
DEFAULT_TOKEN_PATH = HERMES_HOME / "state" / ".gh_token"


@dataclass(frozen=True)
class Limits:
    # Rate limiting is evaluated once per MESSAGE, so without a burst cap one
    # paste of N links would create N lanes on a single check. Real traffic
    # (30d): 273 of 277 PR-bearing messages carried <=5 links, but four genuine
    # batches ran 11-24 ("PLAT-2086 - 24 PRs for review"). 25 admits every
    # observed batch while bounding a runaway paste.
    max_prs_per_message: int = 25
    per_user_hourly: int = 30
    per_user_daily: int = 60
    global_concurrent_lanes: int = 6
    reject_reply_cooldown_seconds: int = 3600


@dataclass(frozen=True)
class Config:
    mode: str = "off"
    team_id: str = "T02PND103"
    bot_user_id: str = "U0B9TK6B41M"
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    passive_channels: tuple[str, ...] = DEFAULT_CHANNELS
    email_domains: tuple[str, ...] = DEFAULT_DOMAINS
    orgs: tuple[str, ...] = ("Ylopo",)
    github_token_path: Path = DEFAULT_TOKEN_PATH
    rubric_version: str = "2"
    # The remediation loop's re-review identity. All three default to "" so a
    # deployment that has not configured the loop behaves exactly as before.
    loop_bot_user_id: str = ""
    loop_marker: str = ""
    loop_attributed_user_id: str = ""
    # The discovery cron's own admitted post — a second, narrower door into the
    # same gate. Both default to "" so this branch is inert until a later task
    # deploys the value; see boris_intake/gate.py's _is_discovery_post for why
    # identity alone cannot separate it from Boris's own messages (the
    # discovery cron posts as the same bot user as Boris itself).
    discovery_marker: str = ""
    discovery_bot_user_id: str = ""
    # A teammate's bot -> the human who owns it. That bot's links are reviewed
    # as if the owner had posted them, so identity, quota and audit all resolve
    # to a real person.
    trusted_bots: dict[str, str] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)


def _tuple(raw, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        vals = tuple(str(v).strip() for v in raw if str(v).strip())
        return vals or fallback
    return fallback


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError):
        return Config()
    block = data.get("boris_intake")
    if not isinstance(block, dict):
        return Config()

    mode = str(block.get("mode", "off")).strip().lower()
    if mode not in VALID_MODES:
        mode = "off"

    channels = _tuple(block.get("channels"), DEFAULT_CHANNELS)
    # A passive channel that is not an allowlisted channel is meaningless —
    # gate check 2 would reject it first. Drop rather than silently widen.
    passive = tuple(c for c in _tuple(block.get("passive_channels"), channels)
                    if c in channels)

    raw_limits = block.get("limits") if isinstance(block.get("limits"), dict) else {}
    limits = Limits(**{k: int(v) for k, v in raw_limits.items()
                       if k in Limits.__dataclass_fields__})

    token_path = block.get("github_token_path")
    rubric_version = str(block.get("rubric_version") or "2").strip() or "2"
    loop_bot_user_id = str(block.get("loop_bot_user_id", "") or "").strip()
    loop_marker = str(block.get("loop_marker", "") or "").strip()
    loop_attributed_user_id = str(block.get("loop_attributed_user_id", "") or "").strip()
    discovery_marker = str(block.get("discovery_marker", "") or "").strip()
    discovery_bot_user_id = str(block.get("discovery_bot_user_id", "") or "").strip()
    raw_bots = block.get("trusted_bots") if isinstance(block.get("trusted_bots"), dict) else {}
    trusted_bots = {str(bot).strip(): str(owner).strip()
                    for bot, owner in raw_bots.items()
                    if str(bot).strip() and str(owner or "").strip()}
    return Config(
        mode=mode,
        team_id=str(block.get("team_id", "T02PND103")),
        bot_user_id=str(block.get("bot_user_id", "U0B9TK6B41M")),
        channels=channels,
        passive_channels=passive,
        email_domains=_tuple(block.get("email_domains"), DEFAULT_DOMAINS),
        orgs=_tuple(block.get("orgs"), ("Ylopo",)),
        github_token_path=(Path(str(token_path)).expanduser() if token_path
                           else DEFAULT_TOKEN_PATH),
        rubric_version=rubric_version,
        loop_bot_user_id=loop_bot_user_id,
        loop_marker=loop_marker,
        loop_attributed_user_id=loop_attributed_user_id,
        discovery_marker=discovery_marker,
        discovery_bot_user_id=discovery_bot_user_id,
        trusted_bots=trusted_bots,
        limits=limits,
    )
