"""Durable state for the remediation loop.

Mirrors boris_intake/store.py: compare-and-swap transitions, an append-only
event trail, and short owner-stamped leases so two ticks can never drive the
same loop.

Every mutator opens an explicit `BEGIN IMMEDIATE` *inside* its `try` and rolls
back on any exception, the `BEGIN` included. That makes a whole bug class
unrepresentable: a raise between a write and its commit can no longer leave a
write applied-but-uncommitted on this long-lived connection, where a later
unrelated commit would flush a state change with no matching event (breaking
the append-only trail) and where the open write transaction would hold the
database lock against every other loop. Keeping the `BEGIN` itself under the
guard closes the same hole one level up: were the connection ever already in a
transaction, `cannot start a transaction within a transaction` would otherwise
escape unrolled-back and wedge every later mutator on this connection forever.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from pr_remediation.models import TERMINAL_STATES, LoopRequest, LoopState

LEASE_SECONDS = 300.0
_USER_VERSION = 1

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
    lease_owner TEXT,
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

# The one INSERT conflict open_loop is allowed to read as "someone else already
# opened this loop". Every other constraint failure is a programming error and
# must not be disguised as a lost race.
_GENERATION_CONFLICT = (
    "UNIQUE constraint failed: "
    "loops.owner, loops.repo, loops.pr_number, loops.generation"
)

# Columns transition() may write. Field names are interpolated into the SET
# clause, so anything outside this set is refused rather than trusted: CPython
# passes non-identifier keys through **kwargs unchanged, which would otherwise
# let a caller typo (or a splatted config/verdict dict) inject SQL.
_TRANSITION_FIELDS = frozenset({
    "round_count",
    "lane_id",
    "escalation_reason",
    "head_sha",
    "next_attempt_at",
})

# Forward-only edges. Terminal states are absorbing: a ready or escalated loop
# is finished, and re-driving one would re-run every side effect (opening a
# lane, posting to GitHub) the loop already performed.
_ALLOWED_TRANSITIONS: dict[LoopState, frozenset[LoopState]] = {
    LoopState.OBSERVED: frozenset({
        LoopState.REMEDIATING,
        LoopState.AWAITING_HEAD_MOVE,
        LoopState.REREVIEW_POSTED,
        LoopState.READY,
        LoopState.ESCALATED,
    }),
    LoopState.REMEDIATING: frozenset({
        LoopState.REMEDIATING,
        LoopState.AWAITING_HEAD_MOVE,
        LoopState.REREVIEW_POSTED,
        LoopState.READY,
        LoopState.ESCALATED,
    }),
    LoopState.AWAITING_HEAD_MOVE: frozenset({
        LoopState.REMEDIATING,
        LoopState.REREVIEW_POSTED,
        LoopState.READY,
        LoopState.ESCALATED,
    }),
    LoopState.REREVIEW_POSTED: frozenset({
        LoopState.REMEDIATING,
        LoopState.AWAITING_HEAD_MOVE,
        LoopState.REREVIEW_POSTED,
        LoopState.READY,
        LoopState.ESCALATED,
    }),
    LoopState.READY: frozenset(),
    LoopState.ESCALATED: frozenset(),
}

# Positive whitelist for the batch claim path. Selecting the states we know how
# to drive (rather than everything that is not terminal) means a row carrying an
# unrecognised state string is simply never claimed, so it cannot abort — and
# with it wedge — a batch that spans every other open PR.
_CLAIMABLE_STATES = tuple(
    state.value for state in LoopState if state not in TERMINAL_STATES
)


class CorruptLoopRow(ValueError):
    """A persisted row carries a state string this build does not recognise.

    Raised in place of the enum's anonymous ValueError so the bad row is
    attributable. It can surface while a write transaction is open --
    `open_loop` reads through `active_for_pr` inside its own BEGIN IMMEDIATE --
    but never escapes one: every mutator rolls back before it propagates, so a
    corrupt row fails loudly without leaving the database lock held.
    """

    def __init__(self, loop_id: str, state: Any):
        super().__init__(f"loop {loop_id} has unrecognised state {state!r}")
        self.loop_id = loop_id
        self.state = state


def _is_generation_conflict(exc: sqlite3.IntegrityError) -> bool:
    return (
        getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE"
        and str(exc) == _GENERATION_CONFLICT
    )


def _is_duplicate_verdict(exc: sqlite3.IntegrityError) -> bool:
    return getattr(exc, "sqlite_errorname", "") in (
        "SQLITE_CONSTRAINT_PRIMARYKEY",
        "SQLITE_CONSTRAINT_UNIQUE",
    )


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        # Identifies this process's leases. Without it release() and
        # transition() cannot tell "my lease" from "the lease a later tick took
        # after mine lapsed", and two ticks can drive one loop.
        self._lease_owner = uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            columns = {
                str(row["name"]) for row in self._db.execute("PRAGMA table_info(loops)")
            }
            if "lease_owner" not in columns:
                self._db.execute("ALTER TABLE loops ADD COLUMN lease_owner TEXT")
            self._db.execute(f"PRAGMA user_version={_USER_VERSION}")
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def close(self) -> None:
        self._db.close()

    def _row(self, row: sqlite3.Row) -> LoopRequest:
        try:
            state = LoopState(row["state"])
        except ValueError as exc:
            raise CorruptLoopRow(row["loop_id"], row["state"]) from exc
        return LoopRequest(
            loop_id=row["loop_id"], owner=row["owner"], repo=row["repo"],
            pr_number=row["pr_number"], generation=row["generation"],
            head_sha=row["head_sha"], boris_request_id=row["boris_request_id"],
            root_thread_ts=row["root_thread_ts"], state=state,
            round_count=row["round_count"], lane_id=row["lane_id"],
            escalation_reason=row["escalation_reason"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def active_for_pr(self, owner: str, repo: str, pr_number: int) -> LoopRequest | None:
        # Deliberately "not terminal" rather than the claim path's positive
        # whitelist: a row we cannot parse must still block a rival loop for
        # this PR, and failing loudly here is contained to this one PR.
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
        try:
            self._db.execute("BEGIN IMMEDIATE")
            if self.active_for_pr(owner, repo, pr_number) is not None:
                self._db.rollback()
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
            except sqlite3.IntegrityError as exc:
                if not _is_generation_conflict(exc):
                    # A NOT NULL (or any other) constraint failure is a bug in
                    # the caller, not the documented "already active" race.
                    raise
                # Another writer opened a loop for this (owner, repo,
                # pr_number, generation) between our active_for_pr check and
                # this INSERT. Honour the documented contract (None means
                # "already active") instead of leaking the race.
                self._db.rollback()
                return None
            self._append(loop_id, "opened", {"generation": generation}, now)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get(loop_id)

    def get(self, loop_id: str) -> LoopRequest | None:
        cur = self._db.execute("SELECT * FROM loops WHERE loop_id=?", (loop_id,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def transition(self, loop_id: str, expected: LoopState, target: LoopState,
                   *, now: float, **fields: Any) -> bool:
        unknown = set(fields) - _TRANSITION_FIELDS
        if unknown:
            raise ValueError(f"unsupported transition fields: {sorted(unknown)}")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT state, lease_owner, lease_expires_at FROM loops WHERE loop_id=?",
                (loop_id,),
            ).fetchone()
            if row is None or row["state"] != expected.value:
                self._db.rollback()
                return False
            # A lease held by someone else — or our own lapsed lease, which a
            # later tick may already have taken over — means we are no longer
            # the driver and must not write.
            if row["lease_owner"] is not None and (
                row["lease_owner"] != self._lease_owner
                or row["lease_expires_at"] is None
                or float(row["lease_expires_at"]) <= now
            ):
                self._db.rollback()
                return False
            if target not in _ALLOWED_TRANSITIONS[expected]:
                raise ValueError(
                    f"invalid transition: {expected.value} -> {target.value}")
            columns = {**fields, "state": target.value, "updated_at": now}
            assignments = ", ".join(f"{name}=?" for name in columns)
            cur = self._db.execute(
                f"UPDATE loops SET {assignments} WHERE loop_id=? AND state=? "
                "AND (lease_owner IS NULL OR "
                "(lease_owner=? AND lease_expires_at>?))",
                (*columns.values(), loop_id, expected.value, self._lease_owner, now),
            )
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._append(loop_id, "transition",
                         {"from": expected.value, "to": target.value, **fields}, now)
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def record_note(self, loop_id: str, kind: str, detail: dict, now: float) -> None:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._append(loop_id, kind, detail, now)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def _append(self, loop_id: str, kind: str, detail: dict, at: float) -> None:
        """Append one audit event. Deliberately does NOT commit: it must land
        in the same transaction as the write it describes, so the trail can
        never gain an event for a rolled-back write or lose one for a
        committed write."""
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
        states = ",".join("?" for _ in _CLAIMABLE_STATES)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            ids = [
                str(row["loop_id"])
                for row in self._db.execute(
                    f"SELECT loop_id FROM loops WHERE state IN ({states}) "
                    "AND next_attempt_at<=? "
                    "AND (lease_owner IS NULL OR lease_expires_at<=?) "
                    "ORDER BY created_at, loop_id LIMIT ?",
                    (*_CLAIMABLE_STATES, now, now, int(limit)),
                ).fetchall()
            ]
            if not ids:
                self._db.commit()
                return ()
            loop_ids = ",".join("?" for _ in ids)
            # The state guard is repeated here: a row that went terminal
            # between the SELECT and this UPDATE must not be claimed.
            self._db.execute(
                f"UPDATE loops SET lease_owner=?, lease_expires_at=? "
                f"WHERE loop_id IN ({loop_ids}) AND state IN ({states}) "
                "AND (lease_owner IS NULL OR lease_expires_at<=?)",
                (self._lease_owner, now + LEASE_SECONDS, *ids, *_CLAIMABLE_STATES, now),
            )
            # Re-read what we actually hold: the pre-UPDATE snapshot could
            # hand the caller an already-wrong state or round_count.
            rows = self._db.execute(
                f"SELECT * FROM loops WHERE loop_id IN ({loop_ids}) "
                "AND lease_owner=? AND lease_expires_at>? "
                "ORDER BY created_at, loop_id",
                (*ids, self._lease_owner, now),
            ).fetchall()
            claimed = tuple(self._row(row) for row in rows)
            self._db.commit()
            return claimed
        except Exception:
            self._db.rollback()
            raise

    def claim(self, loop_id: str, *, now: float) -> bool:
        """Take the lease on exactly this loop. False if we cannot have it.

        `claim_due` leases a whole due batch, which is no use to the verdict
        path: that one finds its loop by `active_for_pr` and must lease *that*
        row before it starts doing irreversible work (opening a remediation
        lane, posting to Slack). Without this the driver either races a
        concurrent tick or skips leasing entirely, and its closing
        `transition()` silently returns False after the side effects fired --
        orphaning the lane and losing the round.

        False means: no such loop, a terminal (or unrecognised) state that must
        never be driven again, or an unexpired lease held by someone else.
        Re-taking a lease we already hold succeeds and extends it, so a driver
        can call this without tracking whether it already owns one.
        """
        states = ",".join("?" for _ in _CLAIMABLE_STATES)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            cur = self._db.execute(
                f"UPDATE loops SET lease_owner=?, lease_expires_at=? "
                f"WHERE loop_id=? AND state IN ({states}) "
                "AND (lease_owner IS NULL OR lease_owner=? OR lease_expires_at<=?)",
                (self._lease_owner, now + LEASE_SECONDS, loop_id,
                 *_CLAIMABLE_STATES, self._lease_owner, now),
            )
            if cur.rowcount != 1:
                # Nothing was written, so roll back rather than commit an empty
                # transaction: an empty commit would flush anything else left
                # pending on this long-lived connection.
                self._db.rollback()
                return False
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def renew_claim(self, loop_id: str, *, now: float) -> bool:
        """Extend our own lease. False if we no longer hold it.

        LEASE_SECONDS is short relative to a drive batch, which can walk many
        loops with a network call each. A tick whose lease lapses mid-batch
        finds its later `transition()` calls returning False after the side
        effects have already fired, so a long-running driver renews between
        steps and stops on False rather than continuing unsafely.

        Owner- and expiry-guarded like the sibling store's `renew_claim`: a
        lease that already lapsed cannot be renewed, because a later tick may
        have taken the row over in the meantime. Re-acquire with `claim` (which
        will refuse if that happened) instead.
        """
        try:
            self._db.execute("BEGIN IMMEDIATE")
            cur = self._db.execute(
                "UPDATE loops SET lease_expires_at=? "
                "WHERE loop_id=? AND lease_owner=? AND lease_expires_at>?",
                (now + LEASE_SECONDS, loop_id, self._lease_owner, now),
            )
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def release(self, loop_id: str) -> bool:
        """Drop our own lease. False if we no longer hold it.

        Owner-guarded on purpose: a slow tick finishing after its lease lapsed
        must not hand the current owner's row to a third tick.
        """
        try:
            self._db.execute("BEGIN IMMEDIATE")
            cur = self._db.execute(
                "UPDATE loops SET lease_owner=NULL, lease_expires_at=0 "
                "WHERE loop_id=? AND lease_owner=?",
                (loop_id, self._lease_owner),
            )
            self._db.commit()
            return cur.rowcount == 1
        except Exception:
            self._db.rollback()
            raise

    def mark_verdict_seen(self, boris_request_id: str, now: float) -> bool:
        """False if this verdict was already consumed."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "INSERT INTO seen_verdicts (boris_request_id, at) VALUES (?,?)",
                (boris_request_id, now))
            self._db.commit()
            return True
        except sqlite3.IntegrityError as exc:
            if not _is_duplicate_verdict(exc):
                self._db.rollback()
                raise
            # The INSERT already opened a write transaction; without a
            # rollback here it stays open, holding a write lock, until the
            # next successful commit on this connection happens to close it.
            self._db.rollback()
            return False
        except Exception:
            self._db.rollback()
            raise
