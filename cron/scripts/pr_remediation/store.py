"""Durable state for the remediation loop.

Mirrors boris_intake/store.py: compare-and-swap transitions, an append-only
event trail, and short leases so two ticks can never drive the same loop.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from pr_remediation.models import TERMINAL_STATES, LoopRequest, LoopState

LEASE_SECONDS = 300.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS loops (
    loop_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    boris_request_id TEXT NOT NULL,
    root_thread_ts TEXT NOT NULL,
    state TEXT NOT NULL,
    round_count INTEGER NOT NULL DEFAULT 0,
    lane_id TEXT,
    escalation_reason TEXT,
    lease_expires_at REAL NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (owner, repo, pr_number, generation)
);
CREATE TABLE IF NOT EXISTS loop_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS loop_events_loop ON loop_events (loop_id, id);
CREATE TABLE IF NOT EXISTS seen_verdicts (
    boris_request_id TEXT PRIMARY KEY,
    at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _row(self, row: sqlite3.Row) -> LoopRequest:
        return LoopRequest(
            loop_id=row["loop_id"], owner=row["owner"], repo=row["repo"],
            pr_number=row["pr_number"], generation=row["generation"],
            head_sha=row["head_sha"], boris_request_id=row["boris_request_id"],
            root_thread_ts=row["root_thread_ts"], state=LoopState(row["state"]),
            round_count=row["round_count"], lane_id=row["lane_id"],
            escalation_reason=row["escalation_reason"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def active_for_pr(self, owner: str, repo: str, pr_number: int) -> LoopRequest | None:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        cur = self._db.execute(
            f"SELECT * FROM loops WHERE owner=? AND repo=? AND pr_number=? "
            f"AND state NOT IN ({placeholders}) LIMIT 1",
            (owner, repo, pr_number, *(s.value for s in TERMINAL_STATES)),
        )
        row = cur.fetchone()
        return self._row(row) if row else None

    def open_loop(self, *, owner: str, repo: str, pr_number: int, head_sha: str,
                  boris_request_id: str, root_thread_ts: str,
                  now: float) -> LoopRequest | None:
        if self.active_for_pr(owner, repo, pr_number) is not None:
            return None
        cur = self._db.execute(
            "SELECT COALESCE(MAX(generation), 0) AS g FROM loops "
            "WHERE owner=? AND repo=? AND pr_number=?",
            (owner, repo, pr_number),
        )
        generation = cur.fetchone()["g"] + 1
        loop_id = f"lp_{uuid.uuid4().hex}"
        try:
            self._db.execute(
                "INSERT INTO loops (loop_id, owner, repo, pr_number, generation, "
                "head_sha, boris_request_id, root_thread_ts, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (loop_id, owner, repo, pr_number, generation, head_sha,
                 boris_request_id, root_thread_ts, LoopState.OBSERVED.value, now, now),
            )
        except sqlite3.IntegrityError:
            # Another writer opened a loop for this (owner, repo, pr_number,
            # generation) between our active_for_pr check and this INSERT.
            # Honour the documented contract (None means "already active")
            # instead of leaking the race as an unhandled exception.
            self._db.rollback()
            return None
        self._append(loop_id, "opened", {"generation": generation}, now)
        self._db.commit()
        return self.get(loop_id)

    def get(self, loop_id: str) -> LoopRequest | None:
        cur = self._db.execute("SELECT * FROM loops WHERE loop_id=?", (loop_id,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def transition(self, loop_id: str, expected: LoopState, target: LoopState,
                   *, now: float, **fields: Any) -> bool:
        columns = ["state=?", "updated_at=?"]
        values: list[Any] = [target.value, now]
        for key, value in fields.items():
            columns.append(f"{key}=?")
            values.append(value)
        cur = self._db.execute(
            f"UPDATE loops SET {', '.join(columns)} WHERE loop_id=? AND state=?",
            (*values, loop_id, expected.value),
        )
        if cur.rowcount == 0:
            self._db.commit()
            return False
        self._append(loop_id, "transition",
                     {"from": expected.value, "to": target.value, **{
                         k: v for k, v in fields.items()}}, now)
        self._db.commit()
        return True

    def record_note(self, loop_id: str, kind: str, detail: dict, now: float) -> None:
        self._append(loop_id, kind, detail, now)
        self._db.commit()

    def _append(self, loop_id: str, kind: str, detail: dict, at: float) -> None:
        self._db.execute(
            "INSERT INTO loop_events (loop_id, kind, detail_json, at) VALUES (?,?,?,?)",
            (loop_id, kind, json.dumps(detail, separators=(",", ":"), default=str), at),
        )

    def events(self, loop_id: str) -> tuple[dict, ...]:
        cur = self._db.execute(
            "SELECT kind, detail_json, at FROM loop_events WHERE loop_id=? ORDER BY id",
            (loop_id,))
        return tuple(
            {"kind": r["kind"], "detail": json.loads(r["detail_json"]), "at": r["at"]}
            for r in cur.fetchall()
        )

    def claim_due(self, *, now: float, limit: int) -> tuple[LoopRequest, ...]:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        cur = self._db.execute(
            f"SELECT * FROM loops WHERE state NOT IN ({placeholders}) "
            f"AND lease_expires_at <= ? AND next_attempt_at <= ? "
            f"ORDER BY created_at LIMIT ?",
            (*(s.value for s in TERMINAL_STATES), now, now, limit),
        )
        claimed = []
        for row in cur.fetchall():
            taken = self._db.execute(
                "UPDATE loops SET lease_expires_at=? WHERE loop_id=? AND lease_expires_at<=?",
                (now + LEASE_SECONDS, row["loop_id"], now),
            )
            if taken.rowcount:
                claimed.append(self._row(row))
        self._db.commit()
        return tuple(claimed)

    def release(self, loop_id: str) -> None:
        self._db.execute("UPDATE loops SET lease_expires_at=0 WHERE loop_id=?", (loop_id,))
        self._db.commit()

    def mark_verdict_seen(self, boris_request_id: str, now: float) -> bool:
        """False if this verdict was already consumed."""
        try:
            self._db.execute(
                "INSERT INTO seen_verdicts (boris_request_id, at) VALUES (?,?)",
                (boris_request_id, now))
            self._db.commit()
            return True
        except sqlite3.IntegrityError:
            # The INSERT already opened a write transaction; without a
            # rollback here it stays open, holding a write lock, until the
            # next successful commit on this connection happens to close it.
            self._db.rollback()
            return False
