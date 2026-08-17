import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config, load_config
from boris_intake import models
from boris_intake.models import ReasonCode, SILENT_REASONS


def test_defaults_are_shadow_and_locked_down(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("boris_intake:\n  mode: shadow\n")
    cfg = load_config(cfg_file)
    assert cfg.mode == "shadow"
    assert cfg.team_id == "T02PND103"
    assert cfg.orgs == ("Ylopo",)
    assert cfg.email_domains == ("ylopo.com", "avenga.com", "maverickre.com")
    assert cfg.github_token_path.name == ".gh_token"
    assert cfg.rubric_version == "2"


def test_unknown_mode_falls_back_to_off(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("boris_intake:\n  mode: banana\n")
    assert load_config(cfg_file).mode == "off"


def test_missing_block_yields_off(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("other: {}\n")
    assert load_config(cfg_file).mode == "off"


def test_passive_channels_must_be_subset_of_channels(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "boris_intake:\n"
        "  mode: live\n"
        "  channels: [C1]\n"
        "  passive_channels: [C1, C2]\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.passive_channels == ("C1",)  # C2 dropped, not in channels


def test_silent_reasons_never_include_identity_rejection():
    assert ReasonCode.IDENTITY_NOT_TRUSTED not in SILENT_REASONS
    assert ReasonCode.CHANNEL_NOT_ALLOWED in SILENT_REASONS


def test_rubric_version_is_loaded_as_a_string(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("boris_intake:\n  mode: shadow\n  rubric_version: 3\n")

    assert load_config(cfg_file).rubric_version == "3"


def test_discovery_fields_default_to_empty_string_on_the_dataclass():
    """Pins the Config dataclass defaults directly (config.py:57-58), bypassing
    load_config entirely -- load_config always explicit-passes these two
    fields, so only a bare Config() exercises the class-level default."""
    cfg = Config()
    assert cfg.discovery_marker == ""
    assert cfg.discovery_bot_user_id == ""


def test_discovery_fields_absent_load_as_empty_even_when_loop_fields_are_set(tmp_path):
    """Mirrors TODAY'S LIVE config.yaml: loop_marker and bot_user_id are set,
    and there are no discovery_* keys at all. A `.get("loop_marker")` or
    `.get("bot_user_id")` copy-paste slip in the discovery loader would
    silently key the discovery door on the loop's own marker/bot id -- "absent
    must never mean open" is exactly the property this pins."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "boris_intake:\n"
        "  mode: live\n"
        "  loop_marker: '[fixed it. probably. v1]'\n"
        "  bot_user_id: U0B9TK6B41M\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.discovery_marker == ""
    assert cfg.discovery_bot_user_id == ""


def test_discovery_fields_round_trip_and_do_not_alias_the_loop_fields(tmp_path):
    """A YAML block containing both discovery_* keys, with values distinct from
    the loop_* keys, so a `.get("loop_marker")`/`.get("bot_user_id")` slip
    would make discovery_marker/discovery_bot_user_id equal the WRONG value
    and this fails."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "boris_intake:\n"
        "  mode: live\n"
        "  loop_marker: '[fixed it. probably. v1]'\n"
        "  bot_user_id: U0B9TK6B41M\n"
        "  discovery_marker: '[swept this one up. v1]'\n"
        "  discovery_bot_user_id: U0DISCOVERYBOT\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.discovery_marker == "[swept this one up. v1]"
    assert cfg.discovery_bot_user_id == "U0DISCOVERYBOT"
    assert cfg.discovery_marker != cfg.loop_marker
    assert cfg.discovery_bot_user_id != cfg.bot_user_id


def test_review_policy_has_stable_canonical_identity():
    normal = models.ReviewPolicy(ignore_ci=False, rubric_version="2")
    waived = models.ReviewPolicy(ignore_ci=True, rubric_version="2")

    assert normal.canonical_json() == '{"ignore_ci":false,"rubric_version":"2"}'
    assert normal.fingerprint() == (
        "sha256:eef25c2554a4acb3fbe3ce70422a3149c8cc37d1bdf22f1934fef38bea96c936"
    )
    assert waived.canonical_json() == '{"ignore_ci":true,"rubric_version":"2"}'
    assert waived.fingerprint() == (
        "sha256:a0d07e1a835ab382921fa38c92d1ddccaf62070e94d2b16fb90a12713f25774f"
    )
