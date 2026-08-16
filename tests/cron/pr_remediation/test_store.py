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
