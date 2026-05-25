"""Regression tests for #28712 — kanban dispatcher must not auto-promote
worker-initiated ``kanban_block`` (sticky blocks), but must keep
auto-recovering circuit-breaker blocks.

The bug: when a worker called ``kanban_block(reason="review-required:
...")`` to hand off to a human, the dispatcher's ``recompute_ready``
would promote the task back to ``ready`` on the next tick.  The fresh
worker found nothing to do (work already applied), exited cleanly, and
got recorded as a ``protocol_violation`` → ``gave_up`` → promote → loop
until manual intervention.

These tests pin down:

* Worker / operator-initiated blocks are sticky and survive
  ``recompute_ready``.
* Direct/manual DB triage blocks without durable sticky evidence can still
  auto-recover — the original intent of #40c1decb3 is preserved for
  non-terminal legacy states.
* Structured ``gave_up`` rows emitted after retry-budget exhaustion are
  sticky and require explicit operator action.
* An explicit ``kanban_unblock`` clears the sticky state.
* The full block → promote → crash → ``gave_up`` loop is broken after
  this fix: subsequent ticks leave the task blocked.

The tangentially related schema-init ordering bug originally reported
in #28712 (``init_db`` crashing on legacy DBs that pre-dated the
``session_id`` migration) is covered separately by
``test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes``,
landed via #28754 / #28781 ahead of this fix.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Worker-initiated kanban_block must be sticky
# ---------------------------------------------------------------------------


def test_worker_block_is_not_auto_promoted_by_recompute_ready(kanban_home: Path) -> None:
    """A standalone task that a worker explicitly blocks for review
    must stay blocked across an arbitrary number of dispatcher ticks.
    Before #28712's fix, ``recompute_ready`` would silently flip it
    back to ``ready`` on the very next tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs human review")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify ACL change",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Hammer the promotion code — exactly the dispatcher loop's
        # behaviour, just compressed in time.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "worker-blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"


def test_worker_block_on_child_with_done_parents_is_still_sticky(kanban_home: Path) -> None:
    """The parent-completion path is the one ``recompute_ready`` was
    designed for, so it's the most dangerous false-positive: even when
    every parent is done, a worker-initiated block on the child must
    stay blocked."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="parent ok")

        kb.claim_task(conn, child)
        kb.block_task(
            conn, child,
            reason="review-required: child needs sign-off",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        assert kb.get_task(conn, child).status == "blocked"

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


def test_initial_status_blocked_is_not_auto_promoted_by_recompute_ready(kanban_home: Path) -> None:
    """Tasks created with ``initial_status='blocked'`` are explicit
    operator gates and must stay blocked until unblocked."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ops gate", initial_status="blocked")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"

        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "blocked"


def test_initial_status_blocked_with_done_parent_is_still_sticky(kanban_home: Path) -> None:
    """Even when parent gates are already open, an initial blocked gate
    remains sticky until explicit unblock."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        kb.complete_task(conn, parent, result="ok")

        child = kb.create_task(
            conn,
            title="ops-gated child",
            parents=[parent],
            initial_status="blocked",
        )
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "blocked"

        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, child)
        assert task is not None
        assert task.status == "blocked"


def test_initial_status_blocked_emits_blocked_event(kanban_home: Path) -> None:
    """Create-time blocked tasks should emit a blocked event so the
    sticky-block guard has durable provenance."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gate", initial_status="blocked")
        events = kb.list_events(conn, tid)

    blocked_events = [e for e in events if e.kind == "blocked"]
    assert blocked_events
    assert blocked_events[-1].payload == {"reason": "initial_status=blocked"}


def test_unblock_clears_initial_status_blocked_gate(kanban_home: Path) -> None:
    """An explicit unblock should clear create-time gates so later
    non-sticky blocked states can auto-recover."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gate", initial_status="blocked")
        assert kb.unblock_task(conn, tid) is True

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"

        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.commit()

        assert kb.recompute_ready(conn) == 1
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"


# ---------------------------------------------------------------------------
# Circuit-breaker blocks still auto-recover (preserve #40c1decb3 intent)
# ---------------------------------------------------------------------------


def test_circuit_breaker_block_still_auto_promotes(kanban_home: Path) -> None:
    """A child that was put into ``blocked`` *without* a worker-issued
    ``kanban_block`` (e.g. circuit-breaker after repeated spawn
    failures, manual DB triage) must still get auto-promoted when its
    parents complete — preserves the pre-#28712 recovery semantics."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        # Simulate a circuit-breaker / direct triage that flips status
        # without emitting a ``blocked`` event — exactly what
        # ``_record_task_failure`` does after a ``gave_up``.
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=5, "
            "last_failure_error='persistent error' WHERE id=?",
            (child,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        task = kb.get_task(conn, child)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_payloadless_gave_up_event_alone_does_not_make_block_sticky(kanban_home: Path) -> None:
    """Legacy/payloadless ``gave_up`` rows do not prove the retry budget
    was exhausted by ``_record_task_failure``. Keep them non-sticky unless
    an earlier explicit block remains uncleared."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")

        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (child,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (child, int(time.time())),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, child).status == "ready"


def test_exhausted_failure_gave_up_event_is_sticky(kanban_home: Path) -> None:
    """A structured ``gave_up`` emitted when retry budget is exhausted
    must not be auto-promoted by the next dispatcher tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="repro", assignee="worker", max_retries=1)
        kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (tid,),
            )
            run_id = kb._end_run(
                conn,
                tid,
                outcome="crashed",
                status="crashed",
                error="pid 12345 not alive",
                metadata={"pid": 12345},
            )
            kb._append_event(conn, tid, "crashed", {"pid": 12345}, run_id=run_id)

        blocked = kb._record_task_failure(
            conn,
            tid,
            error="pid 12345 not alive",
            outcome="crashed",
            release_claim=False,
            end_run=False,
            event_payload_extra={"pid": 12345},
        )
        assert blocked is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1

        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1


# ---------------------------------------------------------------------------
# unblock_task clears the sticky state
# ---------------------------------------------------------------------------


def test_unblock_clears_sticky_state_and_lets_block_recover(kanban_home: Path) -> None:
    """``hermes kanban unblock`` (or the ``kanban_unblock`` tool) is
    the only legitimate way out of a worker-initiated block.  After
    unblock, a *subsequent* circuit-breaker block on the same task
    must again be eligible for auto-recovery."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: ...",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.unblock_task(conn, tid)
        # After unblock the task is no longer blocked at all.
        assert kb.get_task(conn, tid).status == "ready"

        # Now simulate a *later* circuit-breaker block (no new
        # ``blocked`` event, just status flip).  The most recent
        # block/unblock event is ``unblocked`` → guard does not fire
        # → recompute can recover.
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?", (tid,),
        )
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Full bug-shaped loop: block → promote → crash → gave_up → next tick
# ---------------------------------------------------------------------------


def test_protocol_violation_loop_is_broken(kanban_home: Path) -> None:
    """Reproduces the exact #28712 loop and asserts the dispatcher
    leaves the task blocked instead of cycling.

    Loop shape from the issue:

    1. Worker calls ``kanban_block`` → status='blocked',
       ``task_runs.outcome='blocked'``, ``blocked`` event.
    2. (Bug) Dispatcher promotes back to ``ready``.
    3. Fresh worker exits cleanly without terminal tool call →
       ``protocol_violation`` event.
    4. ``_record_task_failure(failure_limit=1)`` → ``gave_up`` event,
       status='blocked' again.
    5. (Bug) Dispatcher promotes again → infinite loop.

    With the fix in place, step 2 never happens — the test simulates
    one would-be loop cycle by faking the crash-then-gave_up entries
    that *would* have been written and asserts the *next* tick still
    leaves the task blocked.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop reproducer")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: human eyes please",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # First dispatcher tick — must NOT promote.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"

        # Simulate the (hypothetical) protocol_violation + gave_up
        # entries that the dispatcher would have written if the bug
        # were still present.  Even with those event rows in place,
        # the worker-initiated ``blocked`` event is the most recent
        # of the ``{blocked, unblocked}`` pair, so the sticky guard
        # still fires.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'protocol_violation', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()

        # Subsequent ticks must still leave it blocked.
        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# Schema-init recovery on legacy DBs is covered by
# tests/hermes_cli/test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes
# (landed via #28754 / #28781).  The original PR shipped a duplicate test
# here; dropped during salvage to avoid two assertions of the same contract.
# ---------------------------------------------------------------------------
