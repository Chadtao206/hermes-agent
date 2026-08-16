import pytest

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig
from pr_remediation.slack_io import SlackIO

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None, ("Null deref",))


def caller_for(response=None):
    """Fake mirroring the real slack_read.slack_call(token, method, **params)
    signature: token first positional, method second, params as kwargs."""
    calls = []

    def call(token, method, **params):
        calls.append((token, method, params))
        return response or {"ok": True, "ts": "2.0"}

    return call, calls


def test_rereview_post_carries_the_marker_and_the_pr_url():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).post_rereview(VERDICT, "1.0", 2)
    token, method, params = calls[0]
    assert token == "tok"
    assert method == "chat.postMessage"
    assert params["thread_ts"] == "1.0"
    assert params["channel"] == "C0BPCKPEFCG"
    assert "[fixed it. probably. v1]" in params["text"]
    assert "https://github.com/Ylopo/example/pull/42" in params["text"]


def test_rereview_post_states_the_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).post_rereview(VERDICT, "1.0", 2)
    assert "Round 2 of 3" in calls[0][2]["text"]


def test_thinkies_is_added_and_removed_by_name():
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), "tok", call)
    io.add_thinkies("C1", "5.0")
    io.remove_thinkies("C1", "5.0")
    assert calls[0][1] == "reactions.add"
    assert calls[0][2]["name"] == "thinkies"
    assert calls[1][1] == "reactions.remove"


def test_ready_notification_goes_to_pr_curator():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).notify_ready(VERDICT, rounds=2)
    token, method, params = calls[0]
    assert params["channel"] == "C0BANB70BHR"
    assert "ready for human review" in params["text"].lower()
    assert "https://github.com/Ylopo/example/pull/42" in params["text"]


def test_escalation_notification_names_the_reason_and_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).notify_escalation(
        VERDICT, rounds=3, reason="round_cap")
    text = calls[0][2]["text"]
    assert "round_cap" in text and "3" in text
    assert calls[0][2]["channel"] == "C0BANB70BHR"


def test_a_failed_post_returns_none_rather_than_raising():
    call, _ = caller_for(response={"ok": False, "error": "channel_not_found"})
    assert SlackIO(RemediationConfig(), "tok", call).post_rereview(
        VERDICT, "1.0", 1) is None


def test_caller_is_invoked_with_token_first_and_never_inside_params():
    """Defect 1: the real slack_call(token, method, **params) helper takes the
    token as the first positional argument. Params become the urlencoded POST
    body, so leaving token in there would leak the credential into the
    request body as well as breaking the call (binding token to the method
    name and leaving method unfilled, raising TypeError against the real
    helper)."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "super-secret-tok", call).post_rereview(
        VERDICT, "1.0", 1)
    token, method, params = calls[0]
    assert token == "super-secret-tok"
    assert method == "chat.postMessage"
    assert "token" not in params


def test_post_rereview_refuses_to_post_without_a_thread_and_returns_none():
    """Defect 2: the review service's gate only admits this bot's message as a
    reply in an existing thread (boris_intake/gate.py::_is_loop_repost). A
    top-level post is silently rejected as NOT_HUMAN and nothing reports an
    error, so the loop stalls forever. post_rereview must refuse to post at
    all — not fall back to a top-level post — when thread_ts is empty."""
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), "tok", call)

    assert io.post_rereview(VERDICT, "", 1) is None
    assert io.post_rereview(VERDICT, None, 1) is None
    assert calls == []
