import sqlite3
import threading

import pytest

from pr_remediation.models import LoopState
from pr_remediation.store import _USER_VERSION, CorruptLoopRow, Store


def open_one(store, *, pr=42, head="a" * 40, now=1_000_000.0):
    return store.open_loop(
        owner="Ylopo", repo="example", pr_number=pr, head_sha=head,
        boris_request_id=f"br_{head[:8]}", root_thread_ts="1.0", now=now,
    )


def test_opening_a_loop_starts_at_generation_one_in_observed(tmp_path):
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    assert loop.generation == 1
    assert loop.state is LoopState.OBSERVED
    assert loop.round_count == 0


# --- the reaction anchor: status_message_ts ---------------------------------

def test_open_loop_persists_the_status_message_anchor(tmp_path):
    """The column the reaction is anchored on (see loop.py's
    _clear_running_signal / _open_lane), distinct from root_thread_ts."""
    store = Store(tmp_path / "r.db")
    loop = store.open_loop(
        owner="Ylopo", repo="example", pr_number=42, head_sha="a" * 40,
        boris_request_id="br_1", root_thread_ts="1.0",
        status_message_ts="2.0", now=1_000_000.0,
    )
    assert loop.status_message_ts == "2.0"
    assert loop.root_thread_ts == "1.0", "the two anchors are independent columns"
    assert store.get(loop.loop_id).status_message_ts == "2.0"


def test_open_loop_without_a_status_message_leaves_the_anchor_null(tmp_path):
    """status_message_ts is nullable in the source; a verdict with none must
    open a loop rather than raise or fabricate one."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    assert loop.status_message_ts is None


def test_transition_can_move_the_reaction_anchor(tmp_path):
    """`open_loop` writes the anchor once, and a loop outlives the verdict that
    opened it: every later Boris verdict posts its own status message, so the
    round a verdict starts has to be able to carry the anchor with it (see
    loop.py's _open_lane). That makes status_message_ts a writable column on
    the same compare-and-swap that records the round -- and, because the field
    name is interpolated into the SET clause, one the allowlist has to admit
    explicitly."""
    store = Store(tmp_path / "r.db")
    loop = store.open_loop(
        owner="Ylopo", repo="example", pr_number=42, head_sha="a" * 40,
        boris_request_id="br_1", root_thread_ts="1.0",
        status_message_ts="2.0", now=1_000_000.0,
    )
    assert store.transition(loop.loop_id, LoopState.OBSERVED,
                            LoopState.REMEDIATING, now=1_000_050.0,
                            round_count=1, status_message_ts="7.0") is True
    assert store.get(loop.loop_id).status_message_ts == "7.0"
    assert store.get(loop.loop_id).root_thread_ts == "1.0", \
        "the two anchors stay independent columns"
    moved = [e["detail"] for e in store.events(loop.loop_id)
             if e["kind"] == "transition"]
    assert moved == [{"from": "observed", "to": "remediating",
                      "round_count": 1, "status_message_ts": "7.0"}], \
        "the trail says which message that round's reaction went on"


def test_transition_can_clear_the_reaction_anchor(tmp_path):
    """Nullable at the column, so nullable through the writer: nothing here
    decides whether a null anchor is a sensible thing to write (loop.py does,
    and refuses to), but a store that could not represent one would push that
    decision into a SQL error."""
    store = Store(tmp_path / "r.db")
    loop = store.open_loop(
        owner="Ylopo", repo="example", pr_number=42, head_sha="a" * 40,
        boris_request_id="br_1", root_thread_ts="1.0",
        status_message_ts="2.0", now=1_000_000.0,
    )
    assert store.transition(loop.loop_id, LoopState.OBSERVED,
                            LoopState.REMEDIATING, now=1_000_050.0,
                            status_message_ts=None) is True
    assert store.get(loop.loop_id).status_message_ts is None


def test_a_second_open_while_active_is_refused(tmp_path):
    """One live loop per PR; a duplicate verdict must not start a rival."""
    store = Store(tmp_path / "r.db")
    open_one(store)
    assert open_one(store, head="b" * 40) is None


def test_a_new_generation_opens_after_the_previous_one_ended(tmp_path):
    store = Store(tmp_path / "r.db")
    first = open_one(store)
    store.transition(first.loop_id, LoopState.OBSERVED, LoopState.ESCALATED,
                     now=1_000_100.0, escalation_reason="round_cap")
    second = open_one(store, head="b" * 40, now=1_000_200.0)
    assert second is not None
    assert second.generation == 2
    assert second.round_count == 0, "a fresh generation resets the round counter"


def test_transition_is_compare_and_swap(tmp_path):
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    assert store.transition(loop.loop_id, LoopState.OBSERVED,
                            LoopState.REMEDIATING, now=1_000_050.0) is True
    # The same stale expectation must not apply twice.
    assert store.transition(loop.loop_id, LoopState.OBSERVED,
                            LoopState.REMEDIATING, now=1_000_060.0) is False
    # A refused CAS wrote nothing, so it must roll back rather than commit an
    # empty transaction.
    assert store._db.in_transaction is False


def test_every_transition_lands_in_the_audit_trail(tmp_path):
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store.transition(loop.loop_id, LoopState.OBSERVED, LoopState.REMEDIATING,
                     now=1_000_050.0, round_count=1)
    store.record_note(loop.loop_id, "shadow_decision", {"would": "open lane"}, 1_000_051.0)
    kinds = [e["kind"] for e in store.events(loop.loop_id)]
    assert kinds == ["opened", "transition", "shadow_decision"]


def test_claim_due_leases_a_row_so_two_ticks_cannot_share_it(tmp_path):
    store = Store(tmp_path / "r.db")
    open_one(store)
    assert len(store.claim_due(now=1_000_010.0, limit=10)) == 1
    assert store.claim_due(now=1_000_011.0, limit=10) == ()


def test_a_released_row_can_be_claimed_again(tmp_path):
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store.claim_due(now=1_000_010.0, limit=10)
    store.release(loop.loop_id)
    assert len(store.claim_due(now=1_000_012.0, limit=10)) == 1


def test_terminal_loops_are_never_claimed(tmp_path):
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store.transition(loop.loop_id, LoopState.OBSERVED, LoopState.READY, now=1_000_100.0)
    assert store.claim_due(now=1_000_200.0, limit=10) == ()


def test_a_duplicate_mark_verdict_seen_does_not_leave_a_write_lock_held(tmp_path):
    """A dangling transaction after the IntegrityError path holds a write
    lock on the database until something else on this connection happens to
    commit. mark_verdict_seen runs on essentially every tick (it returns
    False for the overwhelmingly common case of an already-consumed
    verdict), so a missing rollback there means a write transaction is left
    open almost continuously, which can surface much later as an unrelated
    'database is locked' error for any other writer."""
    store = Store(tmp_path / "r.db")
    assert store.mark_verdict_seen("br_dup", 1_000_000.0) is True
    assert store.mark_verdict_seen("br_dup", 1_000_001.0) is False
    assert store._db.in_transaction is False


class _RacingConnection:
    """Wraps a real sqlite3.Connection and, on the first INSERT into
    `loops`, first slips in a conflicting row through the same real
    connection (as a concurrent writer would) so the caller's own INSERT
    then hits a genuine UNIQUE violation. sqlite3.Connection.execute is
    read-only and can't be monkeypatched directly, hence the wrapper."""

    def __init__(self, real):
        self._real = real
        self._armed = True

    def execute(self, sql, params=()):
        if self._armed and sql.startswith("INSERT INTO loops"):
            self._armed = False
            (_loop_id, owner, repo, pr_number, generation, _head_sha,
             _boris_request_id, _root_thread_ts, status_message_ts, state,
             created_at, updated_at) = params
            self._real.execute(
                sql,
                ("lp_racer", owner, repo, pr_number, generation, "c" * 40,
                 "br_racer", "1.0", status_message_ts, state, created_at,
                 updated_at),
            )
            self._real.commit()
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_open_loop_returns_none_instead_of_raising_on_a_racing_insert(tmp_path):
    """open_loop's contract is 'returns None if a loop is already active'.
    Between the active_for_pr check and the INSERT, another writer could
    win a race and insert a row for the same (owner, repo, pr_number,
    generation) first, so open_loop's own INSERT hits a genuine UNIQUE
    violation and sqlite3.IntegrityError propagates out instead of the
    documented None. Simulate the race deterministically by inserting the
    conflicting row through the store's own connection immediately before
    the real INSERT runs, rather than a flaky thread-timing test."""
    store = Store(tmp_path / "r.db")
    store._db = _RacingConnection(store._db)
    assert open_one(store) is None


# --- C1: caller-supplied field names must never reach the SET clause -------

def test_transition_refuses_field_names_that_are_not_writable_columns(tmp_path):
    """CPython passes non-identifier string keys through **kwargs unchanged,
    so an unvalidated field name is interpolated straight into the UPDATE's
    SET clause. A caller typo -- or any future call site that splats a
    config- or verdict-derived dict -- could then write arbitrary SQL, and
    the resulting garbage `state` also wedges every later read of the row."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    with pytest.raises(ValueError, match="unsupported transition fields"):
        store.transition(loop.loop_id, LoopState.OBSERVED, LoopState.REMEDIATING,
                         now=1_000_050.0, **{"round_count=99, state": "corrupt"})
    with pytest.raises(ValueError, match="unsupported transition fields"):
        store.transition(loop.loop_id, LoopState.OBSERVED, LoopState.REMEDIATING,
                         now=1_000_051.0, not_a_column=1)
    after = store.get(loop.loop_id)
    assert after.state is LoopState.OBSERVED
    assert after.round_count == 0
    assert store._db.in_transaction is False


def _insert_raw(store, *, loop_id, state, created_at, pr_number=7, generation=1):
    store._db.execute(
        "INSERT INTO loops (loop_id, owner, repo, pr_number, generation, head_sha, "
        "boris_request_id, root_thread_ts, state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (loop_id, "Ylopo", "example", pr_number, generation, "d" * 40,
         "br_raw", "1.0", state, created_at, created_at),
    )
    store._db.commit()


def test_a_row_with_an_unknown_state_cannot_wedge_a_claim_batch(tmp_path):
    """One corrupted row must not stop every other PR's loop. Parsing the
    persisted state inside claim_due's loop -- after the lease UPDATEs have
    been issued but before the commit -- means a single bad row raises
    mid-batch and leaves a write transaction holding the database lock, so
    every other loop then fails with 'database is locked'."""
    store = Store(tmp_path / "r.db")
    _insert_raw(store, loop_id="lp_corrupt", state="not_a_state", created_at=999_999.0)
    healthy = open_one(store)

    claimed = store.claim_due(now=1_000_010.0, limit=10)
    assert [c.loop_id for c in claimed] == [healthy.loop_id]
    assert store._db.in_transaction is False, "no write transaction may be left open"
    # The bad row still fails loudly, and attributably, when read directly.
    with pytest.raises(CorruptLoopRow):
        store.get("lp_corrupt")


def test_a_raise_inside_claim_due_rolls_back_instead_of_holding_the_lock(tmp_path):
    """Any exception between claim_due's lease UPDATE and its commit must
    roll back; otherwise the write is left applied-but-uncommitted on this
    long-lived connection with the database lock still held."""
    store = Store(tmp_path / "r.db")
    open_one(store)

    def boom(row):
        raise RuntimeError("row parse crash")

    store._row = boom
    with pytest.raises(RuntimeError, match="row parse crash"):
        store.claim_due(now=1_000_010.0, limit=10)
    assert store._db.in_transaction is False
    del store._row
    # The lease was rolled back with everything else, so the row is free.
    assert len(store.claim_due(now=1_000_011.0, limit=10)) == 1


# --- M6: the grace window is entered once, and only from `remediating` -----


def test_the_grace_window_has_no_self_edge(tmp_path):
    """The guarantee the whole no-op judgement rests on.

    `awaiting_head_move` is one wait for a push that may be moments late, and
    `HEAD_MOVE_GRACE_SECONDS`'s own comment cites the absent self-edge as what
    makes it exactly one: the next look either finds the push or calls the
    round a no-op. With a self-edge here, a loop could be parked again on every
    due-pass visit and the window would never end -- the wedge this state was
    added to close, rebuilt inside it. It raises rather than returning False so
    a driver that tried would be loud, not quietly stuck.
    """
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    assert store.transition(loop.loop_id, LoopState.OBSERVED,
                            LoopState.REMEDIATING, now=1_000_100.0) is True
    assert store.transition(loop.loop_id, LoopState.REMEDIATING,
                            LoopState.AWAITING_HEAD_MOVE, now=1_000_200.0) is True
    with pytest.raises(ValueError, match="invalid transition"):
        store.transition(loop.loop_id, LoopState.AWAITING_HEAD_MOVE,
                         LoopState.AWAITING_HEAD_MOVE, now=1_000_300.0)
    assert store.get(loop.loop_id).state is LoopState.AWAITING_HEAD_MOVE
    assert store._db.in_transaction is False


@pytest.mark.parametrize("origin", [LoopState.OBSERVED,
                                    LoopState.REREVIEW_POSTED])
def test_the_grace_window_is_reachable_from_remediating_alone(tmp_path, origin):
    """A table is a claim about the driver, so an edge nothing takes is a false
    one. `loop.py`'s `_await_head_move` is the only write that targets this
    state, and it is reached only from `_route_due`'s last branch -- which needs
    a `LaneFacts`, which `_observe_lane` produces only for a `_lane_pending`
    row: `remediating` with a lane, or `awaiting_head_move` itself, whose own
    self-edge is refused above. Neither of these two states can ever satisfy
    that, so these edges were listed and dead.
    """
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    if origin is not LoopState.OBSERVED:
        assert store.transition(loop.loop_id, LoopState.OBSERVED, origin,
                                now=1_000_100.0) is True
    with pytest.raises(ValueError, match="invalid transition"):
        store.transition(loop.loop_id, origin, LoopState.AWAITING_HEAD_MOVE,
                         now=1_000_200.0)
    assert store.get(loop.loop_id).state is origin
    assert store._db.in_transaction is False


@pytest.mark.parametrize("origin", [LoopState.AWAITING_HEAD_MOVE,
                                    LoopState.REREVIEW_POSTED])
def test_a_pull_request_that_stops_being_open_can_go_stale_from_either_wait(
        tmp_path, origin):
    """The commonest ending there is, from the two states a loop spends most of
    its life waiting in. Both edges are live in the driver -- a merge during the
    grace window, and a merge while Boris re-reviews -- and neither was pinned
    here."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    if origin is LoopState.AWAITING_HEAD_MOVE:
        assert store.transition(loop.loop_id, LoopState.OBSERVED,
                                LoopState.REMEDIATING, now=1_000_100.0) is True
        assert store.transition(loop.loop_id, LoopState.REMEDIATING, origin,
                                now=1_000_150.0) is True
    else:
        assert store.transition(loop.loop_id, LoopState.OBSERVED, origin,
                                now=1_000_100.0) is True
    assert store.transition(loop.loop_id, origin, LoopState.STALE,
                            now=1_000_200.0, escalation_reason="pr_not_open") is True
    assert store.get(loop.loop_id).state is LoopState.STALE


# --- I2: terminal states are absorbing ------------------------------------

@pytest.mark.parametrize("terminal", [LoopState.READY, LoopState.ESCALATED])
def test_a_terminal_loop_can_never_be_driven_again(tmp_path, terminal):
    """A ready or escalated loop is finished. Re-driving one would re-run
    every side effect it already performed (opening a lane, posting to
    GitHub, notifying Slack)."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    assert store.transition(loop.loop_id, LoopState.OBSERVED, terminal,
                            now=1_000_100.0) is True
    with pytest.raises(ValueError, match="invalid transition"):
        store.transition(loop.loop_id, terminal, LoopState.OBSERVED, now=1_000_200.0)
    with pytest.raises(ValueError, match="invalid transition"):
        store.transition(loop.loop_id, terminal, LoopState.REMEDIATING, now=1_000_201.0)
    assert store.get(loop.loop_id).state is terminal
    assert store._db.in_transaction is False


# --- I3: leases are owned -------------------------------------------------

def test_release_only_drops_our_own_lease(tmp_path):
    """An unconditional release lets a slow tick, finishing after its own
    lease lapsed, hand the current owner's row to a third tick."""
    db = tmp_path / "r.db"
    owner_tick = Store(db)
    other_tick = Store(db)
    loop = open_one(owner_tick)
    assert len(owner_tick.claim_due(now=1_000_010.0, limit=10)) == 1

    assert other_tick.release(loop.loop_id) is False
    assert other_tick.claim_due(now=1_000_020.0, limit=10) == ()
    assert owner_tick.release(loop.loop_id) is True


def test_transition_refuses_a_driver_whose_lease_has_lapsed(tmp_path):
    """Nothing detects lease expiry today, so a slow tick can keep writing
    after a second tick has taken the row over -- both drive the loop and
    every side effect before the final write happens twice."""
    db = tmp_path / "r.db"
    slow_tick = Store(db)
    next_tick = Store(db)
    loop = open_one(slow_tick)
    assert len(slow_tick.claim_due(now=1_000_010.0, limit=10)) == 1
    # The lease lapses (LEASE_SECONDS = 300) and the next tick takes over.
    assert len(next_tick.claim_due(now=1_000_400.0, limit=10)) == 1

    assert slow_tick.transition(loop.loop_id, LoopState.OBSERVED,
                                LoopState.REMEDIATING, now=1_000_401.0) is False
    assert slow_tick.get(loop.loop_id).state is LoopState.OBSERVED
    assert next_tick.transition(loop.loop_id, LoopState.OBSERVED,
                                LoopState.REMEDIATING, now=1_000_402.0) is True


# --- I4: claim_due must guard on state and return what it claimed ---------

class _MutatingConnection:
    """Applies `mutation` through the store's own connection immediately
    before the first lease UPDATE, standing in for a writer that changed the
    row between claim_due's SELECT and its UPDATE."""

    def __init__(self, real, mutation, params):
        self._real = real
        self._mutation = mutation
        self._params = params
        self._armed = True

    def execute(self, sql, params=()):
        if self._armed and sql.startswith("UPDATE loops SET lease"):
            self._armed = False
            self._real.execute(self._mutation, self._params)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_claim_due_does_not_claim_a_row_that_went_terminal_after_the_select(tmp_path):
    """The conditional UPDATE filters on the lease only, so a loop that
    reached a terminal state between the SELECT and the UPDATE is claimed
    and driven anyway."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store._db = _MutatingConnection(
        store._db, "UPDATE loops SET state=? WHERE loop_id=?",
        (LoopState.ESCALATED.value, loop.loop_id))
    assert store.claim_due(now=1_000_010.0, limit=10) == ()


def test_claim_due_returns_the_claimed_row_not_the_pre_claim_snapshot(tmp_path):
    """The returned LoopRequest is built from the row as read before the
    claim, so a caller can be handed an already-wrong state or round_count
    and act on it."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store._db = _MutatingConnection(
        store._db, "UPDATE loops SET round_count=? WHERE loop_id=?",
        (9, loop.loop_id))
    claimed = store.claim_due(now=1_000_010.0, limit=10)
    assert len(claimed) == 1
    assert claimed[0].round_count == 9


# --- I5: no gap between a write and its commit ----------------------------

def test_a_raise_between_the_write_and_the_commit_lands_no_silent_change(tmp_path):
    """execute -> _append -> commit with nothing guaranteeing the gap leaves
    the UPDATE applied-but-uncommitted when the audit append raises. The
    next unrelated commit on this connection then flushes a state change
    with no matching event, breaking the append-only trail."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)

    def boom(*args, **kwargs):
        raise RuntimeError("audit crash")

    store._append = boom
    with pytest.raises(RuntimeError, match="audit crash"):
        store.transition(loop.loop_id, LoopState.OBSERVED, LoopState.REMEDIATING,
                         now=1_000_050.0, round_count=1)
    assert store._db.in_transaction is False
    del store._append

    # An unrelated later commit must not flush the abandoned state change.
    store.record_note(loop.loop_id, "tick", {"phase": "later"}, 1_000_060.0)
    assert store.get(loop.loop_id).state is LoopState.OBSERVED
    assert store.get(loop.loop_id).round_count == 0
    assert [e["kind"] for e in store.events(loop.loop_id)] == ["opened", "tick"]


# --- I6: connection hardening ---------------------------------------------

def test_a_store_opens_on_a_fresh_directory_in_wal_mode(tmp_path):
    """'Safe on a fresh database' has to include a fresh directory: the cron
    creates its state directory on first run."""
    db = tmp_path / "state" / "remediation" / "r.db"
    store = Store(db)
    assert db.exists()
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert open_one(store).generation == 1


def test_a_store_waits_on_a_busy_writer_instead_of_failing_the_tick(tmp_path):
    """WAL still serialises writers, and every mutator takes the write lock up
    front with BEGIN IMMEDIATE. Without an explicit busy timeout a tick that
    collides with an in-flight write raises 'database is locked' immediately
    and drops the loop, rather than waiting the other writer out."""
    store = Store(tmp_path / "r.db")
    assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000


def test_a_store_stamps_the_schema_version_it_wrote(tmp_path):
    """The stamp is how a future migration tells an old file from a current
    one; an unstamped database reads as version 0 and is indistinguishable
    from one written before the column layout it assumes."""
    db = tmp_path / "r.db"
    store = Store(db)
    assert _USER_VERSION != 0, "0 is the unstamped default and cannot be a version"
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == _USER_VERSION
    store.close()
    # Reopening an existing database re-stamps rather than leaving it stale.
    assert Store(db)._db.execute("PRAGMA user_version").fetchone()[0] == _USER_VERSION


def test_a_v1_database_migrates_in_the_status_message_column_without_loss(tmp_path):
    """A database written before status_message_ts existed (schema version 1,
    an 11-column `loops` table with no such column) must gain it and keep its
    data -- not be dropped and recreated, and not raise. This is the migration
    the store's ALTER-TABLE-in-__init__ mechanism is for; it must follow that
    existing mechanism (like lease_owner before it) rather than a new one."""
    db = tmp_path / "r.db"
    raw = sqlite3.connect(str(db))
    raw.execute(
        """
        CREATE TABLE loops (
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
        )
        """
    )
    raw.execute(
        "INSERT INTO loops (loop_id, owner, repo, pr_number, generation, "
        "head_sha, boris_request_id, root_thread_ts, state, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("lp_old", "Ylopo", "example", 7, 1, "a" * 40, "br_old", "1.0",
         LoopState.REMEDIATING.value, 999_000.0, 999_000.0),
    )
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()

    store = Store(db)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == _USER_VERSION

    loop = store.get("lp_old")
    assert loop is not None, "the pre-existing row must survive the migration"
    assert loop.root_thread_ts == "1.0", "existing data must not be lost"
    assert loop.state is LoopState.REMEDIATING
    assert loop.status_message_ts is None, \
        "an old row has no recoverable anchor -- see loop.py for why that means skip, not backfill"

    # The store is fully usable afterward: a fresh loop can still be opened,
    # and it carries the new column like any post-migration row.
    fresh = open_one(store, pr=8)
    assert fresh.status_message_ts is None


# --- I7: only the intended UNIQUE conflict is swallowed -------------------

def test_open_loop_does_not_report_a_programming_error_as_already_active(tmp_path):
    """A blanket `except sqlite3.IntegrityError` cannot tell the UNIQUE race
    it targets from a NOT NULL violation, so a bad call is silently reported
    as 'a loop is already active' and the verdict is dropped."""
    store = Store(tmp_path / "r.db")
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.open_loop(owner=None, repo="example", pr_number=42,
                        head_sha="a" * 40, boris_request_id="br_x",
                        root_thread_ts="1.0", now=1_000_000.0)
    assert store._db.in_transaction is False


# --- concurrency ----------------------------------------------------------

def test_concurrent_ticks_yield_exactly_one_winner_and_no_errors(tmp_path):
    """Each worker builds its own Store *inside its own thread*: sqlite3
    connections are single-thread by default, so a Store built on the main
    thread would make every worker raise ProgrammingError while a harness
    that only counted successful claims still reported one winner. Both the
    winner count and the per-thread exception count are asserted on, and the
    ticks that lost end the way a real tick does -- by releasing what they
    thought they held, which must not free the winner's lease."""
    db = tmp_path / "r.db"
    seed = Store(db)
    loop_id = open_one(seed).loop_id
    seed.close()

    workers = 8
    barrier = threading.Barrier(workers, timeout=30)
    claims: list[int] = []
    errors: list[BaseException] = []

    def tick():
        try:
            store = Store(db)
            try:
                barrier.wait()
                claimed = store.claim_due(now=1_000_010.0, limit=10)
                claims.append(len(claimed))
                if not claimed:
                    store.release(loop_id)
            finally:
                store.close()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert [repr(e) for e in errors] == []
    assert len(claims) == workers
    assert sum(claims) == 1, f"exactly one tick may claim the loop, got {claims}"
    observer = Store(db)
    assert observer.claim_due(now=1_000_020.0, limit=10) == (), \
        "the losing ticks' releases must not have freed the winner's lease"


# --- claim(): leasing one specific loop ------------------------------------

def test_claim_leases_the_one_loop_the_verdict_path_is_about_to_drive(tmp_path):
    """The verdict path finds its loop with active_for_pr, not claim_due, so
    without a single-loop claim it must either race a concurrent tick or skip
    leasing altogether -- and then its closing transition() returns False after
    the irreversible work (opening a remediation lane, posting to Slack) has
    already happened, orphaning the lane and losing the round."""
    db = tmp_path / "r.db"
    driver = Store(db)
    rival = Store(db)
    loop = open_one(driver)

    assert driver.claim(loop.loop_id, now=1_000_010.0) is True
    # A real lease, not a bookkeeping flag: no other tick can take the row.
    assert rival.claim(loop.loop_id, now=1_000_011.0) is False
    assert rival.claim_due(now=1_000_012.0, limit=10) == ()
    # And the holder can finish the round it started.
    assert driver.transition(loop.loop_id, LoopState.OBSERVED,
                             LoopState.REMEDIATING, now=1_000_013.0) is True
    assert driver._db.in_transaction is False


def test_claiming_a_lease_we_already_hold_succeeds_and_extends_it(tmp_path):
    """Idempotent on purpose: a driver that already claimed the loop (here via
    the batch path) must be able to call claim() again without tracking
    whether it holds one, and the re-claim has to push the expiry out or a
    long round still lapses mid-flight."""
    db = tmp_path / "r.db"
    driver = Store(db)
    rival = Store(db)
    loop = open_one(driver)
    assert len(driver.claim_due(now=1_000_010.0, limit=10)) == 1

    assert driver.claim(loop.loop_id, now=1_000_200.0) is True
    # LEASE_SECONDS is 300: the original lease expired at 1_000_310, the
    # re-claim's at 1_000_500, so a rival at 1_000_400 must still be refused.
    assert rival.claim(loop.loop_id, now=1_000_400.0) is False
    assert rival.claim_due(now=1_000_400.0, limit=10) == ()


def test_claim_refuses_a_missing_terminal_or_unparseable_loop(tmp_path):
    """A finished loop must never be re-driven -- every side effect it
    performed would run again -- and a row whose state this build cannot parse
    is not something to drive either. Both are the caller's cue to stop, not an
    exception to handle."""
    store = Store(tmp_path / "r.db")
    finished = open_one(store)
    store.transition(finished.loop_id, LoopState.OBSERVED, LoopState.READY,
                     now=1_000_100.0)
    _insert_raw(store, loop_id="lp_corrupt", state="not_a_state", created_at=999_999.0)

    assert store.claim(finished.loop_id, now=1_000_200.0) is False
    assert store.claim("lp_nonexistent", now=1_000_200.0) is False
    assert store.claim("lp_corrupt", now=1_000_200.0) is False
    assert store._db.in_transaction is False, "a refused claim writes nothing and rolls back"


# --- renew_claim(): leases that outlive a long batch ------------------------

def test_renew_claim_keeps_a_long_batch_from_losing_its_row_mid_flight(tmp_path):
    """LEASE_SECONDS is 300 while one drive batch can walk many loops with a
    network call each. Without renewal the tick's own lease lapses part-way
    through, a later tick takes the row over, and the first tick's remaining
    transition() calls silently return False after its side effects fired."""
    db = tmp_path / "r.db"
    long_tick = Store(db)
    next_tick = Store(db)
    loop = open_one(long_tick)
    assert len(long_tick.claim_due(now=1_000_010.0, limit=10)) == 1

    assert long_tick.renew_claim(loop.loop_id, now=1_000_200.0) is True
    # Past the original 1_000_310 expiry, inside the renewed 1_000_500 one.
    assert next_tick.claim_due(now=1_000_400.0, limit=10) == ()
    assert next_tick.claim(loop.loop_id, now=1_000_400.0) is False
    assert long_tick.transition(loop.loop_id, LoopState.OBSERVED,
                                LoopState.REMEDIATING, now=1_000_401.0) is True


def test_renew_claim_reports_lost_ownership_instead_of_taking_the_row_back(tmp_path):
    """False is the signal to stop. Renewing unconditionally would let a tick
    that already lost the row to a successor keep driving it, which is the
    double-drive the leases exist to prevent."""
    db = tmp_path / "r.db"
    slow_tick = Store(db)
    next_tick = Store(db)
    loop = open_one(slow_tick)
    assert slow_tick.claim(loop.loop_id, now=1_000_010.0) is True
    # The lease lapses and the successor takes over.
    assert next_tick.claim(loop.loop_id, now=1_000_400.0) is True

    assert slow_tick.renew_claim(loop.loop_id, now=1_000_401.0) is False
    assert next_tick.renew_claim(loop.loop_id, now=1_000_402.0) is True
    assert slow_tick.transition(loop.loop_id, LoopState.OBSERVED,
                                LoopState.REMEDIATING, now=1_000_403.0) is False
    assert slow_tick._db.in_transaction is False


def test_renew_claim_will_not_resurrect_a_lease_that_already_lapsed(tmp_path):
    """A lapsed lease is not ours to extend even when nobody took it yet: we
    cannot tell from here whether a successor is mid-round. Re-claiming is the
    way back, and it refuses if one is."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)

    assert store.renew_claim(loop.loop_id, now=1_000_010.0) is False, "never claimed"
    assert store.claim(loop.loop_id, now=1_000_010.0) is True
    assert store.renew_claim(loop.loop_id, now=1_000_400.0) is False, "lapsed"
    assert store.claim(loop.loop_id, now=1_000_401.0) is True
    assert store.renew_claim(loop.loop_id, now=1_000_402.0) is True


# --- M3: a corrupt row does surface inside a write transaction -------------

def test_a_corrupt_row_surfaces_inside_open_loops_own_transaction(tmp_path):
    """CorruptLoopRow used to claim it was never raised while a write
    transaction is open. open_loop reads through active_for_pr *inside* its
    BEGIN IMMEDIATE, so it is. The guarantee that actually holds is the weaker
    one: the mutator rolls back before the raise propagates, so a corrupt row
    fails loudly without leaving the database lock held or wedging the store."""
    store = Store(tmp_path / "r.db")
    _insert_raw(store, loop_id="lp_corrupt", state="not_a_state",
                created_at=999_999.0, pr_number=42)
    at_raise: dict[str, bool] = {}
    parse = store._row

    def spy(row):
        try:
            return parse(row)
        except CorruptLoopRow:
            at_raise["in_transaction"] = store._db.in_transaction
            raise

    store._row = spy
    with pytest.raises(CorruptLoopRow):
        open_one(store, pr=42)
    del store._row

    assert at_raise["in_transaction"] is True, "raised with the write lock held"
    assert store._db.in_transaction is False, "but unwound before it escaped"
    assert open_one(store, pr=43) is not None, "and the store is not wedged"


# --- M4: a failed BEGIN must not wedge the connection forever --------------

@pytest.mark.parametrize("mutate", [
    pytest.param(lambda s, lid: s.record_note(lid, "note", {}, 1_000_050.0),
                 id="record_note"),
    pytest.param(lambda s, lid: s.transition(lid, LoopState.OBSERVED,
                                             LoopState.REMEDIATING, now=1_000_050.0),
                 id="transition"),
    pytest.param(lambda s, lid: s.claim_due(now=1_000_050.0, limit=10),
                 id="claim_due"),
    pytest.param(lambda s, lid: s.claim(lid, now=1_000_050.0), id="claim"),
    pytest.param(lambda s, lid: s.renew_claim(lid, now=1_000_050.0), id="renew_claim"),
    pytest.param(lambda s, lid: s.release(lid), id="release"),
    pytest.param(lambda s, lid: s.mark_verdict_seen("br_new", 1_000_050.0),
                 id="mark_verdict_seen"),
    pytest.param(lambda s, lid: s.open_loop(
        owner="Ylopo", repo="example", pr_number=99, head_sha="e" * 40,
        boris_request_id="br_99", root_thread_ts="1.0", now=1_000_050.0),
        id="open_loop"),
])
def test_a_stray_open_transaction_cannot_wedge_a_mutator_forever(tmp_path, mutate):
    """Every mutator opens BEGIN IMMEDIATE. Run it while this long-lived
    connection is already in a transaction and SQLite raises 'cannot start a
    transaction within a transaction' -- with the BEGIN outside the try there
    is no rollback, the connection stays in that transaction, and every
    subsequent mutator raises the same way forever. Nothing in the public API
    reaches this today, which is exactly why it must be closed now: it is one
    statement away, and the failure mode is permanent."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store._db.execute("BEGIN IMMEDIATE")

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        mutate(store, loop.loop_id)
    assert store._db.in_transaction is False, "the failed BEGIN must be rolled back"

    # Recovered: the next write on this connection goes through.
    store.record_note(loop.loop_id, "after", {}, 1_000_060.0)
    assert [e["kind"] for e in store.events(loop.loop_id)] == ["opened", "after"]


# --- M5: a refused CAS rolls back, it does not commit ----------------------

class _PendingWriteConnection:
    """Slips one extra write into the store's transaction immediately after
    the statement matching `after`, standing in for anything left pending on
    this long-lived connection. A commit flushes it; a rollback discards it --
    which is how a test tells the two apart, since both leave `in_transaction`
    False."""

    def __init__(self, real, after, sql, params):
        self._real = real
        self._after = after
        self._sql = sql
        self._params = params
        self._armed = True

    def execute(self, sql, params=()):
        result = self._real.execute(sql, params)
        if self._armed and sql.startswith(self._after):
            self._armed = False
            self._real.execute(self._sql, self._params)
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_refused_cas_rolls_back_rather_than_committing_an_empty_transaction(tmp_path):
    """A refused CAS writes nothing of its own, so committing looks harmless.
    It is not: commit is not scoped to the method that calls it, and flushes
    whatever else is pending in the same transaction on this connection -- the
    silent half-write the BEGIN IMMEDIATE discipline exists to prevent.
    Asserting `in_transaction is False` cannot catch that, because a commit
    leaves it False too."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store._db = _PendingWriteConnection(
        store._db, "SELECT state, lease_owner",
        "INSERT INTO loop_events (loop_id, kind, detail_json, at) VALUES (?,?,?,?)",
        (loop.loop_id, "pending", "{}", 1_000_050.0),
    )

    # Stale expectation: the loop is observed, not remediating.
    assert store.transition(loop.loop_id, LoopState.REMEDIATING,
                            LoopState.READY, now=1_000_050.0) is False
    store._db = store._db._real

    assert [e["kind"] for e in store.events(loop.loop_id)] == ["opened"], \
        "an empty commit would have flushed the pending write"
    assert store._db.in_transaction is False


# --- I1: defer(), and the ordering that makes it a rotation ----------------


def _next_attempt(store, loop_id):
    return store._db.execute(
        "SELECT next_attempt_at FROM loops WHERE loop_id=?",
        (loop_id,)).fetchone()["next_attempt_at"]


def test_defer_pushes_a_loop_out_of_the_next_batch(tmp_path):
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    assert store.claim_due(now=1_000_010.0, limit=10)

    assert store.defer(loop.loop_id, now=1_000_010.0,
                       not_before=1_000_025.0) is True
    store.release(loop.loop_id)
    assert store.claim_due(now=1_000_020.0, limit=10) == ()
    assert len(store.claim_due(now=1_000_030.0, limit=10)) == 1


def test_defer_only_ever_raises_the_floor(tmp_path):
    """Monotone on purpose. A route that chose a longer wait -- the head-move
    grace window -- must keep it; a backoff applied on top of that window must
    not cut it short, or a late push stops being given time to appear."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store.claim_due(now=1_000_010.0, limit=10)
    store.transition(loop.loop_id, LoopState.OBSERVED, LoopState.REMEDIATING,
                     now=1_000_010.0, next_attempt_at=1_000_500.0)

    assert store.defer(loop.loop_id, now=1_000_010.0,
                       not_before=1_000_025.0) is True
    assert _next_attempt(store, loop.loop_id) == 1_000_500.0


def test_defer_does_not_move_updated_at(tmp_path):
    """`updated_at` is when the loop last actually changed, and it is what the
    lane age bound measures from. A reschedule is not a change: bumping it once
    a tick would push that deadline out forever and silently restore the wedge
    the bound exists to end."""
    store = Store(tmp_path / "r.db")
    loop = open_one(store)
    store.claim_due(now=1_000_010.0, limit=10)

    assert store.defer(loop.loop_id, now=1_000_020.0,
                       not_before=1_000_035.0) is True
    assert store.get(loop.loop_id).updated_at == loop.updated_at


def test_defer_refuses_a_terminal_or_missing_loop(tmp_path):
    store = Store(tmp_path / "r.db")
    finished = open_one(store)
    store.transition(finished.loop_id, LoopState.OBSERVED, LoopState.READY,
                     now=1_000_100.0)

    assert store.defer(finished.loop_id, now=1_000_200.0,
                       not_before=1_000_215.0) is False
    assert store.defer("lp_nonexistent", now=1_000_200.0,
                       not_before=1_000_215.0) is False
    assert store._db.in_transaction is False, \
        "a refused defer writes nothing and rolls back"


def test_defer_refuses_a_loop_someone_else_holds(tmp_path):
    db = tmp_path / "r.db"
    owner_tick, other_tick = Store(db), Store(db)
    loop = open_one(owner_tick)
    assert len(owner_tick.claim_due(now=1_000_010.0, limit=10)) == 1

    assert other_tick.defer(loop.loop_id, now=1_000_020.0,
                            not_before=1_000_035.0) is False
    assert _next_attempt(owner_tick, loop.loop_id) == 0.0


def test_claim_due_serves_the_least_recently_visited_loop_first(tmp_path):
    """Ordering by age alone is what let twenty permanently-due loops hold the
    batch forever: they sorted first on every tick and there were always
    exactly `limit` of them. Leading with `next_attempt_at` makes the batch a
    rotation -- a loop just visited sorts behind one that has been waiting."""
    store = Store(tmp_path / "r.db")
    oldest = open_one(store, pr=1, head="a" * 40, now=1_000_000.0)
    younger = open_one(store, pr=2, head="b" * 40, now=1_000_001.0)

    assert [c.loop_id for c in store.claim_due(now=1_000_010.0, limit=1)] == \
        [oldest.loop_id], "with nothing visited yet, age still decides"
    store.defer(oldest.loop_id, now=1_000_010.0, not_before=1_000_011.0)
    store.release(oldest.loop_id)

    assert [c.loop_id for c in store.claim_due(now=1_000_020.0, limit=1)] == \
        [younger.loop_id], "the loop nobody has looked at must come first"
