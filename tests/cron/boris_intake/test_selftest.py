import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config
from boris_intake.selftest import FORBIDDEN_PROBES, run_selftest


def test_probe_set_covers_every_class_of_bypass():
    joined = " ".join(FORBIDDEN_PROBES)
    assert "curl" in joined
    assert "bash -c" in joined
    assert "gh pr merge" in joined
    assert "/Users/ctao/.hermes/.env" in joined
    assert "workflow" in joined
    assert "gist" in joined


def test_selftest_reports_missing_token(tmp_path):
    cfg = Config(mode="live", github_token_path=tmp_path / "nope")
    failures = run_selftest(cfg)
    assert any("token" in f for f in failures)


def test_selftest_passes_on_a_healthy_install():
    # Runs against the real installed guard and wrapper.
    failures = run_selftest(Config(mode="live"))
    assert failures == [], failures
