import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.models import ReasonCode, Rejection, ReviewCommand, ReviewPolicy
from boris_intake.slack_io import SlackIO

CMD = ReviewCommand(
    "Ylopo",
    "repo",
    5,
    "f" * 40,
    "zjenkins",
    "U1",
    "a@ylopo.com",
    "C1",
    "1.0",
    "1.0",
    "https://ylopo.slack.com/archives/C1/p10",
    ReviewPolicy(False),
)


def recorder():
    calls = []

    def call(token, method, **kw):
        calls.append((method, kw))
        return {"ok": True}

    return calls, call


def test_adds_only_rubbing():
    calls, call = recorder()
    SlackIO(call, "t", dry_run=False).add_reviewing_reaction("C1", "1.0")
    assert calls == [
        ("reactions.add", {"channel": "C1", "timestamp": "1.0", "name": "rubbing"})
    ]


def test_ack_is_threaded_and_names_the_pr_and_sha():
    calls, call = recorder()
    SlackIO(call, "t", dry_run=False).reply_ack(CMD)
    method, kw = calls[0]
    assert method == "chat.postMessage"
    assert kw["thread_ts"] == "1.0"
    assert "Ylopo/repo#5" in kw["text"] and "fffffff" in kw["text"]


def test_no_outbound_message_contains_a_parseable_pr_url():
    """Guards against self-trigger. Bot messages are already rejected as
    not_human, but that is one layer: if an outbound message carried a real PR
    URL, a HUMAN quoting or copying it would start a review. Every message we
    emit therefore names PRs as slugs (Ylopo/repo#5), never as links.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from boris_intake.grammar import extract_pr_refs

    calls, call = recorder()
    io = SlackIO(call, "t", dry_run=False)
    io.reply_ack(CMD)
    for reason in ReasonCode:
        io.reply_rejection("C1", "1.0", Rejection(reason, "Ylopo/repo#5 @ abc1234"))

    assert calls, "expected outbound messages to inspect"
    for method, kw in calls:
        text = kw.get("text", "")
        assert not extract_pr_refs(text), (
            f"outbound {method} carries a parseable PR URL and could be quoted "
            f"by a human into a new review: {text!r}"
        )


def test_ack_omits_internal_and_boilerplate_noise():
    # The lane id is kanban-internal; the read-only policy lives in the channel
    # description, the pin, and every GitHub comment footer. Neither belongs on
    # every review.
    calls, call = recorder()
    SlackIO(call, "t", dry_run=False).reply_ack(CMD)
    text = calls[0][1]["text"]
    for noise in ("lane", "t_1", "Read-only", "COMMENT review", "blocking"):
        assert noise not in text, f"{noise!r} should not be in the ack: {text!r}"
    assert text == "Boris is reviewing Ylopo/repo#5 @ fffffff"


def test_rejection_reply_includes_reason():
    calls, call = recorder()
    SlackIO(call, "t", dry_run=False).reply_rejection(
        "C1", "1.0", Rejection(ReasonCode.ORG_NOT_ALLOWED, "Evil/x#1")
    )
    _, kw = calls[0]
    assert "Evil/x#1" in kw["text"]
    assert kw["thread_ts"] == "1.0"


def test_unknown_flag_reply_is_the_supported_flags_contract():
    calls, call = recorder()
    detail = "Unknown review flag: --ignore-cii. Supported flags: --ignore-ci"

    SlackIO(call, "t", dry_run=False).reply_rejection(
        "C1", "1.0", Rejection(ReasonCode.UNKNOWN_FLAG, detail)
    )

    assert calls[0][1]["text"] == detail


def test_dry_run_performs_no_network_calls():
    calls, call = recorder()
    io = SlackIO(call, "t", dry_run=True)
    io.add_reviewing_reaction("C1", "1.0")
    io.reply_ack(CMD)
    io.reply_rejection("C1", "1.0", Rejection(ReasonCode.RATE_LIMITED, "slow down"))
    assert calls == []
    assert len(io.dry_run_log) == 3


def test_slack_failure_is_swallowed():
    def call(token, method, **kw):
        return {"ok": False, "error": "ratelimited"}

    SlackIO(call, "t", dry_run=False).add_reviewing_reaction(
        "C1", "1.0"
    )  # must not raise


def lifecycle_request():
    return SimpleNamespace(
        request_id="br_123",
        channel_id="C1",
        root_thread_ts="0.5",
    )


def test_lifecycle_status_is_created_in_the_root_thread_with_request_metadata():
    calls = []

    def call(token, method, **kw):
        calls.append((method, kw))
        return {"ok": True, "ts": "2.0"}

    message_ts = SlackIO(call, "t", dry_run=False).post_status(
        lifecycle_request(), "STARTING REVIEW"
    )

    assert message_ts == "2.0"
    assert calls == [
        (
            "chat.postMessage",
            {
                "channel": "C1",
                "thread_ts": "0.5",
                "text": "STARTING REVIEW",
                "metadata": {
                    "event_type": "boris_review_status_v2",
                    "event_payload": {"request_id": "br_123"},
                },
            },
        )
    ]


def test_lifecycle_status_lookup_matches_request_metadata_and_returns_text():
    def call(token, method, **kw):
        assert method == "conversations.replies"
        return {
            "ok": True,
            "messages": [
                {"ts": "0.5", "text": "root"},
                {
                    "ts": "2.0",
                    "text": "other",
                    "metadata": {
                        "event_type": "boris_review_status_v2",
                        "event_payload": {"request_id": "br_other"},
                    },
                },
                {
                    "ts": "3.0",
                    "text": "REVIEWING",
                    "metadata": {
                        "event_type": "boris_review_status_v2",
                        "event_payload": {"request_id": "br_123"},
                    },
                },
            ],
        }

    status = SlackIO(call, "t", dry_run=False).find_status(lifecycle_request())

    assert status.message_ts == "3.0"
    assert status.text == "REVIEWING"


def test_lifecycle_status_lookup_follows_opaque_cursors_and_requests_metadata():
    cursors = []

    def call(token, method, **kw):
        cursors.append(kw.get("cursor"))
        assert kw["limit"] == 200
        assert kw["include_all_metadata"] is True
        if kw.get("cursor") is None:
            return {
                "ok": True,
                "messages": [{"ts": "0.5", "text": "root"}],
                "response_metadata": {"next_cursor": "opaque-next"},
            }
        return {
            "ok": True,
            "messages": [
                {
                    "ts": "3.0",
                    "text": "REVIEWING",
                    "metadata": {
                        "event_type": "boris_review_status_v2",
                        "event_payload": {"request_id": "br_123"},
                    },
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }

    status = SlackIO(call, "t", dry_run=False).find_status(lifecycle_request())

    assert cursors == [None, "opaque-next"]
    assert status.message_ts == "3.0"


@pytest.mark.parametrize(
    ("method", "error", "exception_name"),
    (
        ("find", "thread_not_found", "SlackRootUnavailable"),
        ("post", "thread_not_found", "SlackRootUnavailable"),
        ("update", "message_not_found", "SlackStatusMissing"),
        ("update", "cant_update_message", "SlackStatusMissing"),
        ("update", "edit_window_closed", "SlackStatusMissing"),
        ("update", "ratelimited", "SlackLifecycleError"),
    ),
)
def test_lifecycle_slack_failures_are_typed(method, error, exception_name):
    from boris_intake import slack_io

    def call(token, api_method, **kw):
        return {"ok": False, "error": error}

    io = SlackIO(call, "t", dry_run=False)
    operation = {
        "find": lambda: io.find_status(lifecycle_request()),
        "post": lambda: io.post_status(lifecycle_request(), "STARTING"),
        "update": lambda: io.update_status(lifecycle_request(), "2.0", "DONE"),
    }[method]

    with pytest.raises(getattr(slack_io, exception_name)):
        operation()
