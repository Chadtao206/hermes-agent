import sqlite3

from boris_intake.config import Config
from boris_intake.selftest import ALLOWED_GH_VERBS, REFUSED_GH_VERBS, run_selftest


def test_wrapper_probes_cover_read_and_refused_write_routes():
    allowed = " ".join(" ".join(argv) for argv in ALLOWED_GH_VERBS)
    refused = " ".join(" ".join(argv) for argv in REFUSED_GH_VERBS)
    assert all(verb in allowed for verb in ("pr view", "pr diff", "pr checks", "api"))
    assert all(verb in refused for verb in (
        "workflow", "gist", "pr merge", "pr review", "pr comment"
    ))


def test_selftest_reports_missing_token(hermetic_selftest):
    cfg = Config(mode="live", github_token_path=hermetic_selftest.token_path)
    failures = run_selftest(cfg)
    assert any("token" in f for f in failures)
    assert len(hermetic_selftest.calls) == (
        len(ALLOWED_GH_VERBS) + len(REFUSED_GH_VERBS)
    )


def test_selftest_passes_with_temp_assets_and_injected_runner(hermetic_selftest):
    hermetic_selftest.token_path.write_text("test-token\n")
    hermetic_selftest.token_path.chmod(0o600)

    failures = run_selftest(
        Config(mode="live", github_token_path=hermetic_selftest.token_path)
    )

    assert failures == [], failures
    assert [argv[1:] for argv, _ in hermetic_selftest.calls] == [
        *[list(argv) for argv in ALLOWED_GH_VERBS],
        *[list(argv) for argv in REFUSED_GH_VERBS],
    ]
    assert all(
        kwargs["env"]["GHX_RO_DRY_RUN"] == "1"
        for _, kwargs in hermetic_selftest.calls
    )
    connection = sqlite3.connect(hermetic_selftest.db_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    connection.close()


def test_selftest_fails_closed_on_invalid_registry(hermetic_selftest):
    hermetic_selftest.token_path.write_text("test-token\n")
    hermetic_selftest.token_path.chmod(0o600)
    hermetic_selftest.registry_path.write_text("{}")

    failures = run_selftest(
        Config(mode="live", github_token_path=hermetic_selftest.token_path)
    )

    assert any("gate registry" in failure for failure in failures)


def test_selftest_fails_closed_on_unsealed_profile(hermetic_selftest):
    hermetic_selftest.token_path.write_text("test-token\n")
    hermetic_selftest.token_path.chmod(0o600)
    hermetic_selftest.profile_path.write_text("toolsets: []\n")

    failures = run_selftest(
        Config(mode="live", github_token_path=hermetic_selftest.token_path)
    )

    assert any("profile" in failure for failure in failures)


def test_selftest_fails_closed_without_worker_task_binding(hermetic_selftest):
    hermetic_selftest.token_path.write_text("test-token\n")
    hermetic_selftest.token_path.chmod(0o600)
    profile = hermetic_selftest.profile_path.read_text()
    hermetic_selftest.profile_path.write_text(
        profile.replace(
            "      BORIS_KANBAN_TASK: ${HERMES_KANBAN_TASK}\n", ""
        )
    )

    failures = run_selftest(
        Config(mode="live", github_token_path=hermetic_selftest.token_path)
    )

    assert any("task binding" in failure for failure in failures)
