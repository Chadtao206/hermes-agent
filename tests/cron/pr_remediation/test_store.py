import sqlite3
import threading

import pytest

from pr_remediation.models import LoopState
from pr_remediation.store import Store


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
            (_loop_id, owner, repo, pr_number, generation, head_sha,
             boris_request_id, root_thread_ts, state, created_at, updated_at) = params
            self._real.execute(
                sql,
                ("lp_racer", owner, repo, pr_number, generation, "c" * 40,
                 "br_racer", "1.0", state, created_at, updated_at),
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
    from pr_remediation.store import CorruptLoopRow

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
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
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
