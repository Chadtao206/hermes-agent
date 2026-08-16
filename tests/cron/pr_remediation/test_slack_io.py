import pytest

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig
from pr_remediation.slack_io import SlackIO

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None, ("Null deref",))


def caller_for(response=None):
    calls = []

    def call(method, **payload):
        calls.append((method, payload))
        return response or {"ok": True, "ts": "2.0"}

    return call, calls


def test_rereview_post_carries_the_marker_and_the_pr_url():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).post_rereview(VERDICT, "1.0", 2)
    method, payload = calls[0]
    assert method == "chat.postMessage"
    assert payload["thread_ts"] == "1.0"
    assert payload["channel"] == "C0BPCKPEFCG"
    assert "[fixed it. probably. v1]" in payload["text"]
    assert "https://github.com/Ylopo/example/pull/42" in payload["text"]


def test_rereview_post_states_the_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).post_rereview(VERDICT, "1.0", 2)
    assert "Round 2 of 3" in calls[0][1]["text"]


def test_thinkies_is_added_and_removed_by_name():
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), "tok", call)
    io.add_thinkies("C1", "5.0")
    io.remove_thinkies("C1", "5.0")
    assert calls[0][0] == "reactions.add"
    assert calls[0][1]["name"] == "thinkies"
    assert calls[1][0] == "reactions.remove"


def test_ready_notification_goes_to_pr_curator():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).notify_ready(VERDICT, rounds=2)
    method, payload = calls[0]
    assert payload["channel"] == "C0BANB70BHR"
    assert "ready for human review" in payload["text"].lower()
    assert "https://github.com/Ylopo/example/pull/42" in payload["text"]


def test_escalation_notification_names_the_reason_and_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "tok", call).notify_escalation(
        VERDICT, rounds=3, reason="round_cap")
    text = calls[0][1]["text"]
    assert "round_cap" in text and "3" in text
    assert calls[0][1]["channel"] == "C0BANB70BHR"


def test_a_failed_post_returns_none_rather_than_raising():
    call, _ = caller_for(response={"ok": False, "error": "channel_not_found"})
    assert SlackIO(RemediationConfig(), "tok", call).post_rereview(
        VERDICT, "1.0", 1) is None
