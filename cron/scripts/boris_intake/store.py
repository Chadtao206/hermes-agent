"""Durable intake and lifecycle state.

SQLite rather than JSON because the daemon and the catch-up poll can both write,
and because Slack redelivery makes event dedup correctness-critical.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from boris_intake.config import Limits
from boris_intake.models import (
    AdmissionResult,
    GateSnapshot,
    LifecycleRequest,
    NormalizedEvent,
    ReviewCommand,
    ReviewPolicy,
    SemanticOutcome,
    WorkflowState,
)

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
CREATE TABLE IF NOT EXISTS review_requests (
    request_id TEXT PRIMARY KEY,
    review_identity TEXT NOT NULL,
    gate_fingerprint TEXT NOT NULL,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    pr_author TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    gate_snapshot_json TEXT NOT NULL,
    requester_user_id TEXT NOT NULL,
    requester_email TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    source_message_ts TEXT NOT NULL,
    root_thread_ts TEXT NOT NULL,
    permalink TEXT NOT NULL,
    status_message_ts TEXT,
    lane_id TEXT,
    workflow_state TEXT NOT NULL,
    semantic_outcome TEXT,
    validated_result_json TEXT,
    github_review_id TEXT,
    github_review_url TEXT,
    publication_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE (review_identity, gate_fingerprint)
);
CREATE INDEX IF NOT EXISTS review_requests_due
    ON review_requests (workflow_state, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS event_requests (
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('admitted', 'duplicate')),
    PRIMARY KEY (channel, ts, ordinal),
    FOREIGN KEY (request_id) REFERENCES review_requests (request_id)
);
CREATE TABLE IF NOT EXISTS request_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    at REAL NOT NULL,
    FOREIGN KEY (request_id) REFERENCES review_requests (request_id)
);
CREATE INDEX IF NOT EXISTS request_events_request_id
    ON request_events (request_id, id);
"""

_TERMINAL_STATES = (WorkflowState.COMPLETE, WorkflowState.SLACK_UNAVAILABLE)

_ALLOWED_TRANSITIONS = {
    WorkflowState.ADMITTED: {WorkflowState.STATUS_MESSAGE_PENDING},
    WorkflowState.STATUS_MESSAGE_PENDING: {
        WorkflowState.STARTING_REVIEW,
        WorkflowState.DELIVERY_PENDING,
    },
    WorkflowState.STARTING_REVIEW: {
        WorkflowState.REVIEWING,
        WorkflowState.DELIVERY_PENDING,
    },
    WorkflowState.REVIEWING: {
        WorkflowState.GATE_WAIT_PENDING,
        WorkflowState.PUBLICATION_PENDING,
        WorkflowState.DELIVERY_PENDING,
    },
    WorkflowState.GATE_WAIT_PENDING: {
        WorkflowState.PUBLICATION_PENDING,
        WorkflowState.DELIVERY_PENDING,
    },
    WorkflowState.PUBLICATION_PENDING: {WorkflowState.DELIVERY_PENDING},
    WorkflowState.DELIVERY_PENDING: {
        WorkflowState.COMPLETE,
        WorkflowState.SLACK_UNAVAILABLE,
    },
    WorkflowState.COMPLETE: set(),
    WorkflowState.SLACK_UNAVAILABLE: set(),
}

_TRANSITION_FIELDS = {
    "status_message_ts",
    "lane_id",
    "semantic_outcome",
    "validated_result_json",
    "github_review_id",
    "github_review_url",
    "publication_attempts",
    "delivery_attempts",
    "next_attempt_at",
    "last_error",
    "gate_snapshot_json",
    "gate_fingerprint",
}


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.execute("PRAGMA user_version=2")
        self._db.commit()

    def _request_from_row(self, row: sqlite3.Row) -> LifecycleRequest:
        policy_raw = json.loads(row["policy_json"])
        outcome = row["semantic_outcome"]
        return LifecycleRequest(
            request_id=row["request_id"],
            owner=row["owner"],
            repo=row["repo"],
            pr_number=int(row["pr_number"]),
            head_sha=row["head_sha"],
            pr_author=row["pr_author"],
            policy=ReviewPolicy(
                ignore_ci=bool(policy_raw["ignore_ci"]),
                rubric_version=str(policy_raw["rubric_version"]),
            ),
            policy_fingerprint=row["policy_fingerprint"],
            gate_snapshot=GateSnapshot.from_json(row["gate_snapshot_json"]),
            requester_user_id=row["requester_user_id"],
            requester_email=row["requester_email"],
            channel_id=row["source_channel"],
            message_ts=row["source_message_ts"],
            root_thread_ts=row["root_thread_ts"],
            permalink=row["permalink"],
            workflow_state=WorkflowState(row["workflow_state"]),
            semantic_outcome=SemanticOutcome(outcome) if outcome else None,
            status_message_ts=row["status_message_ts"],
            lane_id=row["lane_id"],
            validated_result_json=row["validated_result_json"],
            github_review_id=row["github_review_id"],
            github_review_url=row["github_review_url"],
            publication_attempts=int(row["publication_attempts"]),
            delivery_attempts=int(row["delivery_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            last_error=row["last_error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            completed_at=(float(row["completed_at"])
                          if row["completed_at"] is not None else None),
        )

    def _requests_for_link(self, channel: str, ts: str,
                           disposition: str) -> tuple[LifecycleRequest, ...]:
        rows = self._db.execute(
            "SELECT r.* FROM event_requests e "
            "JOIN review_requests r ON r.request_id=e.request_id "
            "WHERE e.channel=? AND e.ts=? AND e.disposition=? "
            "ORDER BY e.ordinal",
            (channel, ts, disposition),
        ).fetchall()
        return tuple(self._request_from_row(row) for row in rows)

    def get_request(self, request_id: str) -> LifecycleRequest | None:
        row = self._db.execute(
            "SELECT * FROM review_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return self._request_from_row(row) if row else None

    def requests_for_event(self, channel: str, ts: str) -> tuple[LifecycleRequest, ...]:
        return self._requests_for_link(channel, ts, "admitted")

    def admit_event(
        self,
        event: NormalizedEvent,
        commands: tuple[ReviewCommand, ...],
        gate_snapshots: tuple[GateSnapshot, ...],
        now: float,
    ) -> AdmissionResult:
        if len(commands) != len(gate_snapshots):
            raise ValueError("one Gate Snapshot is required per Review Command")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            seen = self._db.execute(
                "SELECT 1 FROM seen_events WHERE channel=? AND ts=?",
                (event.channel_id, event.ts),
            ).fetchone()
            if seen:
                result = AdmissionResult(
                    admitted=self._requests_for_link(
                        event.channel_id, event.ts, "admitted"),
                    duplicates=self._requests_for_link(
                        event.channel_id, event.ts, "duplicate"),
                    redelivered=True,
                )
                self._db.commit()
                return result

            self._db.execute(
                "INSERT INTO seen_events (channel, ts) VALUES (?,?)",
                (event.channel_id, event.ts),
            )
            admitted: list[LifecycleRequest] = []
            duplicates: list[LifecycleRequest] = []
            for ordinal, (command, snapshot) in enumerate(zip(commands, gate_snapshots)):
                existing = self._db.execute(
                    "SELECT * FROM review_requests "
                    "WHERE review_identity=? AND gate_fingerprint=?",
                    (command.idempotency_key, snapshot.fingerprint),
                ).fetchone()
                if existing:
                    request = self._request_from_row(existing)
                    duplicates.append(request)
                    disposition = "duplicate"
                else:
                    request_id = f"br_{uuid.uuid4().hex}"
                    values = {
                        "request_id": request_id,
                        "review_identity": command.idempotency_key,
                        "gate_fingerprint": snapshot.fingerprint,
                        "owner": command.owner,
                        "repo": command.repo,
                        "pr_number": command.pr_number,
                        "head_sha": command.head_sha,
                        "pr_author": command.pr_author,
                        "policy_json": command.policy.canonical_json(),
                        "policy_fingerprint": command.policy.fingerprint(),
                        "gate_snapshot_json": snapshot.canonical_json(),
                        "requester_user_id": command.requester_user_id,
                        "requester_email": command.requester_email,
                        "source_channel": command.channel_id,
                        "source_message_ts": command.message_ts,
                        "root_thread_ts": command.root_thread_ts,
                        "permalink": command.permalink,
                        "workflow_state": WorkflowState.ADMITTED.value,
                        "created_at": now,
                        "updated_at": now,
                    }
                    self._db.execute(
                        """
                        INSERT INTO review_requests (
                            request_id, review_identity, gate_fingerprint,
                            owner, repo, pr_number, head_sha, pr_author,
                            policy_json, policy_fingerprint, gate_snapshot_json,
                            requester_user_id, requester_email, source_channel,
                            source_message_ts, root_thread_ts, permalink,
                            workflow_state, created_at, updated_at
                        ) VALUES (
                            :request_id, :review_identity, :gate_fingerprint,
                            :owner, :repo, :pr_number, :head_sha, :pr_author,
                            :policy_json, :policy_fingerprint, :gate_snapshot_json,
                            :requester_user_id, :requester_email, :source_channel,
                            :source_message_ts, :root_thread_ts, :permalink,
                            :workflow_state, :created_at, :updated_at
                        )
                        """,
                        values,
                    )
                    self._db.execute(
                        "INSERT INTO request_events "
                        "(request_id, kind, to_state, at) VALUES (?,?,?,?)",
                        (request_id, "admitted", WorkflowState.ADMITTED.value, now),
                    )
                    request = self.get_request(request_id)
                    if request is None:
                        raise RuntimeError("admitted request could not be reloaded")
                    admitted.append(request)
                    disposition = "admitted"
                self._db.execute(
                    "INSERT INTO event_requests "
                    "(channel, ts, ordinal, request_id, disposition) VALUES (?,?,?,?,?)",
                    (event.channel_id, event.ts, ordinal,
                     request.request_id, disposition),
                )
            if admitted and event.user_id:
                self._db.execute(
                    "INSERT INTO requests (user_id, at) VALUES (?,?)",
                    (event.user_id, now),
                )
                self._db.execute("DELETE FROM requests WHERE at < ?", (now - 172800,))
            self._db.commit()
            return AdmissionResult(tuple(admitted), tuple(duplicates), False)
        except Exception:
            self._db.rollback()
            raise

    def claim_due(self, now: float, limit: int = 25) -> tuple[LifecycleRequest, ...]:
        terminal = tuple(state.value for state in _TERMINAL_STATES)
        rows = self._db.execute(
            "SELECT * FROM review_requests "
            "WHERE workflow_state NOT IN (?,?) AND next_attempt_at<=? "
            "ORDER BY created_at, request_id LIMIT ?",
            (*terminal, now, int(limit)),
        ).fetchall()
        return tuple(self._request_from_row(row) for row in rows)

    def transition(
        self,
        request_id: str,
        expected: WorkflowState,
        target: WorkflowState,
        **fields: Any,
    ) -> bool:
        unknown = set(fields) - _TRANSITION_FIELDS
        if unknown:
            raise ValueError(f"unsupported transition fields: {sorted(unknown)}")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT workflow_state FROM review_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row or row["workflow_state"] != expected.value:
                self._db.rollback()
                return False
            if target not in _ALLOWED_TRANSITIONS[expected]:
                raise ValueError(f"invalid transition: {expected.value} -> {target.value}")
            converted = dict(fields)
            if isinstance(converted.get("semantic_outcome"), SemanticOutcome):
                converted["semantic_outcome"] = converted["semantic_outcome"].value
            if isinstance(converted.get("gate_snapshot_json"), GateSnapshot):
                snapshot = converted["gate_snapshot_json"]
                converted["gate_snapshot_json"] = snapshot.canonical_json()
                converted.setdefault("gate_fingerprint", snapshot.fingerprint)
            changed_at = time.time()
            converted["workflow_state"] = target.value
            converted["updated_at"] = changed_at
            if target in _TERMINAL_STATES:
                converted["completed_at"] = changed_at
            assignments = ", ".join(f"{name}=?" for name in converted)
            params = [*converted.values(), request_id, expected.value]
            cur = self._db.execute(
                f"UPDATE review_requests SET {assignments} "
                "WHERE request_id=? AND workflow_state=?",
                params,
            )
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.execute(
                "INSERT INTO request_events "
                "(request_id, kind, from_state, to_state, detail_json, at) "
                "VALUES (?,?,?,?,?,?)",
                (request_id, "transition", expected.value, target.value,
                 json.dumps(fields, default=str, separators=(",", ":")), changed_at),
            )
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def record_error(self, request_id: str, stage: str, error: str,
                     next_attempt_at: float) -> None:
        changed_at = time.time()
        detail = json.dumps(
            {"stage": stage, "error": str(error)[:300]}, separators=(",", ":")
        )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE review_requests SET last_error=?, next_attempt_at=?, "
                "updated_at=? WHERE request_id=?",
                (f"{stage}: {str(error)[:300]}", next_attempt_at,
                 changed_at, request_id),
            )
            self._db.execute(
                "INSERT INTO request_events "
                "(request_id, kind, detail_json, at) VALUES (?,?,?,?)",
                (request_id, "error", detail, changed_at),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def request_events(self, request_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._db.execute(
            "SELECT kind, from_state, to_state, detail_json, at "
            "FROM request_events WHERE request_id=? ORDER BY id",
            (request_id,),
        ).fetchall()
        return tuple(
            {
                "kind": row["kind"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "detail": json.loads(row["detail_json"]),
                "at": float(row["at"]),
            }
            for row in rows
        )

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
