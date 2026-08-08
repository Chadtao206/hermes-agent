from boris_intake.config import Config
from boris_intake.selftest import FORBIDDEN_PROBES, REFUSED_GH_VERBS, run_selftest


def test_probe_set_covers_every_class_of_bypass():
    joined = " ".join(FORBIDDEN_PROBES)
    assert "curl" in joined
    assert "bash -c" in joined
    assert "gh pr merge" in joined
    assert "/Users/ctao/.hermes/.env" in joined
    assert "workflow" in joined
    assert "gist" in joined


def test_selftest_reports_missing_token(hermetic_selftest):
    token_path, calls = hermetic_selftest
    cfg = Config(mode="live", github_token_path=token_path)
    failures = run_selftest(cfg)
    assert any("token" in f for f in failures)
    assert len(calls) == len(REFUSED_GH_VERBS)


def test_selftest_passes_with_temp_assets_and_injected_runner(hermetic_selftest):
    token_path, calls = hermetic_selftest
    token_path.write_text("test-token\n")
    token_path.chmod(0o600)

    failures = run_selftest(Config(mode="live", github_token_path=token_path))
    assert failures == [], failures
    assert [argv[1:] for argv, _ in calls] == [list(argv) for argv in REFUSED_GH_VERBS]
    assert all(kwargs["env"]["GHX_RO_DRY_RUN"] == "1" for _, kwargs in calls)
