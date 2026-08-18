import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config
from boris_intake.gate import GateDeps, evaluate
from boris_intake.models import (
    GitHubReadStatus,
    Identity,
    NormalizedEvent,
    PRInfo,
    PRLookup,
    ReasonCode,
    RequestOrigin,
)

BOT = "U0B9TK6B41M"
CFG = Config(mode="live", channels=("C1", "C2"), passive_channels=("C1",),
             bot_user_id=BOT)
GOOD = Identity("U1", "a@ylopo.com", "T02PND103", False, False, False, False)
OPEN_PR = PRInfo("f" * 40, "zjenkins", "OPEN")
FOUND = PRLookup(GitHubReadStatus.FOUND, info=OPEN_PR)
#: GitHub answered, authoritatively, that there is no such pull request.
ABSENT = PRLookup(GitHubReadStatus.ABSENT, detail="GitHub reports no such PR")
#: No answer was obtained. Not the same thing, and the difference is the task.
UNAVAILABLE = PRLookup(
    GitHubReadStatus.UNAVAILABLE,
    detail="GitHub answered with an error",
)


def deps(identity=GOOD, pr=FOUND, rate="ok"):
    return GateDeps(
        identity=lambda uid: identity,
        pr_info=lambda o, r, n, budget: pr,
        rate_verdict=lambda uid: rate,
        now=lambda: 1_000_000.0,
    )


def ev(**kw):
    base = dict(channel_id="C1", channel_type="channel", user_id="U1", ts="1.0",
                thread_ts=None, text="https://github.com/Ylopo/repo/pull/5",
                is_bot=False, subtype=None)
    base.update(kw)
    return NormalizedEvent(**base)


def reasons(result):
    return {r.reason for r in result.rejections}


def test_passive_channel_bare_link_accepted():
    r = evaluate(ev(), CFG, deps())
    assert r.accepted
    cmd = r.commands[0]
    assert cmd.slug == "Ylopo/repo#5"
    assert cmd.head_sha == "f" * 40
    assert cmd.requester_email == "a@ylopo.com"


def test_non_passive_channel_requires_mention():
    r = evaluate(ev(channel_id="C2"), CFG, deps())
    assert not r.accepted and ReasonCode.MENTION_REQUIRED in reasons(r)


def test_non_passive_channel_with_mention_accepted():
    r = evaluate(ev(channel_id="C2",
                    text=f"<@{BOT}> https://github.com/Ylopo/repo/pull/5"),
                 CFG, deps())
    assert r.accepted


def test_unlisted_channel_rejected_silently():
    r = evaluate(ev(channel_id="C_OTHER"), CFG, deps())
    assert not r.accepted
    assert all(x.silent for x in r.rejections)
    assert ReasonCode.CHANNEL_NOT_ALLOWED in reasons(r)


def test_dm_rejected():
    r = evaluate(ev(channel_id="D1", channel_type="im"), CFG, deps())
    assert ReasonCode.CHANNEL_NOT_ALLOWED in reasons(r)


def test_bot_message_rejected():
    r = evaluate(ev(is_bot=True), CFG, deps())
    assert ReasonCode.NOT_HUMAN in reasons(r)


def test_workflow_builder_post_rejected():
    r = evaluate(ev(user_id=None, subtype="bot_message"), CFG, deps())
    assert ReasonCode.NOT_HUMAN in reasons(r)


def test_subtype_message_rejected():
    r = evaluate(ev(subtype="channel_join"), CFG, deps())
    assert ReasonCode.NOT_HUMAN in reasons(r)


def test_thread_reply_without_mention_rejected_outside_passive_channel():
    # C2 is allowlisted but NOT passive: a link dropped into an ordinary
    # engineering discussion must never start a review.
    r = evaluate(ev(channel_id="C2", thread_ts="0.5"), CFG, deps())
    assert ReasonCode.THREAD_REPLY_NO_MENTION in reasons(r)


def test_bare_link_in_thread_accepted_in_passive_channel():
    # Re-posting under the original review thread after pushing a fix is the
    # natural gesture; in a dedicated review channel it must just work.
    r = evaluate(ev(thread_ts="0.5"), CFG, deps())
    assert r.accepted
    assert r.commands[0].slug == "Ylopo/repo#5"


def test_thread_chatter_without_a_link_stays_silent_in_passive_channel():
    r = evaluate(ev(thread_ts="0.5", text="thanks, lgtm!"), CFG, deps())
    assert not r.accepted
    assert ReasonCode.NO_PR_URL in reasons(r)
    assert all(x.silent for x in r.rejections)


def test_thread_repost_reaches_atomic_admission_dedup():
    r = evaluate(ev(thread_ts="0.5"), CFG, deps())
    assert r.accepted


def test_thread_reply_with_mention_accepted():
    r = evaluate(ev(thread_ts="0.5",
                    text=f"<@{BOT}> https://github.com/Ylopo/repo/pull/5"),
                 CFG, deps())
    assert r.accepted


def test_identity_lookup_failure_fails_closed():
    r = evaluate(ev(), CFG, deps(identity=None))
    assert not r.accepted and ReasonCode.IDENTITY_LOOKUP_FAILED in reasons(r)


def test_untrusted_identity_rejected_loudly():
    bad = Identity("U1", "x@gmail.com", "T02PND103", False, False, False, False)
    r = evaluate(ev(), CFG, deps(identity=bad))
    assert ReasonCode.IDENTITY_NOT_TRUSTED in reasons(r)
    assert any(not x.silent for x in r.rejections)


def test_foreign_org_rejected():
    r = evaluate(ev(text="https://github.com/Other/repo/pull/5"), CFG, deps())
    assert ReasonCode.ORG_NOT_ALLOWED in reasons(r)


def test_no_pr_url_rejected_silently():
    r = evaluate(ev(text="good morning"), CFG, deps())
    assert ReasonCode.NO_PR_URL in reasons(r)
    assert all(x.silent for x in r.rejections)


def test_closed_pr_rejected():
    r = evaluate(ev(), CFG, deps(
        pr=PRLookup(GitHubReadStatus.FOUND, info=PRInfo("f" * 40, "x", "MERGED"))))
    assert ReasonCode.PR_NOT_OPEN in reasons(r)


def test_missing_pr_rejected():
    r = evaluate(ev(), CFG, deps(pr=ABSENT))
    assert ReasonCode.PR_NOT_FOUND in reasons(r)


def test_rate_limited_rejected():
    r = evaluate(ev(), CFG, deps(rate="limit of 10 reviews per hour reached"))
    assert ReasonCode.RATE_LIMITED in reasons(r)


def test_mode_off_rejects_everything():
    off = Config(mode="off", channels=("C1",), passive_channels=("C1",))
    r = evaluate(ev(), off, deps())
    assert ReasonCode.MODE_OFF in reasons(r)


def test_multiple_prs_yield_multiple_commands():
    text = "https://github.com/Ylopo/a/pull/1 https://github.com/Ylopo/b/pull/2"
    r = evaluate(ev(text=text), CFG, deps())
    assert [c.slug for c in r.commands] == ["Ylopo/a#1", "Ylopo/b#2"]


def test_ignore_ci_policy_applies_to_every_pr_in_one_message():
    text = (
        "https://github.com/Ylopo/a/pull/1 --ignore-ci "
        "https://github.com/Ylopo/b/pull/2"
    )

    r = evaluate(ev(text=text), CFG, deps())

    assert len(r.commands) == 2
    assert all(c.policy.ignore_ci for c in r.commands)
    assert all(c.policy.rubric_version == "2" for c in r.commands)


def test_policy_changes_review_idempotency_identity():
    normal = evaluate(ev(), CFG, deps()).commands[0]
    waived = evaluate(
        ev(text="https://github.com/Ylopo/repo/pull/5 --ignore-ci"), CFG, deps()
    ).commands[0]

    assert normal.head_sha == waived.head_sha
    assert normal.idempotency_key != waived.idempotency_key


def test_unknown_flag_rejects_entire_message_before_remote_lookups():
    calls = []
    guarded = GateDeps(
        identity=lambda uid: calls.append("identity"),
        pr_info=lambda o, r, n, budget: calls.append("pr"),
        rate_verdict=lambda uid: calls.append("rate"),
        now=lambda: 1_000_000.0,
    )

    r = evaluate(
        ev(text="https://github.com/Ylopo/a/pull/1 --ignore-cii"), CFG, guarded
    )

    assert not r.accepted
    assert reasons(r) == {ReasonCode.UNKNOWN_FLAG}
    assert r.rejections[0].detail == (
        "Unknown review flag: --ignore-cii. Supported flags: --ignore-ci"
    )
    assert calls == []


def test_command_uses_source_thread_root_and_discards_slack_prose():
    r = evaluate(
        ev(
            ts="3.0",
            thread_ts="1.0",
            text="secret prose https://github.com/Ylopo/repo/pull/5 --ignore-ci",
        ),
        CFG,
        deps(),
    )

    cmd = r.commands[0]
    assert cmd.root_thread_ts == "1.0"
    assert "secret prose" not in repr(cmd)


def test_mixed_org_message_accepts_only_allowed_org():
    text = "https://github.com/Ylopo/a/pull/1 https://github.com/Evil/b/pull/2"
    r = evaluate(ev(text=text), CFG, deps())
    assert [c.slug for c in r.commands] == ["Ylopo/a#1"]
    assert ReasonCode.ORG_NOT_ALLOWED in reasons(r)


def test_command_carries_no_slack_free_text():
    r = evaluate(ev(text="DROP TABLE tasks; https://github.com/Ylopo/repo/pull/5"),
                 CFG, deps())
    assert "DROP TABLE" not in repr(r.commands[0])


def test_permalink_is_well_formed():
    r = evaluate(ev(ts="1785988200.123456"), CFG, deps())
    assert r.commands[0].permalink == (
        "https://ylopo.slack.com/archives/C1/p1785988200123456")


# --- burst cap (found while sizing against real traffic, 2026-08-06) ---------
# Rate limiting is per-message, so without this a single paste of N links would
# create N lanes on one check. 24-PR batches are real, so the cap is generous.

def test_burst_cap_bounds_a_runaway_paste():
    from boris_intake.config import Limits
    cfg = Config(mode="live", channels=("C1",), passive_channels=("C1",),
                 bot_user_id=BOT, limits=Limits(max_prs_per_message=3))
    text = " ".join(f"https://github.com/Ylopo/r{i}/pull/{i}" for i in range(1, 8))
    r = evaluate(ev(text=text), cfg, deps())
    assert len(r.commands) == 3
    over = [x for x in r.rejections if x.reason is ReasonCode.RATE_LIMITED]
    assert len(over) == 4
    assert "more than 3 PRs in one message" in over[0].detail


def test_real_world_24_pr_batch_is_admitted_by_default():
    text = " ".join(f"https://github.com/Ylopo/r{i}/pull/{i}" for i in range(1, 25))
    r = evaluate(ev(text=text), CFG, deps())
    assert len(r.commands) == 24
    assert not [x for x in r.rejections if x.reason is ReasonCode.RATE_LIMITED]


# --- remediation loop's marked re-review (Task 1) ----------------------------

MARKER = "[fixed it. probably. v1]"
LOOP_CFG = Config(mode="live", channels=("C1", "C2"), passive_channels=("C1",),
                  bot_user_id=BOT, loop_bot_user_id=BOT, loop_marker=MARKER,
                  loop_attributed_user_id="U07E3AHV686")
CHAD = Identity("U07E3AHV686", "ctao@ylopo.com", "T02PND103", False, False, False, False)


def test_boris_own_verdict_post_is_still_rejected():
    """THE regression test: Boris's verdict carries a real PR URL (result.py:869).

    If this ever passes admission, Boris re-admits its own output on every review.
    """
    verdict = ev(
        user_id=BOT, is_bot=True, thread_ts="0.9",
        text="READY — Ylopo/repo#5 @ fffffff\n"
             "GitHub review: https://github.com/Ylopo/repo/pull/5#pullrequestreview-1",
    )
    result = evaluate(verdict, LOOP_CFG, deps())
    assert reasons(result) == {ReasonCode.NOT_HUMAN}


def test_marked_loop_repost_is_admitted_and_attributed_to_chad():
    repost = ev(
        user_id=BOT, is_bot=True, thread_ts="0.9",
        text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5",
    )
    result = evaluate(repost, LOOP_CFG, deps(identity=CHAD))
    assert not result.rejections
    assert result.commands[0].requester_user_id == "U07E3AHV686"
    assert result.commands[0].requester_email == "ctao@ylopo.com"


def test_marker_from_any_other_bot_is_rejected():
    other = ev(user_id="UOTHERBOT", is_bot=True, thread_ts="0.9",
               text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(other, LOOP_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_marker_outside_a_thread_is_rejected():
    top_level = ev(user_id=BOT, is_bot=True, thread_ts=None,
                   text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(top_level, LOOP_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_marker_on_thread_root_is_rejected():
    """A marked message posted as a thread root (thread_ts == ts) is not a reply."""
    root = ev(user_id=BOT, is_bot=True, ts="0.9", thread_ts="0.9",
             text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(root, LOOP_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_marker_does_nothing_when_the_loop_is_not_configured():
    """Unconfigured deployments must behave exactly as before."""
    repost = ev(user_id=BOT, is_bot=True, thread_ts="0.9",
                text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(repost, CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_loop_repost_with_a_subtype_is_rejected():
    """message_changed and friends are never a fresh request."""
    edited = ev(user_id=BOT, is_bot=True, thread_ts="0.9", subtype="message_changed",
                text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(edited, LOOP_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_loop_branch_inert_when_attribution_is_unset():
    """The mirror of test_discovery_branch_inert_when_attribution_is_unset, which
    the loop door was missing. marker and bot id configured but
    loop_attributed_user_id blank: a repost must not be admitted and attributed
    to "" -- that would only fail closed later at deps.identity(""). The clause
    is what closes the door here, at the guard, before attribution ever
    happens."""
    cfg = replace(LOOP_CFG, loop_attributed_user_id="")
    repost = ev(user_id=BOT, is_bot=True, thread_ts="0.9",
                text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(repost, cfg, deps())) == {ReasonCode.NOT_HUMAN}


def test_loop_branch_inert_when_only_the_marker_is_unset():
    """loop_bot_user_id and loop_attributed_user_id configured but the marker
    blank. `"".startswith("")` is True for every string, so without the guard
    every message from that bot in any thread would be a re-review request --
    the guard, not the predicate, is what has to hold here. The discovery door
    has had this pin since it was written."""
    cfg = replace(LOOP_CFG, loop_marker="")
    repost = ev(user_id=BOT, is_bot=True, thread_ts="0.9",
                text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5")
    assert reasons(evaluate(repost, cfg, deps())) == {ReasonCode.NOT_HUMAN}


def test_a_message_that_merely_quotes_the_marker_is_rejected():
    """The loop always OPENS its re-review with the marker
    (`pr_remediation/slack_io.py::post_rereview`). A message that quotes it
    somewhere in the middle is somebody else's text -- and the somebody else
    with the most access is Boris itself, whose verdict renders model-authored
    issue titles about an untrusted pull request under this same bot user, in
    this same thread. Admitting it hands every PR URL in it Chad's trust.
    See test_integration.py for the same property driven end to end through the
    real renderer."""
    quoted = ev(
        user_id=BOT, is_bot=True, thread_ts="0.9",
        text=f"ISSUES FOUND — Ylopo/repo#5 @ fffffff\n"
             f"• Reviewer quoted {MARKER} from the branch name\n"
             f"https://github.com/Ylopo/repo/pull/5",
    )
    assert reasons(evaluate(quoted, LOOP_CFG, deps())) == {ReasonCode.NOT_HUMAN}


# --- discovery cron's marked top-level request (Task 3) ----------------------
#
# A second, narrower door: discovery posts are top-level (the loop's are always
# thread replies), and their own marker -- never the loop's -- is what
# separates them from Boris's own top-level verdicts, which carry parseable PR
# URLs and post as this same bot user.

DISCOVERY_MARKER = "[swept this one up. v1]"
DISCOVERY_CFG = Config(mode="live", channels=("C1", "C2"), passive_channels=("C1",),
                       bot_user_id=BOT, discovery_bot_user_id=BOT,
                       discovery_marker=DISCOVERY_MARKER,
                       loop_attributed_user_id="U07E3AHV686")


def test_discovery_post_admitted_top_level():
    post = ev(user_id=BOT, is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    result = evaluate(post, DISCOVERY_CFG, deps(identity=CHAD))
    assert not result.rejections
    assert result.commands[0].slug == "Ylopo/repo#5"


def test_discovery_post_requires_its_own_marker():
    """The loop's marker must not open the discovery door."""
    post = ev(user_id=BOT, is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {MARKER}")
    assert reasons(evaluate(post, DISCOVERY_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_boris_own_top_level_verdict_is_not_discovery():
    verdict = ev(
        user_id=BOT, is_bot=True, thread_ts=None,
        text="READY — Ylopo/repo#5 @ fffffff\n"
             "GitHub review: https://github.com/Ylopo/repo/pull/5#pullrequestreview-1",
    )
    assert reasons(evaluate(verdict, DISCOVERY_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_discovery_branch_inert_when_unconfigured():
    """The "both blank" case. Kills nothing on its own -- CFG's blank
    discovery_bot_user_id blocks independently of the guard below. Still worth
    asserting; see test_discovery_branch_inert_when_only_marker_is_unset for the
    test that actually pins the guard."""
    post = ev(user_id=BOT, is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    assert reasons(evaluate(post, CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_discovery_branch_inert_when_only_marker_is_unset():
    """discovery_bot_user_id alone configured must not open the door: an empty
    discovery_marker is trivially "in" every string, so the guard -- not the
    identity check -- is what has to hold here."""
    cfg = replace(DISCOVERY_CFG, discovery_marker="")
    post = ev(user_id=BOT, is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    assert reasons(evaluate(post, cfg, deps())) == {ReasonCode.NOT_HUMAN}


def test_discovery_post_rejected_as_thread_reply():
    """Top-level only -- a threaded post is the loop's door, not this one."""
    reply = ev(user_id=BOT, is_bot=True, ts="789.012", thread_ts="123.456",
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    assert reasons(evaluate(reply, DISCOVERY_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_discovery_post_from_other_bot_is_rejected():
    other = ev(user_id="UOTHERBOT", is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    assert reasons(evaluate(other, DISCOVERY_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_discovery_post_with_a_subtype_is_rejected():
    """message_changed and friends are never a fresh request."""
    edited = ev(user_id=BOT, is_bot=True, thread_ts=None, subtype="message_changed",
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    assert reasons(evaluate(edited, DISCOVERY_CFG, deps())) == {ReasonCode.NOT_HUMAN}


def test_discovery_post_attributes_to_chad():
    post = ev(user_id=BOT, is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    result = evaluate(post, DISCOVERY_CFG, deps(identity=CHAD))
    assert result.commands[0].requester_user_id == CHAD.user_id
    assert result.commands[0].requester_email == CHAD.email


def test_discovery_post_as_thread_root_is_admitted():
    """A thread PARENT carries thread_ts == ts -- this is exactly what
    daemon.py's catch_up produces when it replays through conversations.history
    after a restart. Without this arm, a discovery post that had already
    received Boris's reply before the restart would be silently dropped on
    catch-up. Mirrors test_marker_on_thread_root_is_rejected on the loop side,
    except here thread-root must be ADMITTED, not rejected -- the two doors are
    inverted on threading."""
    root = ev(user_id=BOT, is_bot=True, ts="123.456", thread_ts="123.456",
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    result = evaluate(root, DISCOVERY_CFG, deps(identity=CHAD))
    assert not result.rejections
    assert result.commands[0].slug == "Ylopo/repo#5"


def test_discovery_branch_inert_when_attribution_is_unset():
    """marker and bot id configured but loop_attributed_user_id blank: a post
    must not be admitted and attributed to "" -- that would only fail closed
    later at deps.identity(""). The clause is what closes the door here, at the
    guard, before attribution ever happens."""
    cfg = replace(DISCOVERY_CFG, loop_attributed_user_id="")
    post = ev(user_id=BOT, is_bot=True, thread_ts=None,
              text=f"https://github.com/Ylopo/repo/pull/5 {DISCOVERY_MARKER}")
    assert reasons(evaluate(post, cfg, deps())) == {ReasonCode.NOT_HUMAN}


# ---------------------------------------------------------------------------
# "GitHub was unreachable" is a different rejection from "that PR does not
# exist" -- the 2026-08-17 conflation, at the gate.
# ---------------------------------------------------------------------------


def test_an_unreadable_pr_read_is_github_unavailable_not_pr_not_found():
    """The defect, at the line that caused it. PR_NOT_FOUND is terminal and
    asserts the pull request does not exist; asserting that about an open one
    stranded `Ylopo/forge-content-service#111` for hours."""
    r = evaluate(ev(), CFG, deps(pr=UNAVAILABLE))

    assert reasons(r) == {ReasonCode.GITHUB_UNAVAILABLE}
    assert ReasonCode.PR_NOT_FOUND not in reasons(r)
    assert "Ylopo/repo#5" in r.rejections[0].detail
    assert not r.accepted


def test_the_outage_rejection_is_never_silent():
    """It is precisely the rejection that has to be visible: the one channel that
    would have shown the 2026-08-17 lie was the in-thread reply."""
    from boris_intake.models import ALWAYS_REPLY_REASONS, SILENT_REASONS, Rejection

    assert ReasonCode.GITHUB_UNAVAILABLE not in SILENT_REASONS
    assert Rejection(ReasonCode.GITHUB_UNAVAILABLE).silent is False
    # An always-delivered reason that was also silent would be a contradiction.
    assert not (ALWAYS_REPLY_REASONS & SILENT_REASONS)


def test_one_unreadable_pr_does_not_lose_the_others_in_the_message():
    lookups = {
        5: UNAVAILABLE,
        6: FOUND,
        7: ABSENT,
    }
    d = GateDeps(
        identity=lambda uid: GOOD,
        pr_info=lambda o, r, n, budget: lookups[n],
        rate_verdict=lambda uid: "ok",
        now=lambda: 1_000_000.0,
    )

    r = evaluate(ev(text=(
        "https://github.com/Ylopo/repo/pull/5 "
        "https://github.com/Ylopo/repo/pull/6 "
        "https://github.com/Ylopo/repo/pull/7"
    )), CFG, d)

    assert [c.pr_number for c in r.commands] == [6]
    assert reasons(r) == {ReasonCode.GITHUB_UNAVAILABLE, ReasonCode.PR_NOT_FOUND}


def test_every_pr_read_in_one_message_gets_the_same_retry_budget():
    """One window per message, not per call. `daemon.Pipeline.handle` owns the
    object; the gate only forwards it."""
    seen = []

    def pr_info(owner, repo, number, budget):
        seen.append(budget)
        return FOUND

    d = GateDeps(
        identity=lambda uid: GOOD,
        pr_info=pr_info,
        rate_verdict=lambda uid: "ok",
        now=lambda: 1_000_000.0,
    )
    marker = object()

    evaluate(ev(text=(
        "https://github.com/Ylopo/repo/pull/5 "
        "https://github.com/Ylopo/repo/pull/6"
    )), CFG, d, marker)

    assert seen == [marker, marker]


# ---------------------------------------------------------------------------
# Attribution on the result: who the request is FOR, and which door it came
# through. The rejection-reply cooldown is keyed on both -- see
# GateResult.reply_cooldown_key and daemon._maybe_reply_rejections.
# ---------------------------------------------------------------------------


def test_a_human_rejection_is_attributed_to_the_human():
    r = evaluate(ev(), CFG, deps(pr=UNAVAILABLE))

    assert r.origin is RequestOrigin.HUMAN
    assert r.requester_user_id == "U1"
    assert r.reply_cooldown_key == "human:U1"


def test_a_loop_repost_rejection_is_attributed_to_chad_through_the_loop_door():
    repost = ev(
        user_id=BOT, is_bot=True, thread_ts="0.9",
        text=f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/5",
    )

    r = evaluate(repost, LOOP_CFG, deps(identity=CHAD, pr=UNAVAILABLE))

    assert reasons(r) == {ReasonCode.GITHUB_UNAVAILABLE}
    assert r.origin is RequestOrigin.LOOP
    assert r.requester_user_id == "U07E3AHV686"
    # NOT the bot that posted it, which is what shared one cooldown row with
    # every other bot rejection, and NOT bare "U07E3AHV686", which would share
    # Chad's own row so one bad link he pasted could silence the loop.
    assert r.reply_cooldown_key == "loop:U07E3AHV686"


def test_a_discovery_post_rejection_keys_apart_from_the_loops():
    discovery_cfg = replace(
        LOOP_CFG, discovery_bot_user_id=BOT, discovery_marker="[swept. v1]"
    )
    post = ev(
        user_id=BOT, is_bot=True,
        text="https://github.com/Ylopo/repo/pull/5 [swept. v1]",
    )

    r = evaluate(post, discovery_cfg, deps(identity=CHAD, pr=UNAVAILABLE))

    assert r.origin is RequestOrigin.DISCOVERY
    assert r.reply_cooldown_key == "discovery:U07E3AHV686"


def test_an_accepted_message_carries_the_same_attribution():
    r = evaluate(ev(), CFG, deps())

    assert r.accepted
    assert r.reply_cooldown_key == "human:U1"


# ---------------------------------------------------------------------------
# Which rejections bypass the reply cooldown, pinned in BOTH directions.
#
# Two mutations proved this membership was untested either way: dropping
# GITHUB_UNAVAILABLE from the set survived, and so did narrowing the bypass to
# it alone. The set's own docstring says these mean "the machinery failed, not
# your request was wrong", so the membership is a claim about MEANING, and every
# requester-caused rejection has to stay outside it -- an un-throttled reply on
# every post for a self-inflicted mistake is the noise this cooldown exists to
# stop (P3).
# ---------------------------------------------------------------------------


def test_only_an_infrastructure_failure_bypasses_the_reply_cooldown():
    from boris_intake.models import ALWAYS_REPLY_REASONS

    # Fails if the infrastructure reason is dropped: nobody can act on an outage
    # they never see, which is how three remediation loops were stranded.
    assert ReasonCode.GITHUB_UNAVAILABLE in ALWAYS_REPLY_REASONS
    # Fails if anything else is added. Written as the exact set, at a literal
    # boundary, because the interesting mutations go in both directions.
    assert ALWAYS_REPLY_REASONS == frozenset({ReasonCode.GITHUB_UNAVAILABLE})


def test_no_requester_caused_rejection_is_ever_delivered_unthrottled():
    """Enumerated rather than derived, so adding a reason code forces a decision
    about which side of this line it belongs on.

    GATE_SNAPSHOT_UNAVAILABLE is on this list, and its presence is the fix: after
    the three-state capture split the two meanings apart, its ONLY producer is
    the moved-head refusal -- the requester pushed while requesting. That is
    "your request was wrong" in the most literal way this service sees one, so
    someone racing a push against their own link is throttled like anyone else.
    """
    from boris_intake.models import ALWAYS_REPLY_REASONS

    requester_caused = frozenset({
        ReasonCode.PR_NOT_FOUND,
        ReasonCode.PR_NOT_OPEN,
        ReasonCode.PR_NO_CHECKS,
        ReasonCode.GATE_SNAPSHOT_UNAVAILABLE,
        ReasonCode.ORG_NOT_ALLOWED,
        ReasonCode.IDENTITY_NOT_TRUSTED,
        ReasonCode.UNKNOWN_FLAG,
        ReasonCode.DUPLICATE,
        ReasonCode.RATE_LIMITED,
    })

    assert not (ALWAYS_REPLY_REASONS & requester_caused)
