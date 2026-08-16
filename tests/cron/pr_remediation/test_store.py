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
