"""Tests for the just-do-it / vulcan / maverickre auto-approve pipeline.

Every test that matters here is a NEGATIVE test: this thing posts public
approvals on other people's PRs, so the valuable assertions are the ones
proving it stays quiet.
"""
from __future__ import annotations

import sys
from types import ModuleType
from pathlib import Path

import pytest

SOURCE_SCRIPTS = Path(__file__).resolve().parents[2] / "cron" / "scripts"


def forbidden_external_call(*args, **kwargs):
    raise AssertionError("External Slack/credential access is forbidden in this source test")


slack_stub = ModuleType("slack_read")
slack_stub.ENV_PATH = Path("/nonexistent/jdi-test-env")
slack_stub.parse_env = forbidden_external_call
slack_stub.slack_call = forbidden_external_call
previous_path = sys.path[:]
missing_module = object()
previous_slack = sys.modules.get("slack_read", missing_module)
try:
    sys.path.insert(0, str(SOURCE_SCRIPTS))
    sys.modules["slack_read"] = slack_stub
    from boris_intake.models import Identity, NormalizedEvent  # noqa: E402

    from jdi_autoapprove import github, poll  # noqa: E402
    from jdi_autoapprove.config import Config, Limits, load_config  # noqa: E402
    from jdi_autoapprove.gate import GateDeps, evaluate  # noqa: E402
    from jdi_autoapprove.models import PRFacts, ReasonCode  # noqa: E402
    from jdi_autoapprove.slack_io import SlackIO  # noqa: E402
    from jdi_autoapprove.store import Store  # noqa: E402
finally:
    sys.path[:] = previous_path
    if previous_slack is missing_module:
        sys.modules.pop("slack_read", None)
    else:
        sys.modules["slack_read"] = previous_slack


SHA = "a" * 40
SHA2 = "b" * 40
LINK = "https://github.com/Ylopo/newlead-processor/pull/7"

CFG = Config(mode="live", team_id="T_OK", channels=("C_OK",),
             email_domains=("ylopo.com",), orgs=("Ylopo",))


def ident(email="dev@ylopo.com", team="T_OK", **kw) -> Identity:
    return Identity(user_id="U1", email=email, team_id=team, is_bot=False,
                    deleted=False, is_restricted=kw.get("restricted", False),
                    is_ultra_restricted=kw.get("ultra", False))


def event(**kw) -> NormalizedEvent:
    base = dict(channel_id="C_OK", channel_type="channel", user_id="U1", ts="1.0",
                thread_ts=None, text=LINK, is_bot=False, subtype=None)
    base.update(kw)
    return NormalizedEvent(**base)


OPEN_PR = PRFacts(head_sha=SHA, author="teammate", state="OPEN",
                  review_decision="REVIEW_REQUIRED")


def deps(identity=None, facts=OPEN_PR, approved=None, verdict="ok", viewer="botuser"):
    return GateDeps(
        identity=lambda uid: ident() if identity is None else identity,
        pr_facts=lambda o, r, n: facts,
        approved_at=(approved or (lambda k: None)),
        rate_verdict=lambda uid: verdict,
        viewer_login=viewer,
        now=lambda: 1000.0,
    )


def reasons(result):
    return {r.reason for r in result.rejections}


# --- the gate accepts the ordinary case ----------------------------------

def test_accepts_plain_pr_link():
    result = evaluate(event(), CFG, deps())
    assert result.accepted
    cmd = result.commands[0]
    assert cmd.slug == "Ylopo/newlead-processor#7"
    assert cmd.head_sha == SHA


def test_accepts_thread_reply_without_mention():
    """These channels are fully passive by policy — no mention required."""
    assert evaluate(event(thread_ts="0.5"), CFG, deps()).accepted


# --- everything the gate must refuse -------------------------------------

@pytest.mark.parametrize("kw,expected", [
    (dict(is_bot=True), ReasonCode.NOT_HUMAN),
    (dict(user_id=None), ReasonCode.NOT_HUMAN),
    (dict(subtype="channel_join"), ReasonCode.NOT_HUMAN),
    (dict(channel_id="C_OTHER"), ReasonCode.CHANNEL_NOT_ALLOWED),
    (dict(text="ship it"), ReasonCode.NO_PR_URL),
])
def test_rejects_event_shape(kw, expected):
    assert expected in reasons(evaluate(event(**kw), CFG, deps()))


@pytest.mark.parametrize("identity,expected", [
    (ident(email="x@gmail.com"), ReasonCode.IDENTITY_NOT_TRUSTED),
    (ident(team="T_EVIL"), ReasonCode.IDENTITY_NOT_TRUSTED),
    (ident(ultra=True), ReasonCode.IDENTITY_NOT_TRUSTED),
])
def test_rejects_untrusted_identity(identity, expected):
    assert expected in reasons(evaluate(event(), CFG, deps(identity=identity)))


def test_identity_lookup_failure_fails_closed():
    d = GateDeps(identity=lambda uid: None, pr_facts=lambda o, r, n: OPEN_PR,
                 approved_at=lambda k: None, rate_verdict=lambda uid: "ok",
                 viewer_login="botuser", now=lambda: 0.0)
    assert ReasonCode.IDENTITY_LOOKUP_FAILED in reasons(evaluate(event(), CFG, d))


@pytest.mark.parametrize("text", [
    "https://github.com.evil.com/Ylopo/repo/pull/1",
    "https://github.com@evil.com/Ylopo/repo/pull/1",
    "http://github.com/Ylopo/repo/pull/1",
    "https://notgithub.com/Ylopo/repo/pull/1",
])
def test_rejects_lookalike_urls(text):
    assert ReasonCode.NO_PR_URL in reasons(evaluate(event(text=text), CFG, deps()))


def test_rejects_foreign_org():
    result = evaluate(event(text="https://github.com/Evil/repo/pull/1"), CFG, deps())
    assert not result.accepted
    assert ReasonCode.ORG_NOT_ALLOWED in reasons(result)


@pytest.mark.parametrize("facts,expected", [
    (None, ReasonCode.PR_NOT_FOUND),
    (PRFacts(SHA, "teammate", "MERGED", ""), ReasonCode.PR_NOT_OPEN),
    (PRFacts(SHA, "teammate", "CLOSED", ""), ReasonCode.PR_NOT_OPEN),
])
def test_rejects_unapprovable_pr_states(facts, expected):
    assert expected in reasons(evaluate(event(), CFG, deps(facts=facts)))


def test_skips_self_authored_pr():
    """GitHub 422s a self-approval; skip rather than collect a guaranteed error."""
    facts = PRFacts(SHA, "Chadtao206", "OPEN", "REVIEW_REQUIRED")
    result = evaluate(event(), CFG, deps(facts=facts, viewer="chadtao206"))
    assert not result.accepted
    assert ReasonCode.SELF_AUTHORED in reasons(result)


def test_skips_already_approved_pr():
    facts = PRFacts(SHA, "teammate", "OPEN", "APPROVED")
    assert ReasonCode.ALREADY_APPROVED in reasons(evaluate(event(), CFG, deps(facts=facts)))


def test_already_approved_is_honoured_when_disabled():
    cfg = Config(mode="live", team_id="T_OK", channels=("C_OK",),
                 email_domains=("ylopo.com",), skip_if_already_approved=False)
    facts = PRFacts(SHA, "teammate", "OPEN", "APPROVED")
    assert evaluate(event(), cfg, deps(facts=facts)).accepted


def test_rate_limited_blocks_whole_message():
    result = evaluate(event(), CFG, deps(verdict="limit of 30 per hour reached"))
    assert ReasonCode.RATE_LIMITED in reasons(result)


def test_global_cap_reports_daily_cap_reason():
    result = evaluate(event(), CFG, deps(verdict="global cap of 100 auto-approvals in 24h reached"))
    assert ReasonCode.DAILY_CAP in reasons(result)


def test_mode_off_rejects_everything():
    assert not evaluate(event(), Config(mode="off", channels=("C_OK",)), deps()).accepted


# --- dedup ----------------------------------------------------------------

def test_duplicate_at_same_head_is_skipped():
    result = evaluate(event(), CFG, deps(approved=lambda k: 123.0))
    assert ReasonCode.DUPLICATE in reasons(result)


def test_new_head_sha_is_a_new_approval():
    """A re-post after pushing a fix is a different key and earns its own stamp."""
    seen = {f"jdiapprove:Ylopo/newlead-processor#7@{SHA}": 1.0}
    facts = PRFacts(SHA2, "teammate", "OPEN", "REVIEW_REQUIRED")
    result = evaluate(event(), CFG, deps(facts=facts, approved=lambda k: seen.get(k)))
    assert result.accepted


def test_burst_cap_bounds_a_paste():
    cfg = Config(mode="live", team_id="T_OK", channels=("C_OK",),
                 email_domains=("ylopo.com",), limits=Limits(max_prs_per_message=2))
    text = " ".join(f"https://github.com/Ylopo/r{i}/pull/{i + 1}" for i in range(5))
    result = evaluate(event(text=text), cfg, deps())
    assert len(result.commands) == 2
    assert ReasonCode.RATE_LIMITED in reasons(result)


# --- the approval call ----------------------------------------------------

def test_approve_refuses_without_valid_sha():
    ok, detail = github.approve("Ylopo", "r", 1, "not-a-sha", "b", "tok",
                                runner=lambda *a, **k: pytest.fail("must not run"))
    assert not ok and "head sha" in detail


def test_approve_pins_commit_id():
    captured = {}

    def runner(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env", {})
        return type("P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    ok, _ = github.approve("Ylopo", "r", 7, SHA, "body text", "tok", runner=runner)
    assert ok
    argv = captured["argv"]
    assert f"commit_id={SHA}" in argv
    assert "event=APPROVE" in argv
    assert "repos/Ylopo/r/pulls/7/reviews" in argv
    assert captured["env"]["GH_TOKEN"] == "tok"


def test_approve_reports_failure_detail():
    def runner(argv, **kw):
        return type("P", (), {"returncode": 1, "stdout": "",
                              "stderr": "HTTP 422: can not approve own pr"})()

    ok, detail = github.approve("Ylopo", "r", 7, SHA, "b", "tok", runner=runner)
    assert not ok and "422" in detail


# --- pipeline behaviour ---------------------------------------------------

def build_pipeline(tmp_path: Path, cfg: Config, approve, facts=OPEN_PR):
    store = Store(tmp_path / "s.db")
    slack = SlackIO(lambda *a, **k: {"ok": True}, "tok", dry_run=cfg.mode != "live")
    pipe = poll.Pipeline(cfg, deps(facts=facts), store, slack, "ghtok",
                         tmp_path / "audit.jsonl", approve=approve)
    return pipe, slack


def test_shadow_mode_never_calls_github_or_slack(tmp_path):
    def approve(*a, **k):
        pytest.fail("shadow mode must not call GitHub")

    cfg = Config(mode="shadow", team_id="T_OK", channels=("C_OK",),
                 email_domains=("ylopo.com",))
    pipe, slack = build_pipeline(tmp_path, cfg, approve)
    pipe.handle(event())
    assert slack.dry_run_log == []
    assert any("would approve" in n for n in pipe.notes)


def test_live_mode_approves_and_reacts(tmp_path):
    calls = []

    def approve(owner, repo, num, sha, body, token, **k):
        calls.append((owner, repo, num, sha))
        return True, ""

    posted = []
    pipe, _ = build_pipeline(tmp_path, CFG, approve)
    pipe.slack = SlackIO(lambda tok, method, **kw: posted.append((method, kw)) or {"ok": True},
                         "tok", dry_run=False)
    pipe.handle(event())
    assert calls == [("Ylopo", "newlead-processor", 7, SHA)]
    assert posted[0][0] == "reactions.add"
    assert posted[0][1]["name"] == "blinkapproved"


def test_failed_approval_uses_failure_reaction_and_speaks(tmp_path):
    posted = []
    pipe, _ = build_pipeline(tmp_path, CFG, lambda *a, **k: (False, "HTTP 403"))
    pipe.slack = SlackIO(lambda tok, method, **kw: posted.append((method, kw)) or {"ok": True},
                         "tok", dry_run=False)
    pipe.handle(event())
    assert posted[0][1]["name"] == "warning"
    assert any("FAILED to approve" in n for n in pipe.notes)


def test_redelivered_event_is_handled_once(tmp_path):
    calls = []
    pipe, _ = build_pipeline(tmp_path, CFG,
                             lambda *a, **k: calls.append(1) or (True, ""))
    pipe.handle(event())
    pipe.handle(event())          # same channel+ts, as Slack redelivery would
    assert len(calls) == 1


def test_successful_approval_is_recorded_for_dedup(tmp_path):
    store = Store(tmp_path / "s.db")
    slack = SlackIO(lambda *a, **k: {"ok": True}, "tok", dry_run=False)
    pipe = poll.Pipeline(CFG, deps(approved=store.approved_at), store, slack,
                         "ghtok", tmp_path / "a.jsonl",
                         approve=lambda *a, **k: (True, ""))
    pipe.handle(event())
    key = f"jdiapprove:Ylopo/newlead-processor#7@{SHA}"
    assert store.approved_at(key) is not None


# --- store ----------------------------------------------------------------

def test_global_daily_cap_trips(tmp_path):
    store = Store(tmp_path / "s.db")
    limits = Limits(global_daily=2)
    for i in range(2):
        store.record_approval(f"k{i}", "Ylopo/r#1", 1000.0)
    assert store.rate_verdict("U1", limits, 1000.0).startswith("global cap")


def test_per_user_hourly_limit(tmp_path):
    store = Store(tmp_path / "s.db")
    limits = Limits(per_user_hourly=2, global_daily=999)
    for _ in range(2):
        store.record_request("U1", 1000.0)
    assert "per hour" in store.rate_verdict("U1", limits, 1000.0)
    assert store.rate_verdict("U2", limits, 1000.0) == "ok"


# --- config ---------------------------------------------------------------

def test_missing_block_is_off(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("other: {}\n")
    assert load_config(p).mode == "off"


def test_empty_channel_list_forces_off(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("jdi_autoapprove:\n  mode: live\n  channels: []\n")
    assert load_config(p).mode == "off"


def test_unknown_mode_is_off(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("jdi_autoapprove:\n  mode: yolo\n  channels: [C1]\n")
    assert load_config(p).mode == "off"


def test_unparseable_config_is_off(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("jdi_autoapprove: [unclosed\n")
    assert load_config(p).mode == "off"


def test_reaction_name_strips_colons(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("jdi_autoapprove:\n  mode: live\n  channels: [C1]\n"
                 "  reaction: ':blinkapproved:'\n")
    assert load_config(p).reaction == "blinkapproved"


def test_pipeline_is_loaded_from_maintained_checkout():
    assert Path(poll.__file__).resolve() == SOURCE_SCRIPTS / "jdi_autoapprove" / "poll.py"
    assert Path(github.__file__).resolve() == SOURCE_SCRIPTS / "jdi_autoapprove" / "github.py"
