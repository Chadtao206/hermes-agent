"""Durable state: event dedup, approval dedup, rate limits, daily cap.

SQLite rather than JSON because the socket listener and the catch-up poll can
both write, and because Slack redelivery makes event dedup correctness-critical
here — a redelivered message must never produce a second approval.

Separate DB from boris_intake.db on purpose: the two pipelines share no rows,
and an independent file means neither can corrupt or lock the other.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from jdi_autoapprove.config import Limits

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (
    channel TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (channel, ts));
CREATE TABLE IF NOT EXISTS approvals (
    idempotency_key TEXT PRIMARY KEY, slug TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    user_id TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS requests_user_at ON requests (user_id, at);
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

    # --- approval dedup ---------------------------------------------------
    def approved_at(self, idempotency_key: str) -> float | None:
        cur = self._db.execute(
            "SELECT at FROM approvals WHERE idempotency_key=?", (idempotency_key,))
        row = cur.fetchone()
        return float(row[0]) if row else None

    def record_approval(self, idempotency_key: str, slug: str, now: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO approvals (idempotency_key, slug, at) VALUES (?,?,?)",
            (idempotency_key, slug, now))
        self._db.commit()

    def approvals_since(self, since: float) -> int:
        cur = self._db.execute("SELECT COUNT(*) FROM approvals WHERE at>=?", (since,))
        return int(cur.fetchone()[0])

    # --- rate limiting ----------------------------------------------------
    def _count_since(self, user_id: str, since: float) -> int:
        cur = self._db.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id=? AND at>=?", (user_id, since))
        return int(cur.fetchone()[0])

    def rate_verdict(self, user_id: str, limits: Limits, now: float) -> str:
        if self.approvals_since(now - 86400) >= limits.global_daily:
            return (f"global cap of {limits.global_daily} auto-approvals in 24h "
                    "reached — auto-approve is paused")
        if self._count_since(user_id, now - 3600) >= limits.per_user_hourly:
            return f"limit of {limits.per_user_hourly} per hour reached"
        if self._count_since(user_id, now - 86400) >= limits.per_user_daily:
            return f"limit of {limits.per_user_daily} per day reached"
        return "ok"

    def record_request(self, user_id: str, now: float) -> None:
        self._db.execute("INSERT INTO requests (user_id, at) VALUES (?,?)", (user_id, now))
        self._db.execute("DELETE FROM requests WHERE at < ?", (now - 172800,))
        self._db.commit()

    def prune(self, now: float | None = None) -> None:
        """Keep seen_events bounded; 14d is far beyond any redelivery window."""
        now = now if now is not None else time.time()
        self._db.execute("DELETE FROM seen_events WHERE CAST(ts AS REAL) < ?",
                         (now - 14 * 86400,))
        self._db.commit()
