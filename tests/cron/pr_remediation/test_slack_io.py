import re

import pytest

from pr_remediation.boris_source import Issue, Verdict
from pr_remediation.config import RemediationConfig
from pr_remediation.slack_io import SlackIO

VERDICT = Verdict("br_1", "Ylopo", "example", 42, "a" * 40, "1.0",
                  "ISSUES_FOUND", None, (Issue(title="Null deref"),))

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
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=2, unresolved_threads=[])
    _token, _method, params = calls[0]
    assert params["channel"] == "C0BANB70BHR"
    assert "ready for human review" in params["text"].lower()
    assert "https://github.com/Ylopo/example/pull/42" in params["text"]


# --- notify_ready's third parameter: unresolved bot review threads --------
#
# The blind spot this fix closes: the first live run declared a PR "ready"
# while three Codex threads sat open on the same file. `notify_ready` now
# takes what GitHubIO.unresolved_bot_threads() found and says so -- but the
# contract is asymmetric on purpose. `[]` (checked, nothing open) must
# produce byte-for-byte the same notice this method always sent, so a clean
# PR's message never changes. `None` (the check itself failed) must say so
# explicitly rather than silently falling back to the clean notice --
# collapsing the two would recreate the exact false all-clear this whole
# change exists to remove, indistinguishable from a genuinely clean PR to
# anyone reading Slack.

def test_no_unresolved_threads_produces_the_unchanged_clean_notice():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=2, unresolved_threads=[])
    text = calls[0][2]["text"]
    assert text == (
        "✅ Ylopo/example#42 is ready for human review.\n"
        "https://github.com/Ylopo/example/pull/42\n"
        "Cleared after 2 remediation round(s)."
    )


def test_unresolved_bot_threads_are_reported_with_count_and_paths():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=2, unresolved_threads=[
            {"path": "scripts/lib/tag-vendors.ts", "outdated": False},
            {"path": "scripts/lib/tag-vendors.ts", "outdated": False},
            {"path": "scripts/lib/tag-vendors.ts", "outdated": False},
        ])
    text = calls[0][2]["text"]
    assert "3 unresolved bot review thread" in text
    assert "scripts/lib/tag-vendors.ts" in text
    # The clean-notice lines must still be present verbatim -- this adds a
    # line, it does not replace the notice.
    assert "is ready for human review" in text
    assert "Cleared after 2 remediation round(s)." in text


def test_unresolved_bot_threads_across_multiple_files_names_each():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=1, unresolved_threads=[
            {"path": "a.ts", "outdated": False},
            {"path": "b.ts", "outdated": False},
        ])
    text = calls[0][2]["text"]
    assert "2 unresolved bot review threads" in text
    assert "a.ts" in text and "b.ts" in text


def test_outdated_threads_are_counted_not_dropped():
    """An outdated thread points at code that has since changed and is
    usually less actionable -- but it is not resolved, so it must still be
    counted and named, only distinguished from a current one."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=1, unresolved_threads=[
            {"path": "a.ts", "outdated": True},
        ])
    text = calls[0][2]["text"]
    assert "1 unresolved bot review thread" in text
    assert "outdated" in text
    assert "a.ts" in text


def test_a_thread_with_no_path_is_never_rendered_as_the_word_none():
    """`lanes.py`'s brief section drops a pathless thread from its file list
    and keeps it in its count; this notice used to render one as the literal
    `"None"`, which reads as a filename. The two render one read for one
    audience apiece and must not disagree about it -- a human comparing the
    Slack notice against the remediation brief would find one naming a file
    that does not exist."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=1, unresolved_threads=[
            {"path": "a.ts", "outdated": False},
            {"outdated": False},
        ])
    text = calls[0][2]["text"]
    assert "None" not in text, "a missing path must not read as a filename"
    assert "2 unresolved bot review threads" in text, \
        "the thread is real whatever this build can say about where it is"
    assert "a.ts" in text


def test_threads_with_no_path_at_all_still_report_their_count():
    """And drop the "on:" clause rather than trailing an empty one."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=1, unresolved_threads=[{"outdated": False}])
    text = calls[0][2]["text"]
    assert "1 unresolved bot review thread" in text
    assert "None" not in text and "on:" not in text


def test_a_failed_thread_check_says_so_rather_than_staying_silent():
    """The failure mode that matters most: None must never read as 'nothing
    outstanding'. A transient GraphQL error must produce a notice that says
    the check could not be made, not the clean notice a genuinely clear PR
    would get."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=2, unresolved_threads=None)
    text = calls[0][2]["text"]
    assert "could not" in text.lower()
    assert "is ready for human review" in text


def test_a_failed_thread_check_does_not_prevent_the_notice_from_posting():
    call, calls = caller_for()
    ts = SlackIO(RemediationConfig(), TOK, TOK, call).notify_ready(
        VERDICT, rounds=2, unresolved_threads=None)
    assert ts is not None
    assert calls, "the ready notice must still post when the thread check failed"


# --- notify_still_running: the progress notice, and its one hard rule ------
#
# Posted by the due pass once a round has run past
# loop.STILL_RUNNING_THRESHOLD_SECONDS with nothing to show for it but the
# ownership label and the thinkies reaction. It must land in the review
# thread, under Son of Chad's identity, exactly like a re-review post -- and
# it must NEVER carry cfg.marker: boris_intake's gate
# (gate.py::_is_loop_repost) admits a thread reply from this bot as a
# re-review *request* precisely when that marker is present, and a progress
# notice that carried it would be admitted as one, spawning a spurious review
# mid-remediation.

def test_still_running_notice_reports_elapsed_and_timeout_minutes():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_still_running(
        VERDICT, elapsed_minutes=45, timeout_minutes=180, thread_ts="1.0")
    text = calls[0][2]["text"]
    assert "45" in text
    assert "180" in text
    assert "escalate" in text.lower()


def test_still_running_notice_posts_as_a_reply_in_the_review_thread():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_still_running(
        VERDICT, elapsed_minutes=45, timeout_minutes=180, thread_ts="1.0")
    token, method, params = calls[0]
    assert token == TOK
    assert method == "chat.postMessage"
    assert params["thread_ts"] == "1.0"
    assert params["channel"] == "C0BPCKPEFCG"


def test_still_running_notice_uses_the_review_token():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), REVIEW_TOK, NOTIFY_TOK, call).notify_still_running(
        VERDICT, elapsed_minutes=45, timeout_minutes=180, thread_ts="1.0")
    assert calls[0][0] == REVIEW_TOK


def test_still_running_notice_refuses_to_post_without_a_thread():
    """Same gate, same guard as post_rereview/repost_rereview: a top-level
    post is silently dropped by the intake gate as NOT_HUMAN."""
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    assert io.notify_still_running(VERDICT, 45, 180, "") is None
    assert io.notify_still_running(VERDICT, 45, 180, None) is None
    assert calls == []


def test_still_running_notice_never_carries_the_loop_marker():
    """The one hard rule: this message must be admitted by boris_intake's
    gate as NOT_HUMAN, never as a loop repost -- see
    boris_intake/gate.py::_is_loop_repost, which keys on cfg.loop_marker
    being present verbatim. A regression here would spawn a spurious
    re-review mid-remediation."""
    call, calls = caller_for()
    cfg = RemediationConfig()
    SlackIO(cfg, TOK, TOK, call).notify_still_running(
        VERDICT, elapsed_minutes=45, timeout_minutes=180, thread_ts="1.0")
    assert cfg.marker not in calls[0][2]["text"]


def test_escalation_notification_names_the_reason_and_round():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_escalation(
        VERDICT, rounds=3, reason="round_cap")
    text = calls[0][2]["text"]
    assert "round_cap" in text and "3" in text
    assert calls[0][2]["channel"] == "C0BANB70BHR"


def test_escalation_without_a_lane_id_carries_no_extra_text():
    """The ordinary case, and every reason except lane_timeout-with-a-live-
    lane: the message must stay byte-for-byte what it was before this
    parameter existed."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).notify_escalation(
        VERDICT, rounds=3, reason="round_cap")
    text = calls[0][2]["text"]
    assert text == (
        "⚠️ Ylopo/example#42 needs you — remediation stopped (round_cap).\n"
        "https://github.com/Ylopo/example/pull/42\n"
        "Stopped at round 3 of 3."
    )


def test_escalation_with_a_lane_id_names_it_and_what_to_clean_up():
    call, calls = caller_for()
    cfg = RemediationConfig()
    SlackIO(cfg, TOK, TOK, call).notify_escalation(
        VERDICT, rounds=1, reason="lane_timeout", lane_id="t_42")
    text = calls[0][2]["text"]
    assert "t_42" in text
    assert "may still be running" in text or "still running" in text
    assert "push" in text.lower()
    assert cfg.owner_label in text
    assert "thinkies" in text
    assert "remove them yourself" in text.lower() or "clean" in text.lower() \
        or "take over" in text.lower()


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


def test_repost_rereview_carries_the_marker_and_the_pr_url():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).repost_rereview(VERDICT, "1.0")
    token, method, params = calls[0]
    assert token == TOK
    assert method == "chat.postMessage"
    assert params["thread_ts"] == "1.0"
    assert params["channel"] == "C0BPCKPEFCG"
    assert "[fixed it. probably. v1]" in params["text"]
    assert "https://github.com/Ylopo/example/pull/42" in params["text"]


def test_repost_rereview_does_not_claim_remediation_was_pushed():
    """The bug this fix closes: a system-origin repost (nothing remediated,
    no lane ran) must never say "remediation pushed"."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).repost_rereview(VERDICT, "1.0")
    text = calls[0][2]["text"].lower()
    assert "pushed" not in text
    assert "no remediation round was spent" in text


def test_repost_rereview_prints_no_misleading_round_number():
    """No "Round N of M" -- that phrasing claims a round was spent, and here
    none was."""
    call, calls = caller_for()
    SlackIO(RemediationConfig(), TOK, TOK, call).repost_rereview(VERDICT, "1.0")
    text = calls[0][2]["text"]
    assert not re.search(r"[Rr]ound\s+\d+\s+of\s+\d+", text)


def test_repost_rereview_refuses_to_post_without_a_thread_and_returns_none():
    """Same gate, same guard as post_rereview -- see the Defect 2 test below."""
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    assert io.repost_rereview(VERDICT, "") is None
    assert io.repost_rereview(VERDICT, None) is None
    assert calls == []


def test_repost_rereview_uses_the_review_token():
    call, calls = caller_for()
    SlackIO(RemediationConfig(), "review-tok", "notify-tok",
           call).repost_rereview(VERDICT, "1.0")
    assert calls[0][0] == "review-tok"


def test_the_two_rereview_messages_stay_distinct():
    """Regression coverage for the bug this fix closes: `_repost` in loop.py
    used to call `post_rereview` with `round_count + 1`, claiming a round was
    spent and a fix was pushed when neither had happened. If the two paths
    were ever collapsed back into sharing one message -- whether by making
    repost_rereview call post_rereview, or by making the two produce
    identical text -- this must fail."""
    call, calls = caller_for()
    io = SlackIO(RemediationConfig(), TOK, TOK, call)
    io.post_rereview(VERDICT, "1.0", 1)
    io.repost_rereview(VERDICT, "1.0")
    lane_pushed_text = calls[0][2]["text"]
    repost_text = calls[1][2]["text"]

    assert lane_pushed_text != repost_text
    assert "remediation pushed" in lane_pushed_text
    assert "remediation pushed" not in repost_text
    assert re.search(r"Round\s+1\s+of\s+3", lane_pushed_text)
    assert not re.search(r"[Rr]ound\s+\d+\s+of\s+\d+", repost_text)


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
        VERDICT, rounds=2, unresolved_threads=[])
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
    "repost_rereview": lambda io: io.repost_rereview(VERDICT, "1.0"),
    "notify_ready": lambda io: io.notify_ready(
        VERDICT, rounds=2, unresolved_threads=[]),
    "notify_escalation": lambda io: io.notify_escalation(
        VERDICT, rounds=3, reason="round_cap"),
    "notify_still_running": lambda io: io.notify_still_running(
        VERDICT, elapsed_minutes=45, timeout_minutes=180, thread_ts="1.0"),
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


@pytest.mark.parametrize("post", ("post_rereview", "repost_rereview"))
def test_both_rereview_posts_open_with_the_marker(post):
    """boris_intake's gate no longer accepts the marker ANYWHERE in the text --
    `_is_loop_repost` requires it to open the message, because a substring test
    also admits Boris's own verdict quoting it (see that predicate's
    docstring). That makes the ORDER of these lines load-bearing, not
    cosmetic: put the PR URL first and every re-review this loop posts is
    silently dropped as NOT_HUMAN, with a `ts` still coming back so nothing
    here notices.

    The marker literal is read off `RemediationConfig` rather than restated
    because what is under test is the position, not the value -- the value has
    its own drift guard in `selftest.py`."""
    call, calls = caller_for()
    cfg = RemediationConfig()
    io = SlackIO(cfg, TOK, TOK, call)
    args = (VERDICT, "1.0", 2) if post == "post_rereview" else (VERDICT, "1.0")

    getattr(io, post)(*args)

    assert calls[0][2]["text"].startswith(cfg.marker)
