import pytest

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig
from pr_remediation.slack_io import SlackIO

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None, ("Null deref",))

# Used everywhere the test doesn't care which token is used, only that
# something reasonable happens with it — kept equal so old assertions that
# checked a single "tok" value still hold verbatim.
TOK = "tok"


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
    SlackIO(RemediationConfig(), TOK, TOK, call).post_rereview(VERDICT, "1.0", 2)
    token, method, params = calls[0]
    assert token == TOK
    assert method == "chat.postMessage"
    assert params["thread_ts"] == "1.0"
    assert params["channel"] == "C0BPCKPEFCG"
    assert "[fixed it. probably. v1]" in params["text"]
    assert "https://github.com/Ylopo/example/pull/42" in params["text"]


def test_rereview_post_states_the_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).post_rereview(VERDICT, "1.0", 2)
    assert "Round 2 of 3" in calls[0][2]["text"]


def test_thinkies_is_added_and_removed_by_name():
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    io.add_thinkies("C1", "5.0")
    io.remove_thinkies("C1", "5.0")
    assert calls[0][1] == "reactions.add"
    assert calls[0][2]["name"] == "thinkies"
    assert calls[1][1] == "reactions.remove"


def test_ready_notification_goes_to_pr_curator():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(VERDICT, rounds=2)
    token, method, params = calls[0]
    assert params["channel"] == "C0BANB70BHR"
    assert "ready for human review" in params["text"].lower()
    assert "https://github.com/Ylopo/example/pull/42" in params["text"]


def test_escalation_notification_names_the_reason_and_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_escalation(
        VERDICT, rounds=3, reason="round_cap")
    text = calls[0][2]["text"]
    assert "round_cap" in text and "3" in text
    assert calls[0][2]["channel"] == "C0BANB70BHR"


def test_a_failed_post_returns_none_rather_than_raising():
    call, _ = caller_for(response={"ok": False, "error": "channel_not_found"})
    assert SlackIO(RemediationConfig(), TOK, TOK, call).post_rereview(
        VERDICT, "1.0", 1) is None


def test_caller_is_invoked_with_token_first_and_never_inside_params():
    """Defect 1: the real slack_call(token, method, **params) helper takes the
    token as the first positional argument. Params become the urlencoded POST
    body, so leaving token in there would leak the credential into the
    request body as well as breaking the call (binding token to the method
    name and leaving method unfilled, raising TypeError against the real
    helper)."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "super-secret-tok", "unused-notify-tok",
           call).post_rereview(VERDICT, "1.0", 1)
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
    io = SlackIO(RemediationConfig(), TOK, TOK, call)

    assert io.post_rereview(VERDICT, "", 1) is None
    assert io.post_rereview(VERDICT, None, 1) is None
    assert calls == []


# --- The two-token contract: each method must use the right identity -------
#
# review_token ("Son of Chad") owns the review-thread work: the re-review
# post and the :thinkies: reactions, because Boris's own gate authorizes a
# re-review by that identity's Slack user id specifically. notify_token (the
# main Hermes bot) owns the notifications, because it is the identity
# already a member of the notify channel — Son of Chad cannot join it at
# all, since that channel lives in a different Slack workspace. A swap here
# is exactly the regression this split exists to prevent: it would either
# make every re-review invisible to Boris's gate, or make every
# notification fail with not_in_channel and vanish silently.

REVIEW_TOK = "review-token-son-of-chad"
NOTIFY_TOK = "notify-token-main-hermes-bot"


def test_post_rereview_uses_the_review_token():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), REVIEW_TOK, NOTIFY_TOK, call).post_rereview(
        VERDICT, "1.0", 1)
    assert calls[0][0] == REVIEW_TOK


def test_thinkies_use_the_review_token():
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), REVIEW_TOK, NOTIFY_TOK, call)
    io.add_thinkies("C1", "5.0")
    io.remove_thinkies("C1", "5.0")
    assert calls[0][0] == REVIEW_TOK
    assert calls[1][0] == REVIEW_TOK


def test_notify_ready_uses_the_notify_token():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), REVIEW_TOK, NOTIFY_TOK, call).notify_ready(
        VERDICT, rounds=2)
    assert calls[0][0] == NOTIFY_TOK


def test_notify_escalation_uses_the_notify_token():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), REVIEW_TOK, NOTIFY_TOK, call).notify_escalation(
        VERDICT, rounds=3, reason="round_cap")
    assert calls[0][0] == NOTIFY_TOK


# --- Regression coverage: no method may raise on any failure shape ---------
#
# slack_io.py runs inside a daemon's polling loop; an escaping exception
# takes down the service. `_post`, `add_thinkies` and `remove_thinkies` each
# guard against (a) a caller that raises and (b) a caller that returns
# something that isn't the expected `{"ok": ...}` dict. Only the `ok: false`
# shape had a committed test before this. These tests pin the rest of the
# failure surface down so a future edit that reintroduces a raise, or drops
# the isinstance(response, dict) guard, gets caught here rather than in prod.

def caller_returning(response):
    """Fake mirroring slack_call(token, method, **params): always returns
    `response`, whatever shape it is (including non-dict garbage)."""
    calls = []

    def call(token, method, **params):
        calls.append((token, method, params))
        return response

    return call, calls


def caller_raising(exc):
    """Fake mirroring slack_call(token, method, **params): always raises
    `exc` instead of returning."""
    calls = []

    def call(token, method, **params):
        calls.append((token, method, params))
        raise exc

    return call, calls


# Response shapes that are failures but are NOT the `{"ok": False}` shape
# already covered above: a dict with no "ok" key at all, and three flavors
# of "not a dict" that would blow up a bare `response["ok"]` or
# `response.get(...)` without the isinstance guard.
NON_SUCCESS_RESPONSES = [
    pytest.param({"note": "no ok key at all"}, id="dict-missing-ok-key"),
    pytest.param(None, id="none-response"),
    pytest.param("not-a-dict", id="string-response"),
    pytest.param(["not", "a", "dict"], id="list-response"),
]

POSTING_METHODS = {
    "post_rereview": lambda io: io.post_rereview(VERDICT, "1.0", 1),
    "notify_ready": lambda io: io.notify_ready(VERDICT, rounds=2),
    "notify_escalation": lambda io: io.notify_escalation(
        VERDICT, rounds=3, reason="round_cap"),
}

REACTION_METHODS = {
    "add_thinkies": lambda io: io.add_thinkies("C1", "5.0"),
    "remove_thinkies": lambda io: io.remove_thinkies("C1", "5.0"),
}

ALL_METHODS = {**POSTING_METHODS, **REACTION_METHODS}


@pytest.mark.parametrize("response", NON_SUCCESS_RESPONSES)
@pytest.mark.parametrize(
    "method_name,invoke", POSTING_METHODS.items(), ids=list(POSTING_METHODS))
def test_posting_method_returns_none_on_bad_response_shape(
        method_name, invoke, response):
    call, _ = caller_returning(response)
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    assert invoke(io) is None


@pytest.mark.parametrize("response", NON_SUCCESS_RESPONSES)
@pytest.mark.parametrize(
    "method_name,invoke", REACTION_METHODS.items(), ids=list(REACTION_METHODS))
def test_reaction_method_does_not_raise_on_bad_response_shape(
        method_name, invoke, response):
    call, _ = caller_returning(response)
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    invoke(io)  # must not raise; nothing further to assert


@pytest.mark.parametrize(
    "method_name,invoke", POSTING_METHODS.items(), ids=list(POSTING_METHODS))
def test_posting_method_returns_none_when_caller_raises(method_name, invoke):
    call, _ = caller_raising(RuntimeError("network exploded"))
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    assert invoke(io) is None


@pytest.mark.parametrize(
    "method_name,invoke", REACTION_METHODS.items(), ids=list(REACTION_METHODS))
def test_reaction_method_does_not_raise_when_caller_raises(method_name, invoke):
    call, _ = caller_raising(RuntimeError("network exploded"))
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    invoke(io)  # must not raise; nothing further to assert


@pytest.mark.parametrize(
    "method_name,invoke", ALL_METHODS.items(), ids=list(ALL_METHODS))
def test_a_raising_callers_exception_message_never_leaks_the_token(
        method_name, invoke):
    """The reviewer verified by hand that a raising caller can't leak the
    bot token through slack_io. Pin it down: embed the token in the
    exception message itself and confirm it never surfaces in whatever the
    method returns (or, since we call it un-guarded here, that it never
    propagates at all — an escaping exception would fail this test)."""
    token = "xoxb-do-not-leak-this-token-9999"

    def call(t, method, **params):
        raise RuntimeError(f"Slack API rejected credentials: {token}")

    io = SlackIO(RemediationConfig(), token, token, call)
    result = invoke(io)

    assert token not in str(result)
