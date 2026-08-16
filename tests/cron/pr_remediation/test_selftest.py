"""Fail-closed preconditions for the remediation daemon.

Two checks reach outside this package on purpose, and both are covered here
against fakes rather than the real deployment:

* the marker sync check, which loads boris_intake's own config.yaml block and
  refuses to start unless its loop_marker matches this service's marker
  byte-for-byte -- the two are otherwise never tied together, and a drift
  between them makes re-review posts vanish with no error anywhere;
* the autofix gate check, which mirrors kanban_dispatch.sh's independent
  refusal to create a lane unless PR_REVIEW_AUTOFIX=1 is set in the live
  .env, checked only in live mode since shadow never dispatches a lane.
"""
import json

from pr_remediation.config import RemediationConfig
from pr_remediation.selftest import run_selftest

MATCHING_MARKER = RemediationConfig().marker


def _boris_intake_config(tmp_path, marker=MATCHING_MARKER, mode="live"):
    path = tmp_path / "boris_config.yaml"
    # json.dumps produces a double-quoted YAML flow scalar (JSON is a valid
    # YAML subset), which sidesteps YAML's own quoting rules entirely -- safe
    # for any marker text this test throws at it.
    path.write_text(
        "boris_intake:\n"
        f"  mode: {mode}\n"
        f"  loop_marker: {json.dumps(marker)}\n"
    )
    return path


def _token(tmp_path, mode=0o600):
    token = tmp_path / "tok"
    token.write_text("x")
    token.chmod(mode)
    return token


def _boris_db(tmp_path):
    db = tmp_path / "boris.db"
    db.write_text("")
    return db


def _env(tmp_path, autofix="1"):
    env = tmp_path / ".env"
    env.write_text(f"PR_REVIEW_AUTOFIX={autofix}\n")
    return env


def test_selftest_fails_closed_on_a_missing_github_token(tmp_path):
    failures = run_selftest(
        RemediationConfig(), token_path=tmp_path / "absent",
        boris_db=tmp_path / "absent.db",
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("token" in f.lower() for f in failures)


def test_selftest_fails_closed_on_a_missing_boris_database(tmp_path):
    failures = run_selftest(
        RemediationConfig(), token_path=_token(tmp_path),
        boris_db=tmp_path / "absent.db",
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("boris" in f.lower() for f in failures)


def test_selftest_rejects_a_world_readable_token(tmp_path):
    failures = run_selftest(
        RemediationConfig(), token_path=_token(tmp_path, 0o644),
        boris_db=_boris_db(tmp_path),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("mode" in f.lower() for f in failures)


def test_live_mode_requires_the_autofix_gate_to_be_open(tmp_path):
    """kanban_dispatch.sh refuses create unless PR_REVIEW_AUTOFIX=1, and a closed
    gate would otherwise look like every lane mysteriously failing."""
    failures = run_selftest(
        RemediationConfig(mode="live"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path), env_path=_env(tmp_path, autofix="0"),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("autofix" in f.lower() for f in failures)


def test_shadow_mode_does_not_need_the_autofix_gate(tmp_path):
    failures = run_selftest(
        RemediationConfig(mode="shadow"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path), env_path=_env(tmp_path, autofix="0"),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert failures == []


def test_a_healthy_deployment_reports_no_failures(tmp_path):
    failures = run_selftest(
        RemediationConfig(mode="live"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path), env_path=_env(tmp_path, autofix="1"),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert failures == []


def test_selftest_fails_closed_when_boris_intake_marker_differs(tmp_path):
    """The real runtime sync point: boris_intake's gate admits a re-review
    post only when it carries boris_intake's OWN configured marker verbatim.
    If this service's marker and boris_intake's loop_marker ever diverge, the
    loop posts re-reviews boris_intake silently drops -- no exception, no log
    line, just a stalled loop. Must be caught before the daemon starts."""
    failures = run_selftest(
        RemediationConfig(mode="live"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path), env_path=_env(tmp_path, autofix="1"),
        boris_intake_config_path=_boris_intake_config(
            tmp_path, marker="[some other marker]"))
    assert any("marker" in f.lower() for f in failures)


def test_selftest_fails_closed_when_boris_intake_config_is_unreadable(tmp_path):
    """boris_intake.config.load_config fails safe to loop_marker="" on a
    missing or malformed file. An empty marker can never equal this
    service's non-empty default, so this must surface as a failure rather
    than being silently swallowed by boris_intake's own fail-safe default."""
    failures = run_selftest(
        RemediationConfig(mode="live"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path), env_path=_env(tmp_path, autofix="1"),
        boris_intake_config_path=tmp_path / "does-not-exist.yaml")
    assert any("marker" in f.lower() for f in failures)


def test_selftest_passes_when_boris_intake_marker_matches_exactly(tmp_path):
    failures = run_selftest(
        RemediationConfig(mode="shadow"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path),
        boris_intake_config_path=_boris_intake_config(
            tmp_path, marker=MATCHING_MARKER))
    assert failures == []


def test_selftest_fails_closed_on_an_empty_marker(tmp_path):
    from dataclasses import replace
    cfg = replace(RemediationConfig(), marker="")
    failures = run_selftest(
        cfg, token_path=_token(tmp_path), boris_db=_boris_db(tmp_path),
        boris_intake_config_path=_boris_intake_config(tmp_path, marker=""))
    assert any("marker" in f.lower() for f in failures)


def test_selftest_fails_closed_on_an_unknown_mode(tmp_path):
    from dataclasses import replace
    cfg = replace(RemediationConfig(), mode="degraded")
    failures = run_selftest(
        cfg, token_path=_token(tmp_path), boris_db=_boris_db(tmp_path),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("mode" in f.lower() for f in failures)


def test_selftest_fails_closed_on_a_round_cap_out_of_range(tmp_path):
    from dataclasses import replace
    cfg = replace(RemediationConfig(), round_cap=0)
    failures = run_selftest(
        cfg, token_path=_token(tmp_path), boris_db=_boris_db(tmp_path),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("round_cap" in f.lower() for f in failures)
