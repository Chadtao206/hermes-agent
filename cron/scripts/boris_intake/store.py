"""Durable intake state: event dedup, lane dedup, rate limits, reply cooldown.

SQLite rather than JSON because the daemon and the catch-up poll can both write,
and because Slack redelivery makes event dedup correctness-critical.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from boris_intake.config import Limits

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (
    channel TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (channel, ts));
CREATE TABLE IF NOT EXISTS lanes (
    idempotency_key TEXT PRIMARY KEY, lane_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    user_id TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS requests_user_at ON requests (user_id, at);
CREATE TABLE IF NOT EXISTS counters (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reject_replies (user_id TEXT PRIMARY KEY, at REAL NOT NULL);
"""


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # --- event dedup ------------------------------------------------------
    def seen_event(self, channel: str, ts: str) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM seen_events WHERE channel=? AND ts=?", (channel, ts))
        return cur.fetchone() is not None

    def record_event(self, channel: str, ts: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO seen_events (channel, ts) VALUES (?,?)",
            (channel, ts))
        self._db.commit()

    # --- lane dedup -------------------------------------------------------
    def lane_for(self, idempotency_key: str) -> str | None:
        cur = self._db.execute(
            "SELECT lane_id FROM lanes WHERE idempotency_key=?", (idempotency_key,))
        row = cur.fetchone()
        return row[0] if row else None

    def record_lane(self, idempotency_key: str, lane_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO lanes (idempotency_key, lane_id) VALUES (?,?)",
            (idempotency_key, lane_id))
        self._db.commit()

    # --- rate limiting ----------------------------------------------------
    def _count_since(self, user_id: str, since: float) -> int:
        cur = self._db.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id=? AND at>=?", (user_id, since))
        return int(cur.fetchone()[0])

    def rate_verdict(self, user_id: str, limits: Limits, now: float) -> str:
        if self.active_lane_count() >= limits.global_concurrent_lanes:
            return (f"{limits.global_concurrent_lanes} concurrent reviews already "
                    "running; try again shortly")
        if self._count_since(user_id, now - 3600) >= limits.per_user_hourly:
            return f"limit of {limits.per_user_hourly} reviews per hour reached"
        if self._count_since(user_id, now - 86400) >= limits.per_user_daily:
            return f"limit of {limits.per_user_daily} reviews per day reached"
        return "ok"

    def record_request(self, user_id: str, now: float) -> None:
        self._db.execute("INSERT INTO requests (user_id, at) VALUES (?,?)", (user_id, now))
        self._db.execute("DELETE FROM requests WHERE at < ?", (now - 172800,))
        self._db.commit()

    def active_lane_count(self) -> int:
        cur = self._db.execute("SELECT v FROM counters WHERE k='active_lanes'")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def set_active_lane_count(self, n: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO counters (k, v) VALUES ('active_lanes', ?)", (int(n),))
        self._db.commit()

    # --- rejection reply throttle ----------------------------------------
    def should_reply_rejection(self, user_id: str, now: float, cooldown: int) -> bool:
        cur = self._db.execute(
            "SELECT at FROM reject_replies WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row and now - float(row[0]) < cooldown:
            return False
        self._db.execute(
            "INSERT OR REPLACE INTO reject_replies (user_id, at) VALUES (?,?)",
            (user_id, now))
        self._db.commit()
        return True
