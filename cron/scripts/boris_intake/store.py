"""Durable intake and lifecycle state.

SQLite rather than JSON because the daemon and the catch-up poll can both write,
and because Slack redelivery makes event dedup correctness-critical.
"""
from __future__ import annotations

import hashlib
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
    terminal_status_confirmed INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_owner TEXT,
    lease_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE (review_identity, gate_fingerprint)
);
CREATE INDEX IF NOT EXISTS review_requests_due
    ON review_requests (workflow_state, next_attempt_at, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS review_requests_lane
    ON review_requests (lane_id) WHERE lane_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS source_reaction_leases (
    channel TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    lease_owner TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    PRIMARY KEY (channel, message_ts)
);
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
CREATE TABLE IF NOT EXISTS request_acceptance_evidence (
    request_id TEXT NOT NULL,
    reference TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (request_id, reference),
    FOREIGN KEY (request_id) REFERENCES review_requests (request_id)
);
CREATE TABLE IF NOT EXISTS review_judgments (
    request_id TEXT PRIMARY KEY,
    canonical_json TEXT NOT NULL,
    judgment_hash TEXT NOT NULL,
    accepted_at REAL NOT NULL,
    task_completed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (request_id) REFERENCES review_requests (request_id)
);
"""

_TERMINAL_STATES = (WorkflowState.COMPLETE, WorkflowState.SLACK_UNAVAILABLE)
DEFAULT_LEASE_SECONDS = 5 * 60

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
    "terminal_status_confirmed",
    "next_attempt_at",
    "last_error",
    "gate_snapshot_json",
    "gate_fingerprint",
}


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self._lease_owner = uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(review_requests)")
            }
            if "lease_owner" not in columns:
                self._db.execute(
                    "ALTER TABLE review_requests ADD COLUMN lease_owner TEXT"
                )
            if "lease_expires_at" not in columns:
                self._db.execute(
                    "ALTER TABLE review_requests ADD COLUMN lease_expires_at REAL"
                )
            if "terminal_status_confirmed" not in columns:
                self._db.execute(
                    "ALTER TABLE review_requests ADD COLUMN "
                    "terminal_status_confirmed INTEGER NOT NULL DEFAULT 0"
                )
            self._db.execute("PRAGMA user_version=5")
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def close(self) -> None:
        self._db.close()

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
            terminal_status_confirmed=bool(row["terminal_status_confirmed"]),
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

    def requests_for_source(
        self, channel: str, ts: str
    ) -> tuple[LifecycleRequest, ...]:
        return self._requests_for_link(channel, ts, "admitted")

    def get_request(self, request_id: str) -> LifecycleRequest | None:
        row = self._db.execute(
            "SELECT * FROM review_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return self._request_from_row(row) if row else None

    def request_for_lane(self, lane_id: str) -> LifecycleRequest | None:
        row = self._db.execute(
            "SELECT * FROM review_requests WHERE lane_id=?", (lane_id,)
        ).fetchone()
        return self._request_from_row(row) if row else None

    def authorize_acceptance_evidence(
        self,
        request_id: str,
        reference: str,
        evidence: dict[str, Any],
    ) -> None:
        if not reference or len(reference) > 512:
            raise ValueError("acceptance evidence reference is invalid")
        if not isinstance(evidence, dict):
            raise ValueError("acceptance evidence must be an object")
        canonical = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
        if len(canonical.encode()) > 64 * 1024:
            raise ValueError("acceptance evidence exceeds 64 KiB")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(
                "SELECT evidence_json FROM request_acceptance_evidence "
                "WHERE request_id=? AND reference=?",
                (request_id, reference),
            ).fetchone()
            if existing:
                if existing["evidence_json"] != canonical:
                    raise ValueError("acceptance evidence authorization conflict")
            else:
                self._db.execute(
                    "INSERT INTO request_acceptance_evidence "
                    "(request_id, reference, evidence_json) VALUES (?,?,?)",
                    (request_id, reference, canonical),
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def acceptance_evidence(self, request_id: str, reference: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT evidence_json FROM request_acceptance_evidence "
            "WHERE request_id=? AND reference=?",
            (request_id, reference),
        ).fetchone()
        return json.loads(row["evidence_json"]) if row else None

    def submit_judgment(self, request_id: str, canonical_json: str, now: float) -> str:
        digest = f"sha256:{hashlib.sha256(canonical_json.encode()).hexdigest()}"
        self._db.execute("BEGIN IMMEDIATE")
        try:
            request = self._db.execute(
                "SELECT workflow_state FROM review_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not request:
                raise ValueError("unknown review request")
            existing = self._db.execute(
                "SELECT canonical_json, judgment_hash FROM review_judgments WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing:
                if existing["canonical_json"] == canonical_json:
                    self._db.commit()
                    return "idempotent"
                detail = json.dumps(
                    {"accepted_hash": existing["judgment_hash"], "rejected_hash": digest},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                self._db.execute(
                    "INSERT INTO request_events "
                    "(request_id, kind, detail_json, at) VALUES (?,?,?,?)",
                    (request_id, "judgment_conflict", detail, now),
                )
                self._db.commit()
                return "conflict"
            if request["workflow_state"] != WorkflowState.REVIEWING.value:
                raise ValueError("review request is not accepting a judgment")
            self._db.execute(
                "INSERT INTO review_judgments "
                "(request_id, canonical_json, judgment_hash, accepted_at) VALUES (?,?,?,?)",
                (request_id, canonical_json, digest, now),
            )
            self._db.execute(
                "INSERT INTO request_events "
                "(request_id, kind, detail_json, at) VALUES (?,?,?,?)",
                (
                    request_id,
                    "judgment_accepted",
                    json.dumps({"judgment_hash": digest}, separators=(",", ":")),
                    now,
                ),
            )
            self._db.commit()
            return "accepted"
        except Exception:
            self._db.rollback()
            raise

    def judgment_for_request(self, request_id: str) -> str | None:
        row = self._db.execute(
            "SELECT canonical_json FROM review_judgments WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return str(row["canonical_json"]) if row else None

    def judgment_accepted_at(self, request_id: str) -> float | None:
        row = self._db.execute(
            "SELECT accepted_at FROM review_judgments WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return float(row["accepted_at"]) if row else None

    def judgment_task_completed(self, request_id: str) -> bool:
        row = self._db.execute(
            "SELECT task_completed FROM review_judgments WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return bool(row and row["task_completed"])

    def mark_judgment_task_completed(self, request_id: str, now: float) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT task_completed FROM review_judgments WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row:
                raise ValueError("no accepted judgment")
            if not row["task_completed"]:
                self._db.execute(
                    "UPDATE review_judgments SET task_completed=1 WHERE request_id=?",
                    (request_id,),
                )
                self._db.execute(
                    "INSERT INTO request_events "
                    "(request_id, kind, detail_json, at) VALUES (?,?,?,?)",
                    (request_id, "reviewer_task_completed", "{}", now),
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

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

    def claim_due(
        self,
        now: float,
        limit: int = 25,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> tuple[LifecycleRequest, ...]:
        terminal = tuple(state.value for state in _TERMINAL_STATES)
        expires_at = now + lease_seconds
        self._db.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                str(row["request_id"])
                for row in self._db.execute(
                    "SELECT request_id FROM review_requests "
                    "WHERE workflow_state NOT IN (?,?) AND next_attempt_at<=? "
                    "AND (lease_owner IS NULL OR lease_expires_at<=?) "
                    "ORDER BY created_at, request_id LIMIT ?",
                    (*terminal, now, now, int(limit)),
                ).fetchall()
            ]
            if not ids:
                self._db.commit()
                return ()
            placeholders = ",".join("?" for _ in ids)
            self._db.execute(
                f"UPDATE review_requests SET lease_owner=?, lease_expires_at=? "
                f"WHERE request_id IN ({placeholders})",
                (self._lease_owner, expires_at, *ids),
            )
            rows = self._db.execute(
                f"SELECT * FROM review_requests WHERE request_id IN ({placeholders}) "
                "AND lease_owner=? ORDER BY created_at, request_id",
                (*ids, self._lease_owner),
            ).fetchall()
            self._db.commit()
            return tuple(self._request_from_row(row) for row in rows)
        except Exception:
            self._db.rollback()
            raise

    def acquire_source_reaction_lease(
        self,
        channel: str,
        message_ts: str,
        now: float,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        expires_at = now + lease_seconds
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                "INSERT INTO source_reaction_leases "
                "(channel, message_ts, lease_owner, lease_expires_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(channel, message_ts) DO UPDATE SET "
                "lease_owner=excluded.lease_owner, "
                "lease_expires_at=excluded.lease_expires_at "
                "WHERE source_reaction_leases.lease_owner=? "
                "OR source_reaction_leases.lease_expires_at<=?",
                (
                    channel,
                    message_ts,
                    self._lease_owner,
                    expires_at,
                    self._lease_owner,
                    now,
                ),
            )
            self._db.commit()
            return cur.rowcount == 1
        except Exception:
            self._db.rollback()
            raise

    def release_source_reaction_lease(
        self, channel: str, message_ts: str
    ) -> bool:
        cur = self._db.execute(
            "DELETE FROM source_reaction_leases "
            "WHERE channel=? AND message_ts=? AND lease_owner=?",
            (channel, message_ts, self._lease_owner),
        )
        self._db.commit()
        return cur.rowcount == 1

    def renew_claim(
        self,
        request_id: str,
        now: float,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        cur = self._db.execute(
            "UPDATE review_requests SET lease_expires_at=? "
            "WHERE request_id=? AND lease_owner=? AND lease_expires_at>?",
            (now + lease_seconds, request_id, self._lease_owner, now),
        )
        self._db.commit()
        return cur.rowcount == 1

    def release_claim(self, request_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE review_requests SET lease_owner=NULL, lease_expires_at=NULL "
            "WHERE request_id=? AND lease_owner=?",
            (request_id, self._lease_owner),
        )
        self._db.commit()
        return cur.rowcount == 1

    def transition(
        self,
        request_id: str,
        expected: WorkflowState,
        target: WorkflowState,
        *,
        now: float | None = None,
        **fields: Any,
    ) -> bool:
        unknown = set(fields) - _TRANSITION_FIELDS
        if unknown:
            raise ValueError(f"unsupported transition fields: {sorted(unknown)}")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            changed_at = time.time() if now is None else float(now)
            row = self._db.execute(
                "SELECT workflow_state, lease_owner, lease_expires_at "
                "FROM review_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if not row or row["workflow_state"] != expected.value:
                self._db.rollback()
                return False
            if row["lease_owner"] is not None and (
                row["lease_owner"] != self._lease_owner
                or row["lease_expires_at"] is None
                or float(row["lease_expires_at"]) <= changed_at
            ):
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
            converted["workflow_state"] = target.value
            converted["updated_at"] = changed_at
            if target in _TERMINAL_STATES:
                converted["completed_at"] = changed_at
            assignments = ", ".join(f"{name}=?" for name in converted)
            params = [*converted.values(), request_id, expected.value]
            cur = self._db.execute(
                f"UPDATE review_requests SET {assignments} "
                "WHERE request_id=? AND workflow_state=? "
                "AND (lease_owner IS NULL OR "
                "(lease_owner=? AND lease_expires_at>?))",
                (*params, self._lease_owner, changed_at),
            )
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.execute(
                "INSERT INTO request_events "
                "(request_id, kind, from_state, to_state, detail_json, at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    request_id,
                    "transition",
                    expected.value,
                    target.value,
                    json.dumps(fields, default=str, separators=(",", ":")),
                    changed_at,
                ),
            )
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def checkpoint(
        self,
        request_id: str,
        expected: WorkflowState,
        *,
        now: float | None = None,
        **fields: Any,
    ) -> bool:
        unknown = set(fields) - _TRANSITION_FIELDS
        if unknown:
            raise ValueError(f"unsupported checkpoint fields: {sorted(unknown)}")
        converted = dict(fields)
        if isinstance(converted.get("semantic_outcome"), SemanticOutcome):
            converted["semantic_outcome"] = converted["semantic_outcome"].value
        if isinstance(converted.get("gate_snapshot_json"), GateSnapshot):
            snapshot = converted["gate_snapshot_json"]
            converted["gate_snapshot_json"] = snapshot.canonical_json()
            converted.setdefault("gate_fingerprint", snapshot.fingerprint)
        changed_at = time.time() if now is None else float(now)
        converted["updated_at"] = changed_at
        assignments = ", ".join(f"{name}=?" for name in converted)
        params = [*converted.values(), request_id, expected.value]
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                f"UPDATE review_requests SET {assignments} "
                "WHERE request_id=? AND workflow_state=? "
                "AND (lease_owner IS NULL OR "
                "(lease_owner=? AND lease_expires_at>?))",
                (*params, self._lease_owner, changed_at),
            )
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.execute(
                "INSERT INTO request_events "
                "(request_id, kind, from_state, to_state, detail_json, at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    request_id,
                    "checkpoint",
                    expected.value,
                    expected.value,
                    json.dumps(fields, default=str, separators=(",", ":")),
                    changed_at,
                ),
            )
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def record_error(
        self,
        request_id: str,
        stage: str,
        error: str,
        next_attempt_at: float,
        *,
        now: float | None = None,
        attempt: int | None = None,
        alert_threshold: int = 5,
    ) -> bool:
        changed_at = time.time() if now is None else float(now)
        detail = json.dumps(
            {"stage": stage, "error": str(error)[:300]}, separators=(",", ":")
        )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                "UPDATE review_requests SET last_error=?, next_attempt_at=?, "
                "updated_at=? WHERE request_id=? "
                "AND (lease_owner IS NULL OR "
                "(lease_owner=? AND lease_expires_at>?))",
                (
                    f"{stage}: {str(error)[:300]}",
                    next_attempt_at,
                    changed_at,
                    request_id,
                    self._lease_owner,
                    changed_at,
                ),
            )
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.execute(
                "INSERT INTO request_events "
                "(request_id, kind, detail_json, at) VALUES (?,?,?,?)",
                (request_id, "error", detail, changed_at),
            )
            if attempt == alert_threshold:
                alert = json.dumps(
                    {"attempt": attempt, "stage": stage},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                self._db.execute(
                    "INSERT INTO request_events "
                    "(request_id, kind, detail_json, at) VALUES (?,?,?,?)",
                    (request_id, "retry_alert", alert, changed_at),
                )
            self._db.commit()
            return True
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

    def error_count(self, request_id: str, stage: str) -> int:
        return sum(
            1
            for event in self.request_events(request_id)
            if event["kind"] == "error" and event["detail"].get("stage") == stage
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
