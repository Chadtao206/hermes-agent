import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import load_config
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
