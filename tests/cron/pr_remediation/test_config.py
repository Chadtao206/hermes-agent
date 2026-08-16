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
    # `stale` joined the vocabulary with I2: a pull request merged or closed
    # under the loop is terminal, but it is neither `ready` (which notifies a
    # human) nor `escalated` (which claims one must intervene).
    assert {s.value for s in LoopState} == {
        "observed", "remediating", "awaiting_head_move",
        "rereview_posted", "ready", "escalated", "stale",
    }


def test_stale_is_terminal_so_a_merged_pr_is_never_re_driven():
    from pr_remediation.models import TERMINAL_STATES

    assert LoopState.STALE in TERMINAL_STATES


def test_load_config_defaults_when_file_is_missing(tmp_path):
    path = tmp_path / "does-not-exist.yaml"
    assert load_config(path) == RemediationConfig()


def test_load_config_defaults_on_malformed_yaml_syntax(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation: [unclosed\n")
    assert load_config(path) == RemediationConfig()


def test_load_config_defaults_when_document_is_not_a_dict(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("just a bare scalar\n")
    assert load_config(path) == RemediationConfig()


def test_load_config_defaults_when_document_is_a_list(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- one\n- two\n")
    assert load_config(path) == RemediationConfig()


def test_load_config_defaults_when_pr_remediation_block_is_not_a_dict(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation: not-a-mapping\n")
    assert load_config(path) == RemediationConfig()


def test_load_config_defaults_round_cap_when_out_of_range(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  round_cap: 11\n")
    assert load_config(path).round_cap == 3


def test_load_config_defaults_round_cap_when_zero_or_negative(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  round_cap: 0\n")
    assert load_config(path).round_cap == 3


def test_load_config_rejects_bool_round_cap(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  round_cap: true\n")
    assert load_config(path).round_cap == 3


def test_load_config_defaults_round_cap_when_not_an_integer(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  round_cap: not-a-number\n")
    assert load_config(path).round_cap == 3


def test_load_config_honours_valid_in_range_round_cap(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_remediation:\n  round_cap: 7\n")
    assert load_config(path).round_cap == 7
