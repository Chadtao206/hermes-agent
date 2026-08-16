from pr_remediation.config import RemediationConfig, load_config
from pr_remediation.models import LoopState


def test_config_defaults_to_shadow_so_a_fresh_install_never_writes():
    assert RemediationConfig().mode == "shadow"


def test_config_carries_the_agreed_operating_values():
    cfg = RemediationConfig()
    assert cfg.round_cap == 3
    assert cfg.marker == "[fixed it. probably. v1]"
    assert cfg.chad_slack_id == "U07E3AHV686"
    assert cfg.chad_github_login == "Chadtao206"
    assert cfg.boris_channel == "C0BPCKPEFCG"
    assert cfg.notify_channel == "C0BANB70BHR"


def test_unknown_mode_falls_back_to_shadow(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  mode: banana\n")
    assert load_config(path).mode == "shadow"


def test_live_mode_is_honoured(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  mode: live\n")
    assert load_config(path).mode == "live"


def test_states_cover_the_whole_loop():
    assert {s.value for s in LoopState} == {
        "observed", "remediating", "awaiting_head_move",
        "rereview_posted", "ready", "escalated",
    }
