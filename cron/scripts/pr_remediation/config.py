"""Operating configuration for the remediation loop."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

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
    path = path or DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return RemediationConfig()
    block = data.get("pr_remediation")
    if not isinstance(block, dict):
        return RemediationConfig()

    mode = str(block.get("mode", "shadow")).strip().lower()
    if mode not in VALID_MODES:
        mode = "shadow"
    round_cap = block.get("round_cap", 3)
    if not isinstance(round_cap, int) or not 1 <= round_cap <= 10:
        round_cap = 3
    return RemediationConfig(mode=mode, round_cap=round_cap)
