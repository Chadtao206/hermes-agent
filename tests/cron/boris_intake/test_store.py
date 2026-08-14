import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Limits
from boris_intake.store import Store

L = Limits(per_user_hourly=2, per_user_daily=3, global_concurrent_lanes=1,
           reject_reply_cooldown_seconds=100)


def test_event_dedup(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.seen_event("C1", "1.0") is False
    s.record_event("C1", "1.0")
    assert s.seen_event("C1", "1.0") is True
    assert s.seen_event("C1", "2.0") is False


def test_lane_dedup_returns_existing_id(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.lane_for("k1") is None
    s.record_lane("k1", "t_abc")
    assert s.lane_for("k1") == "t_abc"


def test_hourly_limit(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    for _ in range(2):
        assert s.rate_verdict("U1", L, now) == "ok"
        s.record_request("U1", now)
    assert "hour" in s.rate_verdict("U1", L, now)


def test_hourly_window_rolls_off(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    for _ in range(2):
        s.record_request("U1", now)
    assert s.rate_verdict("U1", L, now + 3601) == "ok"


def test_daily_limit(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    s.record_request("U1", now)
    s.record_request("U1", now + 3601)
    s.record_request("U1", now + 7202)
    assert "day" in s.rate_verdict("U1", L, now + 7203)


def test_limits_are_per_user(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    for _ in range(2):
        s.record_request("U1", now)
    assert s.rate_verdict("U2", L, now) == "ok"


def test_global_concurrency(tmp_path):
    s = Store(tmp_path / "s.db")
    s.set_active_lane_count(1)
    assert "concurrent" in s.rate_verdict("U1", L, 1_000_000.0)


def test_rejection_reply_cooldown(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    assert s.should_reply_rejection("U1", now, 100) is True
    assert s.should_reply_rejection("U1", now + 1, 100) is False
    assert s.should_reply_rejection("U1", now + 101, 100) is True
