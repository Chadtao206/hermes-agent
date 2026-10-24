"""Operating configuration for the remediation loop."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
    from yaml import YAMLError
except ModuleNotFoundError:  # PyYAML left upstream's dependency set; hermes_yaml wraps ruamel
    import hermes_yaml as yaml
    from ruamel.yaml.error import YAMLError

DEFAULT_CONFIG = Path("/Users/ctao/.hermes/config.yaml")
VALID_MODES = ("shadow", "live", "off")


@dataclass(frozen=True)
class RemediationConfig:
    # shadow by default: the loop logs decisions and mutates nothing until a
    # human flips it, matching boris_intake and kanban_remediation.
    mode: str = "shadow"
    round_cap: int = 3
    marker: str = "[fixed it. probably. v1]"
    chad_slack_id: str = "U07E3AHV686"
    chad_github_login: str = "Chadtao206"
    boris_channel: str = "C0BPCKPEFCG"
    notify_channel: str = "C0BANB70BHR"
    owner_label: str = "hermes-owner-andrej"
    hold_label: str = "hermes-hold"
    poll_seconds: int = 15


def load_config(path: Path | None = None) -> RemediationConfig:
    # Only `mode` and `round_cap` are YAML-configurable. The remaining
    # fields (marker, chad_slack_id, chad_github_login, boris_channel,
    # notify_channel, owner_label, hold_label, poll_seconds) are
    # deliberately fixed defaults, even if someone writes them into the
    # YAML — do not add parsing for them here.
    path = path or DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError):
        return RemediationConfig()
    if not isinstance(data, dict):
        return RemediationConfig()
    block = data.get("pr_remediation")
    if not isinstance(block, dict):
        return RemediationConfig()

    mode = str(block.get("mode", "shadow")).strip().lower()
    if mode not in VALID_MODES:
        mode = "shadow"
    round_cap = block.get("round_cap", 3)
    if (
        not isinstance(round_cap, int)
        or isinstance(round_cap, bool)
        or not 1 <= round_cap <= 10
    ):
        round_cap = 3
    return RemediationConfig(mode=mode, round_cap=round_cap)
