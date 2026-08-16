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


def _env_duplicated(tmp_path, *values):
    """An .env with PR_REVIEW_AUTOFIX repeated, in the given order -- e.g. an
    operator appending a fresh line below a stale one rather than editing it
    in place, which is exactly what kanban_dispatch.sh's `tail -1` is there to
    handle correctly."""
    env = tmp_path / ".env"
    env.write_text("".join(f"PR_REVIEW_AUTOFIX={v}\n" for v in values))
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


def test_autofix_gate_last_duplicate_wins_when_the_last_value_closes_it(tmp_path):
    """kanban_dispatch.sh reads PR_REVIEW_AUTOFIX with `grep ... | tail -1` --
    last match in the file wins, so a stale `=1` earlier in .env followed by
    the operator's current `=0` must read as CLOSED here too. A selftest that
    stopped at the first match would report the gate open while
    kanban_dispatch.sh refuses every lane -- the exact divergence this check
    exists to prevent."""
    failures = run_selftest(
        RemediationConfig(mode="live"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path),
        env_path=_env_duplicated(tmp_path, "1", "0"),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert any("autofix" in f.lower() for f in failures)


def test_autofix_gate_last_duplicate_wins_when_the_last_value_opens_it(tmp_path):
    """Same duplicate-key file, opposite order: last value `=1` must read as
    OPEN, not just "not the first value". Together with the test above, this
    pins last-wins rather than merely not-first-wins."""
    failures = run_selftest(
        RemediationConfig(mode="live"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path),
        env_path=_env_duplicated(tmp_path, "0", "1"),
        boris_intake_config_path=_boris_intake_config(tmp_path))
    assert failures == []


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


# --- Slack reachability: two identities, two workspaces --------------------
#
# Discovered by hand against the real deployment: Son of Chad
# (TICKET_HUB_SLACK_BOT_TOKEN) cannot join #pr-curator because that channel
# lives in a different Slack workspace, and cross-workspace membership does
# not exist. In shadow nothing posts, so the failure is invisible; at live
# every ready notice and escalation would fail with not_in_channel and
# vanish. These tests cover: each pairing is checked, a definitive negative
# fails closed, an inconclusive result does not, and shadow skips the check
# entirely (so a soak alone can never surface a broken channel).

REVIEW_CHANNEL = RemediationConfig().boris_channel
NOTIFY_CHANNEL = RemediationConfig().notify_channel


def _healthy_live_kwargs(tmp_path):
    """Every other live-mode precondition satisfied, so a test can isolate
    the reachability check as the only thing left to fail (or not) on."""
    return dict(
        token_path=_token(tmp_path), boris_db=_boris_db(tmp_path),
        env_path=_env(tmp_path, autofix="1"),
        boris_intake_config_path=_boris_intake_config(tmp_path))


def _member_response():
    return {"ok": True, "channel": {"is_member": True}}


def test_reachability_check_fails_closed_when_the_review_token_is_not_a_member(tmp_path):
    def caller(token, method, **params):
        if params.get("channel") == REVIEW_CHANNEL:
            return {"ok": True, "channel": {"is_member": False}}
        return _member_response()

    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert any("review token" in f.lower() for f in failures)


def test_reachability_check_fails_closed_when_the_notify_token_cannot_see_the_channel(tmp_path):
    def caller(token, method, **params):
        if params.get("channel") == NOTIFY_CHANNEL:
            return {"ok": False, "error": "channel_not_found"}
        return _member_response()

    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert any("notify token" in f.lower() for f in failures)


def test_reachability_check_does_not_fail_on_a_network_error(tmp_path):
    """Constraint 1: an unknown must never be read as a failure. A daemon
    under KeepAlive that refused to start on a transient Slack outage would
    crash-loop forever -- strictly worse than the silent-vanish bug this
    check exists to catch."""
    def caller(token, method, **params):
        raise TimeoutError("slack.com timed out")

    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert failures == []


def test_reachability_check_does_not_fail_on_an_inconclusive_slack_error(tmp_path):
    """An error that is not itself proof of a membership problem (here: rate
    limiting) must not be conflated with a definitive not_in_channel."""
    def caller(token, method, **params):
        return {"ok": False, "error": "ratelimited"}

    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert failures == []


def test_reachability_check_does_not_fail_when_is_member_is_simply_absent(tmp_path):
    """An ok response that doesn't carry is_member at all (a scope that
    permits reading channel metadata but not membership, say) answers
    nothing about membership and must not be treated as a negative."""
    def caller(token, method, **params):
        return {"ok": True, "channel": {}}

    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert failures == []


def test_reachability_check_is_skipped_in_shadow_mode(tmp_path):
    """Shadow never posts, so a broken pairing has no bearing on a shadow
    soak -- and, symmetrically, a shadow soak can never surface one. The
    caller here would fail the check if it were ever invoked; asserting it
    was not is what proves the skip is real rather than coincidental."""
    calls = []

    def caller(token, method, **params):
        calls.append((token, params.get("channel")))
        return {"ok": True, "channel": {"is_member": False}}

    failures = run_selftest(
        RemediationConfig(mode="shadow"), token_path=_token(tmp_path),
        boris_db=_boris_db(tmp_path),
        boris_intake_config_path=_boris_intake_config(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert failures == []
    assert calls == []


def test_reachability_check_checks_both_token_channel_pairings(tmp_path):
    """The review token must be checked against the review channel and the
    notify token against the notify channel -- not, say, both against one
    channel, or the same token checked twice."""
    calls = []

    def caller(token, method, **params):
        calls.append((token, params.get("channel")))
        return _member_response()

    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok", slack_caller=caller)
    assert failures == []
    assert set(calls) == {("rtok", REVIEW_CHANNEL), ("ntok", NOTIFY_CHANNEL)}


def test_reachability_check_is_a_noop_without_an_injected_caller(tmp_path):
    """No caller means nothing can be probed. A wiring gap is not itself the
    proven-bad configuration this check exists to catch, so live mode must
    not crash or fail closed on it."""
    failures = run_selftest(
        RemediationConfig(mode="live"), **_healthy_live_kwargs(tmp_path),
        review_token="rtok", notify_token="ntok")
    assert failures == []
