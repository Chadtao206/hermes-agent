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


def test_marker_matches_boris_intakes_loop_marker_byte_for_byte():
    """Drift guard: boris_intake's `loop_marker` config value (see
    cron/scripts/boris_intake/config.py) must equal this literal exactly.
    The two packages keep independent copies of the same marker on purpose
    (this task does not restructure either package to share a constant),
    so nothing else enforces agreement between them. If someone edits one
    copy without the other, boris_intake's gate stops recognising the
    remediation loop's re-review posts, admits nothing, and the loop
    stalls silently with no error. Break this test, then go update the
    other copy too.
    """
    assert RemediationConfig().marker == "[fixed it. probably. v1]"


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
