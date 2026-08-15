import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config
from boris_intake.daemon import Pipeline, normalize
from boris_intake.gate import GateDeps
from boris_intake.models import GateSnapshot, Identity, PRInfo, WorkflowState
from boris_intake.slack_io import SlackIO
from boris_intake.store import Store

BOT = "U0B9TK6B41M"
LIVE = Config(mode="live", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT)
SHADOW = Config(mode="shadow", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT)
GOOD = Identity("U1", "a@ylopo.com", "T02PND103", False, False, False, False)
SNAPSHOT = GateSnapshot("sha256:empty", "2026-08-14T12:00:00Z", ())


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
        pr_info=lambda o, r, n: PRInfo("f" * 40, "zjenkins", "OPEN"),
        rate_verdict=lambda uid: store.rate_verdict(uid, cfg.limits, 1_000_000.0),
        now=lambda: 1_000_000.0,
    )

    def capture(command, captured_at):
        captures.append((command.slug, captured_at))
        return SNAPSHOT

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
    p, store, calls, captures = _pipeline(tmp_path)
    p.handle(normalize(msg(channel="C_OTHER")))
    rec = records(tmp_path)[0]
    assert rec["decision"] == "reject"
    assert rec["reasons"] == ["channel_not_allowed"]


def test_unknown_flag_rejects_before_snapshot_or_admission(tmp_path):
    p, store, calls, captures = _pipeline(tmp_path)

    p.handle(normalize(msg(text=msg()["text"] + " --ignore-cii")))

    assert captures == []
    assert store.requests_for_event("C1", "1.0") == ()
    assert records(tmp_path)[0]["reasons"] == ["unknown_flag"]


def test_rejection_reply_failure_is_audited(tmp_path):
    p, store, calls, captures = _pipeline(tmp_path)

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
    p, store, calls, captures = _pipeline(tmp_path)
    original_capture = p.capture_gate_snapshot

    def capture(command, captured_at):
        if command.repo == "unavailable":
            return None
        return original_capture(command, captured_at)

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
