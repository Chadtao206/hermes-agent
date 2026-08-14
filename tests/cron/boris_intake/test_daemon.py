import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config
from boris_intake.daemon import Pipeline, normalize
from boris_intake.gate import GateDeps
from boris_intake.models import Identity, PRInfo
from boris_intake.slack_io import SlackIO
from boris_intake.store import Store

BOT = "U0B9TK6B41M"
LIVE = Config(mode="live", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT)
SHADOW = Config(mode="shadow", channels=("C1",), passive_channels=("C1",), bot_user_id=BOT)
GOOD = Identity("U1", "a@ylopo.com", "T02PND103", False, False, False, False)
KEY = "borisslack:Ylopo/repo#5@" + "f" * 40


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

    def slack_call(token, method, **kw):
        calls.append((method, kw))
        return {"ok": True}

    deps = GateDeps(
        identity=lambda uid: GOOD,
        pr_info=lambda o, r, n: PRInfo("f" * 40, "zjenkins", "OPEN"),
        lane_for=store.lane_for,
        rate_verdict=lambda uid: store.rate_verdict(uid, cfg.limits, 1_000_000.0),
        now=lambda: 1_000_000.0,
    )
    p = Pipeline(cfg=cfg, deps=deps, store=store,
                 slack=SlackIO(slack_call, "t", dry_run=cfg.mode != "live"),
                 audit_path=tmp_path / "audit.jsonl",
                 create_lane=lambda cmd: "t_new")
    return p, store, calls


def records(tmp_path):
    return [json.loads(l) for l in
            (tmp_path / "audit.jsonl").read_text().strip().split("\n")]


def test_live_creates_lane_reacts_and_acks(tmp_path):
    p, store, calls = _pipeline(tmp_path)
    p.handle(normalize(msg()))
    methods = [m for m, _ in calls]
    assert "reactions.add" in methods and "chat.postMessage" in methods
    reaction = next(kw for method, kw in calls if method == "reactions.add")
    assert reaction["name"] == "rubbing"
    assert store.lane_for(KEY) == "t_new"


def test_shadow_creates_nothing_but_audits(tmp_path):
    p, store, calls = _pipeline(tmp_path, cfg=SHADOW)
    p.handle(normalize(msg()))
    assert calls == []
    assert store.lane_for(KEY) is None
    assert records(tmp_path)[0]["mode"] == "shadow"
    assert records(tmp_path)[0]["decision"] == "accept"


def test_redelivered_event_is_ignored(tmp_path):
    p, store, calls = _pipeline(tmp_path)
    ev = normalize(msg())
    p.handle(ev)
    before = len(calls)
    p.handle(ev)
    assert len(calls) == before


def test_every_decision_is_audited(tmp_path):
    p, store, calls = _pipeline(tmp_path)
    p.handle(normalize(msg(channel="C_OTHER")))
    rec = records(tmp_path)[0]
    assert rec["decision"] == "reject"
    assert rec["reasons"] == ["channel_not_allowed"]
