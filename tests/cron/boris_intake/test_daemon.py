import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config
from boris_intake.daemon import Pipeline, normalize
from boris_intake.gate import GateDeps
from boris_intake.models import (
    GateSnapshot,
    GateSnapshotLookup,
    GitHubReadStatus,
    Identity,
    PRInfo,
    PRLookup,
    SnapshotStatus,
    WorkflowState,
)
from boris_intake.slack_io import SlackIO
from boris_intake.store import Store

BOT = "U0B9TK6B41M"
LIVE = Config(mode="live", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT)
SHADOW = Config(mode="shadow", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT)
GOOD = Identity("U1", "a@ylopo.com", "T02PND103", False, False, False, False)
SNAPSHOT = GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())
CAPTURED = GateSnapshotLookup(SnapshotStatus.CAPTURED, snapshot=SNAPSHOT)
FOUND_PR = PRLookup(
    GitHubReadStatus.FOUND, info=PRInfo("f" * 40, "zjenkins", "OPEN")
)


def msg(**kw):
    base = {"channel": "C1", "channel_type": "channel", "user": "U1", "ts": "1.0",
            "text": "https://github.com/Ylopo/repo/pull/5"}
    base.update(kw)
    return base


def test_normalize_maps_slack_shape():
    ev = normalize(msg())
    assert ev.channel_id == "C1" and ev.user_id == "U1" and ev.is_bot is False


def test_normalize_flags_bot_message():
    ev = normalize({"channel": "C1", "bot_id": "B1", "ts": "1.0", "text": "x",
                    "subtype": "bot_message"})
    assert ev.is_bot is True


def test_normalize_returns_none_without_channel_or_ts():
    assert normalize({"user": "U1", "text": "x"}) is None


def _pipeline(tmp_path, cfg=LIVE):
    store = Store(tmp_path / "s.db")
    calls = []
    captures = []

    def slack_call(token, method, **kw):
        calls.append((method, kw))
        return {"ok": True}

    deps = GateDeps(
        identity=lambda uid: GOOD,
        pr_info=lambda o, r, n, budget: FOUND_PR,
        rate_verdict=lambda uid: store.rate_verdict(uid, cfg.limits, 1_000_000.0),
        now=lambda: 1_000_000.0,
    )

    def capture(command, captured_at, budget):
        captures.append((command.slug, captured_at))
        return CAPTURED

    p = Pipeline(cfg=cfg, deps=deps, store=store,
                 slack=SlackIO(slack_call, "t", dry_run=cfg.mode != "live"),
                 audit_path=tmp_path / "audit.jsonl",
                 capture_gate_snapshot=capture)
    return p, store, calls, captures


def records(tmp_path):
    return [json.loads(l) for l in
            (tmp_path / "audit.jsonl").read_text().strip().split("\n")]


def test_live_atomically_admits_without_lane_or_slack_lifecycle_writes(tmp_path):
    p, store, calls, captures = _pipeline(tmp_path)
    p.handle(normalize(msg()))

    admitted = store.requests_for_event("C1", "1.0")
    assert len(admitted) == 1
    assert admitted[0].workflow_state is WorkflowState.ADMITTED
    assert calls == []
    assert captures == [("Ylopo/repo#5", "1970-01-12T13:46:40Z")]


def test_shadow_writes_only_a_proposed_audit_record(tmp_path):
    p, store, calls, captures = _pipeline(tmp_path, cfg=SHADOW)
    p.handle(normalize(msg()))

    assert calls == []
    assert captures == [("Ylopo/repo#5", "1970-01-12T13:46:40Z")]
    assert store.requests_for_event("C1", "1.0") == ()
    assert store.seen_event("C1", "1.0") is False
    assert records(tmp_path)[0]["mode"] == "shadow"
    assert records(tmp_path)[0]["decision"] == "accept"
    assert records(tmp_path)[0]["proposed"] is True


def test_redelivery_after_admission_reuses_requests_without_remote_work(tmp_path):
    p, store, calls, captures = _pipeline(tmp_path)
    ev = normalize(msg())
    p.handle(ev)
    original_id = store.requests_for_event("C1", "1.0")[0].request_id
    captures.clear()

    p.handle(ev)

    assert captures == []
    assert calls == []
    assert store.requests_for_event("C1", "1.0")[0].request_id == original_id
    assert records(tmp_path)[-1]["redelivered"] is True


def test_redelivery_recovers_an_audit_crash_after_atomic_admission(
    tmp_path, monkeypatch
):
    from boris_intake import daemon

    p, store, calls, captures = _pipeline(tmp_path)
    original_write = daemon.audit.write
    monkeypatch.setattr(
        daemon.audit,
        "write",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit crash")),
    )

    with pytest.raises(RuntimeError, match="audit crash"):
        p.handle(normalize(msg()))
    admitted_id = store.requests_for_event("C1", "1.0")[0].request_id
    captures.clear()
    monkeypatch.setattr(daemon.audit, "write", original_write)

    p.handle(normalize(msg()))

    assert captures == []
    assert calls == []
    assert store.requests_for_event("C1", "1.0")[0].request_id == admitted_id
    assert records(tmp_path)[0]["redelivered"] is True


def test_every_decision_is_audited(tmp_path):
    p, _store, _calls, _captures = _pipeline(tmp_path)
    p.handle(normalize(msg(channel="C_OTHER")))
    rec = records(tmp_path)[0]
    assert rec["decision"] == "reject"
    assert rec["reasons"] == ["channel_not_allowed"]


def test_unknown_flag_rejects_before_snapshot_or_admission(tmp_path):
    p, store, _calls, captures = _pipeline(tmp_path)

    p.handle(normalize(msg(text=msg()["text"] + " --ignore-cii")))

    assert captures == []
    assert store.requests_for_event("C1", "1.0") == ()
    assert records(tmp_path)[0]["reasons"] == ["unknown_flag"]


def test_rejection_reply_failure_is_audited(tmp_path):
    p, store, calls, _captures = _pipeline(tmp_path)

    def fail_slack(token, method, **kwargs):
        calls.append((method, kwargs))
        return {"ok": False, "error": "ratelimited"}

    p.slack = SlackIO(fail_slack, "t", dry_run=False)
    p.handle(normalize(msg(text=msg()["text"] + " --ignore-cii")))

    assert store.requests_for_event("C1", "1.0") == ()
    failure = records(tmp_path)[-1]
    assert failure["decision"] == "rejection_delivery_error"
    assert failure["error"] == "ratelimited"
    assert failure["channel"] == "C1"
    assert failure["ts"] == "1.0"


def test_snapshot_failure_rejects_only_that_pr_and_admits_the_rest(tmp_path):
    p, store, _calls, _captures = _pipeline(tmp_path)
    original_capture = p.capture_gate_snapshot

    def capture(command, captured_at, budget):
        if command.repo == "unavailable":
            return GateSnapshotLookup(
                SnapshotStatus.HEAD_MOVED, detail="the head moved to abc1234"
            )
        return original_capture(command, captured_at, budget)

    p.capture_gate_snapshot = capture
    p.handle(normalize(msg(text=(
        "https://github.com/Ylopo/repo/pull/5 "
        "https://github.com/Ylopo/unavailable/pull/6"
    ))))

    admitted = store.requests_for_event("C1", "1.0")
    assert [request.slug for request in admitted] == ["Ylopo/repo#5"]
    record = records(tmp_path)[0]
    assert record["reasons"] == ["gate_snapshot_unavailable"]
    assert len(record["details"][0]) <= 300


def test_shadow_pipeline_wiring_does_not_refresh_persisted_lane_counters(
    monkeypatch,
):
    from boris_intake import daemon

    class StoreFake:
        def __init__(self):
            self.refreshes = 0

        def set_active_lane_count(self, count):
            self.refreshes += 1

        def rate_verdict(self, user_id, limits, now):
            return "ok"

    store = StoreFake()
    monkeypatch.setattr(daemon, "Store", lambda path: store)
    monkeypatch.setattr(
        daemon,
        "IdentityResolver",
        lambda call, token: SimpleNamespace(resolve=lambda user_id: GOOD),
    )

    pipeline = daemon.build_pipeline(SHADOW, "token")
    assert pipeline.deps.rate_verdict("U1") == "ok"
    assert store.refreshes == 0


# ---------------------------------------------------------------------------
# One retry budget per MESSAGE, shared by every GitHub read it causes.
# ---------------------------------------------------------------------------


def test_one_budget_is_shared_by_the_gate_and_the_snapshot_capture(tmp_path):
    """A per-call window would let one 40-link paste spend forty of them inside
    a single synchronous handler call. See `github.RETRY_DEADLINE`."""
    from dataclasses import replace as replace_field

    p, _store, _calls, _captures = _pipeline(tmp_path)
    seen = []
    p.deps = replace_field(
        p.deps,
        pr_info=lambda o, r, n, budget: (seen.append(budget), FOUND_PR)[1],
    )
    original = p.capture_gate_snapshot
    p.capture_gate_snapshot = lambda command, at, budget: (
        seen.append(budget), original(command, at, budget)
    )[1]

    p.handle(normalize(msg(text=(
        "https://github.com/Ylopo/repo/pull/5 "
        "https://github.com/Ylopo/repo/pull/6"
    ))))

    assert len(seen) == 4          # two PR reads, two snapshot captures
    assert all(budget is not None for budget in seen)
    assert len({id(budget) for budget in seen}) == 1


def test_each_message_opens_its_own_window(tmp_path):
    from dataclasses import replace as replace_field

    p, _store, _calls, _captures = _pipeline(tmp_path)
    seen = []
    p.deps = replace_field(
        p.deps,
        pr_info=lambda o, r, n, budget: (seen.append(budget), FOUND_PR)[1],
    )

    p.handle(normalize(msg(ts="1.0")))
    p.handle(normalize(msg(ts="2.0", text="https://github.com/Ylopo/repo/pull/6")))

    assert len(seen) == 2
    assert seen[0] is not seen[1]


def test_the_live_wiring_forwards_the_message_budget_to_the_real_reads(
    monkeypatch, tmp_path
):
    """`build_pipeline`'s two lambdas are where a dropped `budget=` would make
    every read single-shot again while every test with a fake still passed."""
    from boris_intake import daemon
    from boris_intake.github import GitHubRetryBudget
    from boris_intake.models import ReviewCommand, ReviewPolicy

    monkeypatch.setattr(daemon, "Store", lambda path: SimpleNamespace(
        set_active_lane_count=lambda count: None,
        rate_verdict=lambda user_id, limits, now: "ok",
    ))
    monkeypatch.setattr(
        daemon, "IdentityResolver",
        lambda call, token: SimpleNamespace(resolve=lambda user_id: GOOD),
    )
    pipeline = daemon.build_pipeline(SHADOW, "token")
    seen = {}
    monkeypatch.setattr(daemon, "fetch_pr", lambda *args, **kwargs: (
        seen.__setitem__("pr", kwargs.get("budget")), FOUND_PR)[1])
    monkeypatch.setattr(daemon, "fetch_gate_snapshot", lambda *args, **kwargs: (
        seen.__setitem__("snapshot", kwargs.get("budget")), CAPTURED)[1])

    budget = GitHubRetryBudget()
    pipeline.deps.pr_info("Ylopo", "repo", 5, budget)
    command = ReviewCommand(
        owner="Ylopo", repo="repo", pr_number=5, head_sha="f" * 40,
        pr_author="a", requester_user_id="U1", requester_email="a@ylopo.com",
        channel_id="C1", message_ts="1.0", root_thread_ts="1.0",
        permalink="https://ylopo.slack.com/archives/C1/p10",
        policy=ReviewPolicy(False),
    )
    pipeline.capture_gate_snapshot(command, "2026-08-17T00:00:00Z", budget)

    assert seen["pr"] is budget
    assert seen["snapshot"] is budget
    assert isinstance(pipeline.new_budget(), GitHubRetryBudget)


# ---------------------------------------------------------------------------
# A snapshot that could not be READ is an outage; a snapshot refused because the
# head MOVED is not. Only one of them is GITHUB_UNAVAILABLE.
# ---------------------------------------------------------------------------


def test_an_unreadable_snapshot_is_reported_as_github_unavailable(tmp_path):
    p, store, _calls, _captures = _pipeline(tmp_path)
    p.capture_gate_snapshot = lambda command, at, budget: GateSnapshotLookup(
        SnapshotStatus.UNAVAILABLE,
        detail="the PR checks could not be read: GitHub answered with an error",
    )

    p.handle(normalize(msg()))

    assert store.requests_for_event("C1", "1.0") == ()
    record = records(tmp_path)[0]
    assert record["reasons"] == ["github_unavailable"]
    assert "could not be read" in record["details"][0]


def test_a_moved_head_keeps_its_own_terminal_reason(tmp_path):
    p, _store, _calls, _captures = _pipeline(tmp_path)
    p.capture_gate_snapshot = lambda command, at, budget: GateSnapshotLookup(
        SnapshotStatus.HEAD_MOVED, detail="the head moved to abc1234 after the request"
    )

    p.handle(normalize(msg()))

    record = records(tmp_path)[0]
    assert record["reasons"] == ["gate_snapshot_unavailable"]
    assert "moved" in record["details"][0]


# ---------------------------------------------------------------------------
# Part B: the rejection nobody was watching for goes silent.
#
# `event.user_id` for a loop re-post IS the bot's own id, so one row in
# `reject_replies` was shared by every loop rejection, for every pull request,
# and by every other bot rejection too. Measured on
# `Ylopo/forge-content-service#111`: last bot reply 08:50:15, its rejection
# 09:46:23, nothing posted, loop stranded in `rereview_posted` for hours.
# ---------------------------------------------------------------------------

MARKER = "[fixed it. probably. v1]"
DISCOVERY_MARKER = "[swept this one up. v1]"
CHAD = "U07E3AHV686"
DOOR_LIVE = Config(
    mode="live", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT,
    loop_bot_user_id=BOT, loop_marker=MARKER, loop_attributed_user_id=CHAD,
    discovery_bot_user_id=BOT, discovery_marker=DISCOVERY_MARKER,
)
ABSENT_PR = PRLookup(GitHubReadStatus.ABSENT, detail="GitHub reports no such PR")
UNREADABLE_PR = PRLookup(
    GitHubReadStatus.UNAVAILABLE, detail="GitHub answered with an error",
)


def _doors(tmp_path, lookup=ABSENT_PR):
    """A live pipeline with both bot doors wired, and every PR read answering
    `lookup`."""
    from dataclasses import replace as replace_field

    p, store, calls, _captures = _pipeline(tmp_path, cfg=DOOR_LIVE)
    p.deps = replace_field(p.deps, pr_info=lambda o, r, n, budget: lookup)
    return p, store, calls


def loop_repost(ts, number=5):
    return normalize({
        "channel": "C1", "channel_type": "channel", "user": BOT, "bot_id": "B1",
        "ts": ts, "thread_ts": "0.9",
        "text": f"{MARKER}\nhttps://github.com/Ylopo/repo/pull/{number}",
    })


def discovery_post(ts, number=6):
    return normalize({
        "channel": "C1", "channel_type": "channel", "user": BOT, "bot_id": "B1",
        "ts": ts,
        "text": f"https://github.com/Ylopo/repo/pull/{number} {DISCOVERY_MARKER}",
    })


def replies(calls):
    return [kw["text"] for method, kw in calls if method == "chat.postMessage"]


def test_a_loop_rejection_is_not_silenced_by_another_bot_doors_rejection(tmp_path):
    """P1, in the exact shape the incident had: both doors post as the same bot
    user, so keying on `event.user_id` put them in one row."""
    p, _store, calls = _doors(tmp_path)

    p.handle(discovery_post("1.0"))
    p.handle(loop_repost("2.0"))

    assert len(replies(calls)) == 2


def test_a_loop_rejection_is_not_silenced_by_chads_own_rejection(tmp_path):
    """P1 again, and why the attributed requester alone is not the key: the loop
    resolves AS Chad, so a bad link Chad pastes must not silence it."""
    p, _store, calls = _doors(tmp_path)
    chad_typo = normalize({
        "channel": "C1", "channel_type": "channel", "user": CHAD, "ts": "1.0",
        "text": "https://github.com/Ylopo/repo/pull/9",
    })

    p.handle(chad_typo)
    p.handle(loop_repost("2.0"))

    assert len(replies(calls)) == 2


def test_a_human_pasting_bad_links_still_gets_one_reply_per_window(tmp_path):
    """P3: the anti-noise property this cooldown exists for, unchanged."""
    p, _store, calls = _doors(tmp_path)
    first = normalize({
        "channel": "C1", "channel_type": "channel", "user": "U1", "ts": "1.0",
        "text": "https://github.com/Ylopo/repo/pull/9",
    })
    second = normalize({
        "channel": "C1", "channel_type": "channel", "user": "U1", "ts": "2.0",
        "text": "https://github.com/Ylopo/repo/pull/10",
    })

    p.handle(first)
    p.handle(second)

    assert len(replies(calls)) == 1


def test_an_infrastructure_rejection_is_always_delivered(tmp_path):
    """P2: nobody can act on "GitHub could not be read" unless they see it, and
    it cannot become noise -- it only happens while GitHub is failing, and the
    loop's own re-post budget is 12 attempts 10 minutes apart."""
    p, _store, calls = _doors(tmp_path, lookup=UNREADABLE_PR)

    p.handle(loop_repost("1.0"))
    p.handle(loop_repost("2.0"))
    p.handle(loop_repost("3.0"))

    assert len(replies(calls)) == 3
    assert all("couldn't reach GitHub" in text for text in replies(calls))


def test_an_infrastructure_reply_does_not_consume_the_ordinary_cooldown(tmp_path):
    """The bypass must not stamp the row, or an outage reply would silence the
    next genuine bad-link reply for an hour -- trading the bug for a new one."""
    p, _store, calls = _doors(tmp_path, lookup=UNREADABLE_PR)
    p.handle(loop_repost("1.0"))

    from dataclasses import replace as replace_field
    p.deps = replace_field(p.deps, pr_info=lambda o, r, n, budget: ABSENT_PR)
    p.handle(loop_repost("2.0"))

    assert len(replies(calls)) == 2


def test_the_infrastructure_rejection_is_the_one_reported(tmp_path):
    """When a message produces several loud rejections, the outage is the one a
    reader has to see -- and it is what justified skipping the throttle."""
    from dataclasses import replace as replace_field

    p, _store, calls = _doors(tmp_path)
    p.deps = replace_field(
        p.deps,
        pr_info=lambda o, r, n, budget: UNREADABLE_PR if n == 5 else ABSENT_PR,
    )

    p.handle(normalize({
        "channel": "C1", "channel_type": "channel", "user": "U1", "ts": "1.0",
        "text": (
            "https://github.com/Ylopo/repo/pull/7 "
            "https://github.com/Ylopo/repo/pull/5"
        ),
    }))

    assert len(replies(calls)) == 1
    assert "couldn't reach GitHub" in replies(calls)[0]


# ---------------------------------------------------------------------------
# A pull request with no CI checks is reviewed with the CI gate waived.
# ---------------------------------------------------------------------------

def test_a_pull_request_with_no_checks_is_admitted_with_ci_waived(tmp_path):
    p, store, calls, _captures = _pipeline(tmp_path)
    p.capture_gate_snapshot = lambda command, at, budget: CAPTURED

    p.handle(normalize(msg()))

    request, = store.requests_for_event("C1", "1.0")
    assert request.policy.ignore_ci
    assert request.gate_snapshot.checks == ()
    record = records(tmp_path)[0]
    assert record["decision"] == "accept"
    assert calls == []


def test_a_moved_head_reply_is_throttled_not_guaranteed(tmp_path):
    """`GATE_SNAPSHOT_UNAVAILABLE`'s only remaining producer is the moved-head
    refusal, and a moved head is "your request was wrong": the requester pushed
    while requesting. Bypassing the cooldown for that would hand anyone racing a
    push against their own link an un-throttled reply on every post."""
    p, _store, calls, _captures = _pipeline(tmp_path)
    p.capture_gate_snapshot = lambda command, at, budget: GateSnapshotLookup(
        SnapshotStatus.HEAD_MOVED, detail="the head moved to abc1234 after the request"
    )

    p.handle(normalize(msg(ts="1.0")))
    p.handle(normalize(msg(ts="2.0", text="https://github.com/Ylopo/repo/pull/6")))

    assert [record["reasons"] for record in records(tmp_path)] == [
        ["gate_snapshot_unavailable"], ["gate_snapshot_unavailable"],
    ]
    assert len(replies(calls)) == 1


def test_the_gate_rejection_audit_row_carries_its_details(tmp_path):
    """That row is the forensic record of the new classification -- the thing the
    next incident is read from -- and `"details": []` survived."""
    from dataclasses import replace as replace_field

    p, _store, _calls, _captures = _pipeline(tmp_path)
    p.deps = replace_field(p.deps, pr_info=lambda o, r, n, budget: UNREADABLE_PR)

    p.handle(normalize(msg()))

    record = records(tmp_path)[0]
    assert record["decision"] == "reject"
    assert record["reasons"] == ["github_unavailable"]
    assert record["details"] == ["Ylopo/repo#5 — GitHub answered with an error"]
