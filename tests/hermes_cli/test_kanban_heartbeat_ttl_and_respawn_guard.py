"""Claim-TTL renewal, spawn-ownership evidence, and respawn-guard behavior.

Regression coverage for a live incident where a card that was heartbeating
every 2 minutes was reclaimed mid-flight the moment its original 15-minute
claim window lapsed, and then refused respawn forever by the ``active_pr``
guard while that guard wrote one event per 10-second dispatcher tick.

Each test states an invariant about how two facts must relate (heartbeat vs
claim expiry, guard reason vs deliberate re-queue, event count vs tick
count) rather than pinning a current constant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()


def _claim_expires(conn, task_id):
    row = conn.execute(
        "SELECT claim_expires FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return row["claim_expires"]


def _run_claim_expires(conn, run_id):
    row = conn.execute(
        "SELECT claim_expires, last_heartbeat_at FROM task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return row["claim_expires"], row["last_heartbeat_at"]


def _worker_pid(conn, task_id):
    row = conn.execute(
        "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return row["worker_pid"]


# ---------------------------------------------------------------------------
# 1. Heartbeats renew the claim TTL
# ---------------------------------------------------------------------------

def test_heartbeat_renews_claim_ttl_past_original_window(kanban_home, monkeypatch):
    """The incident, distilled: a worker heartbeating every 2 minutes must
    still hold its claim well past the 15-minute window it was claimed
    under, and must never be reclaimable by ``release_stale_claims``."""
    now = 7_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="long-runner", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        original_expires = _claim_expires(conn, tid)
        assert original_expires == now + 900

        # Heartbeat every 2 minutes for an hour — four times the original TTL.
        clock = now
        for _ in range(30):
            clock += 120
            monkeypatch.setattr(kb.time, "time", lambda c=clock: c)
            assert kb.heartbeat_worker(
                conn, tid, expected_run_id=task.current_run_id,
            ) is True
            # The invariant: the claim always expires in the future.
            assert _claim_expires(conn, tid) > clock

        # Well past the original window, and the run row agrees with the task.
        assert _claim_expires(conn, tid) > original_expires
        run_expires, run_hb = _run_claim_expires(conn, task.current_run_id)
        assert run_expires == _claim_expires(conn, tid)
        assert run_hb == clock

        # And the reclaim sweeper finds nothing to reclaim.
        assert kb.release_stale_claims(conn) == 0
        assert kb.get_task(conn, tid).status == "running"


def test_heartbeat_never_shortens_a_longer_claim(kanban_home, monkeypatch):
    """A deliberately-long claim outlives a short heartbeat window: the
    renewal may only ever move ``claim_expires`` forward."""
    now = 7_100_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="long-claim", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=7200)
        long_expires = _claim_expires(conn, tid)

        later = now + 60
        monkeypatch.setattr(kb.time, "time", lambda: later)
        assert kb.heartbeat_worker(
            conn, tid, expected_run_id=task.current_run_id, ttl_seconds=900,
        ) is True
        assert _claim_expires(conn, tid) == long_expires


def test_heartbeat_does_not_invent_expiry_for_unexpiring_claim(
    kanban_home, monkeypatch,
):
    """A claim with no expiry must stay unexpiring — stamping one would make
    an intentionally-unexpiring run reclaimable by TTL."""
    now = 7_150_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="no-ttl", assignee="alice")
        task = kb.claim_task(conn, tid)
        conn.execute(
            "UPDATE tasks SET claim_expires = NULL WHERE id = ?", (tid,),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = NULL WHERE id = ?",
            (task.current_run_id,),
        )
        conn.commit()

        assert kb.heartbeat_worker(
            conn, tid, expected_run_id=task.current_run_id,
        ) is True
        assert _claim_expires(conn, tid) is None
        run_expires, _ = _run_claim_expires(conn, task.current_run_id)
        assert run_expires is None


# ---------------------------------------------------------------------------
# 2. Expected-run fencing
# ---------------------------------------------------------------------------

def test_heartbeat_from_superseded_run_is_rejected(kanban_home, monkeypatch):
    """A stale worker must not be able to renew the claim now held by its
    replacement — the fence rejects it and nothing moves."""
    now = 7_200_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="superseded", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        old_run = first.current_run_id

        # Reclaim + re-claim: a new run now owns the card.
        kb.reclaim_task(conn, tid, reason="test")
        second = kb.claim_task(conn, tid, ttl_seconds=900)
        assert second.current_run_id != old_run
        expires_before = _claim_expires(conn, tid)

        later = now + 300
        monkeypatch.setattr(kb.time, "time", lambda: later)
        assert kb.heartbeat_worker(conn, tid, expected_run_id=old_run) is False
        assert _claim_expires(conn, tid) == expires_before

        # The current run's heartbeat still works.
        assert kb.heartbeat_worker(
            conn, tid, expected_run_id=second.current_run_id,
        ) is True
        assert _claim_expires(conn, tid) > expires_before


def test_heartbeat_on_non_running_task_is_rejected(kanban_home, monkeypatch):
    """A completed card must not be kept alive by a late heartbeat."""
    now = 7_250_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="finished", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        run_id = task.current_run_id
        kb.complete_task(conn, tid, summary="done")

        assert kb.heartbeat_worker(conn, tid, expected_run_id=run_id) is False
        assert kb.heartbeat_worker(conn, tid) is False
        assert _claim_expires(conn, tid) is None


# ---------------------------------------------------------------------------
# 3. A live local supervisor is protected at the TTL boundary
# ---------------------------------------------------------------------------

def test_live_local_supervisor_without_pid_is_extended_not_reclaimed(
    kanban_home, monkeypatch,
):
    """The exact reclaim payload from the incident: host-local claim lock,
    ``worker_pid`` NULL, last heartbeat seconds old. That card must be
    extended, not reclaimed — a heartbeat is first-party evidence of life
    and the missing pid says nothing about liveness."""
    now = 7_300_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lane-worker", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)
        # Expired claim, no pid recorded, heartbeat 42s ago (the live values).
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ?, "
            "worker_pid = NULL WHERE id = ?",
            (now - 7, now - 42, tid),
        )
        conn.commit()

        assert kb.release_stale_claims(conn) == 0
        assert kb.get_task(conn, tid).status == "running"
        assert _claim_expires(conn, tid) > now
        extended = _events(conn, tid, "claim_extended")
        assert len(extended) == 1
        payload = json.loads(extended[-1]["payload"])
        assert payload["reason"] == "heartbeat_fresh"
        assert payload["worker_pid"] is None
        assert payload["heartbeat_age_seconds"] == 42


def test_foreign_host_claim_without_pid_is_still_reclaimed(
    kanban_home, monkeypatch,
):
    """Heartbeat freshness protects OUR workers only — a claim from another
    host is not ours to extend, so the reclaim path is unchanged."""
    now = 7_350_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="remote", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)
        conn.execute(
            "UPDATE tasks SET claim_lock = ?, claim_expires = ?, "
            "last_heartbeat_at = ?, worker_pid = NULL WHERE id = ?",
            ("some-other-host:999", now - 7, now - 42, tid),
        )
        conn.commit()

        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).status == "ready"


def test_long_silent_no_pid_worker_is_eventually_reclaimed(
    kanban_home, monkeypatch,
):
    """Heartbeat-only evidence buys a short grace, not immortality: a worker
    that stopped heartbeating a full claim window ago is reclaimed."""
    now = 7_400_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="silent", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ?, "
            "worker_pid = NULL WHERE id = ?",
            (now - 10, now - (kb.DEFAULT_CLAIM_TTL_SECONDS + 60), tid),
        )
        conn.commit()

        assert kb.release_stale_claims(conn) == 1
        assert kb.get_task(conn, tid).status == "ready"


def test_reclaim_payload_separates_claim_host_from_kill_attempt(
    kanban_home, monkeypatch,
):
    """``host_local`` in the reclaim payload must describe the CLAIM LOCK.
    The termination dict reports on the kill attempt (False whenever there
    was no pid to signal); merging it raw made a plainly-local claim read as
    foreign and sent the incident's first triage down the wrong path."""
    now = 7_450_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="payload", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ?, "
            "worker_pid = NULL WHERE id = ?",
            (now - 10, now - (kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS + 60), tid),
        )
        conn.commit()

        assert kb.release_stale_claims(conn) == 1
        payload = json.loads(_events(conn, tid, "reclaimed")[-1]["payload"])
        # The claim lock was ours...
        assert payload["host_local"] is True
        # ...while the kill attempt had no pid to signal, reported separately.
        assert payload["termination_host_local"] is False
        assert payload["termination_attempted"] is False


# ---------------------------------------------------------------------------
# 4. Spawn ownership: fencing, observability, and self-healing adoption
# ---------------------------------------------------------------------------

def test_late_pid_write_for_superseded_run_is_dropped_and_reported(
    kanban_home, monkeypatch,
):
    """The spawn→heartbeat race: a pid that arrives after the card has been
    reclaimed and re-claimed belongs to the OLD child. Stamping it on the
    new run would make the supervisor kill the pane it should keep and
    ``detect_crashed_workers`` reclaim a live run, so it is dropped — and
    the dropped evidence is recorded instead of vanishing silently."""
    now = 7_500_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="racy", assignee="alice")
        first = kb.claim_task(conn, tid, ttl_seconds=900)
        kb.reclaim_task(conn, tid, reason="test")
        second = kb.claim_task(conn, tid, ttl_seconds=900)

        assert kb._record_spawn_ownership(
            conn, tid, 4242, expected_run_id=first.current_run_id,
        ) is False
        assert _worker_pid(conn, tid) is None
        assert _events(conn, tid, "spawned") == []
        unrecorded = _events(conn, tid, "spawn_ownership_unrecorded")
        assert len(unrecorded) == 1
        assert json.loads(unrecorded[0]["payload"])["reason"] == "run_superseded"

        # The current run's own pid write still lands.
        assert kb._record_spawn_ownership(
            conn, tid, 4343, expected_run_id=second.current_run_id,
        ) is True
        assert _worker_pid(conn, tid) == 4343
        assert len(_events(conn, tid, "spawned")) == 1


def test_spawn_returning_no_pid_records_missing_ownership(kanban_home, monkeypatch):
    """An out-of-process launcher that returns no pid leaves the card with no
    ownership evidence. That used to be silent (23 of 44 supervisor-lane runs
    on the live board had no ``spawned`` event); now it is in the event log,
    and it does not fail the run — the child is alive."""
    now = 7_550_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="pidless", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

        assert kb._record_spawn_ownership(
            conn, tid, None, expected_run_id=task.current_run_id,
        ) is False
        assert kb.get_task(conn, tid).status == "running"
        unrecorded = _events(conn, tid, "spawn_ownership_unrecorded")
        assert len(unrecorded) == 1
        payload = json.loads(unrecorded[0]["payload"])
        assert payload["reason"] == "spawn_returned_no_pid"
        assert payload["pid"] is None


def test_heartbeat_adopts_ownership_when_card_has_no_pid(kanban_home, monkeypatch):
    """The other side of the race: a heartbeat carrying the worker's own pid
    re-establishes ownership without a second spawn, and announces it exactly
    once no matter how often the worker heartbeats."""
    now = 7_600_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="adopt", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert _worker_pid(conn, tid) is None

        my_pid = 12321
        for i in range(4):
            monkeypatch.setattr(kb.time, "time", lambda c=now + 120 * i: c)
            assert kb.heartbeat_worker(
                conn, tid,
                expected_run_id=task.current_run_id, worker_pid=my_pid,
            ) is True

        assert _worker_pid(conn, tid) == my_pid
        run_row = conn.execute(
            "SELECT worker_pid FROM task_runs WHERE id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert run_row["worker_pid"] == my_pid
        # One adoption event, not one per heartbeat, and never a duplicate
        # ``spawned`` event (the dispatcher owns that kind).
        assert len(_events(conn, tid, "ownership_adopted")) == 1
        assert _events(conn, tid, "spawned") == []
        assert len(_events(conn, tid, "heartbeat")) == 4


def test_heartbeat_does_not_steal_ownership_from_a_live_worker(
    kanban_home, monkeypatch,
):
    """Two workers on one card is the failure we are avoiding, not causing:
    a heartbeat must never overwrite a recorded pid that is still alive."""
    now = 7_650_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == 555)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="conflict", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        assert kb._record_spawn_ownership(
            conn, tid, 555, expected_run_id=task.current_run_id,
        ) is True

        assert kb.heartbeat_worker(
            conn, tid, expected_run_id=task.current_run_id, worker_pid=999,
        ) is True
        assert _worker_pid(conn, tid) == 555
        assert _events(conn, tid, "ownership_adopted") == []


def test_heartbeat_adopts_over_a_dead_recorded_pid(kanban_home, monkeypatch):
    """A recorded pid that is gone is not an owner — the live heartbeater
    takes over, which is how a readopted supervisor regains ownership."""
    now = 7_700_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="dead-pid", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)
        kb._record_spawn_ownership(
            conn, tid, 777, expected_run_id=task.current_run_id,
        )

        assert kb.heartbeat_worker(
            conn, tid, expected_run_id=task.current_run_id, worker_pid=888,
        ) is True
        assert _worker_pid(conn, tid) == 888
        assert len(_events(conn, tid, "ownership_adopted")) == 1


# ---------------------------------------------------------------------------
# 5. active_pr respawn guard
# ---------------------------------------------------------------------------

def _park_ready_with_pr_comment(conn, *, title, pr_at, author="cron"):
    """Create a ready task whose comment history contains a PR URL."""
    tid = kb.create_task(conn, title=title, assignee="alice")
    kb.add_comment(
        conn, tid, author,
        "opened https://github.com/acme/widgets/pull/7 for review",
    )
    conn.execute(
        "UPDATE task_comments SET created_at = ? WHERE task_id = ?",
        (pr_at, tid),
    )
    conn.commit()
    return tid


def test_pr_comment_alone_still_guards_respawn(kanban_home, monkeypatch):
    """Duplicate-PR safety is the default: with no deliberate re-run, a
    recent PR comment still defers the spawn."""
    now = 7_750_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = _park_ready_with_pr_comment(conn, title="pr-only", pr_at=now - 600)
        assert kb.check_respawn_guard(conn, tid) == "active_pr"


def test_explicit_requeue_after_pr_comment_permits_respawn(
    kanban_home, monkeypatch,
):
    """An operator who deliberately re-queues an existing-PR remediation card
    must be obeyed. Without this the guard held for a full 24 hours and
    refused the card on every 10-second tick with no way to release it."""
    now = 7_800_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = _park_ready_with_pr_comment(conn, title="requeued", pr_at=now - 600)
        assert kb.check_respawn_guard(conn, tid) == "active_pr"

        # Deliberate re-queue AFTER the PR comment.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'status', ?, ?)",
            (tid, json.dumps({"status": "ready"}), now - 60),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("kind", ["status", "promoted", "unblocked", "reclaimed"])
def test_every_deliberate_requeue_kind_releases_the_pr_guard(
    kanban_home, monkeypatch, kind,
):
    """Reclaim, unblock, promote and an explicit ready all mean "run this
    again" — the guard reads them the same way it does for recent_success."""
    now = 7_850_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = _park_ready_with_pr_comment(
            conn, title=f"requeue-{kind}", pr_at=now - 600,
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, NULL, ?)",
            (tid, kind, now - 60),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, tid) is None


def test_pr_comment_newer_than_requeue_rearms_the_guard(kanban_home, monkeypatch):
    """A fresh PR is fresh duplicate-risk: a PR comment posted after the
    re-queue puts the guard back on."""
    now = 7_900_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = _park_ready_with_pr_comment(conn, title="rearm", pr_at=now - 900)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'status', ?, ?)",
            (tid, json.dumps({"status": "ready"}), now - 600),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, tid) is None

        # A newer PR comment lands after the re-queue.
        kb.add_comment(
            conn, tid, "cron",
            "opened https://github.com/acme/widgets/pull/8 (second PR)",
        )
        conn.execute(
            "UPDATE task_comments SET created_at = ? "
            "WHERE task_id = ? AND body LIKE '%pull/8%'",
            (now - 120, tid),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, tid) == "active_pr"


def test_requeue_before_the_pr_comment_does_not_release_the_guard(
    kanban_home, monkeypatch,
):
    """Ordering matters, not mere presence: a re-queue that predates the PR
    comment is not a decision about that PR."""
    now = 7_950_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = _park_ready_with_pr_comment(conn, title="stale-requeue", pr_at=now - 300)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'status', ?, ?)",
            (tid, json.dumps({"status": "ready"}), now - 900),
        )
        conn.commit()
        assert kb.check_respawn_guard(conn, tid) == "active_pr"


# ---------------------------------------------------------------------------
# 6. respawn_guarded event deduplication
# ---------------------------------------------------------------------------

def test_unchanged_guard_reason_does_not_flood_the_event_log(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A 10-second tick interval must not write 10-second events: one card
    held by the guard produced 836 byte-identical rows in the incident,
    burying the rest of its history."""
    now = 8_000_000
    tick_seconds = 10
    ticks = 90  # 15 minutes at the dispatcher's default gateway interval
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = _park_ready_with_pr_comment(conn, title="flood", pr_at=now - 600)

        for i in range(ticks):
            monkeypatch.setattr(kb.time, "time", lambda c=now + tick_seconds * i: c)
            res = kb.dispatch_once(conn, spawn_fn=lambda task, ws: 1)
            # Telemetry is unthrottled — every tick still reports the hold.
            assert (tid, "active_pr") in res.respawn_guarded

        guarded = _events(conn, tid, "respawn_guarded")
        # The contract is one event per throttle window, not one per tick.
        elapsed = tick_seconds * (ticks - 1)
        max_expected = 1 + elapsed // kb._RESPAWN_GUARD_EVENT_REPEAT_SECONDS
        assert len(guarded) <= max_expected, (
            f"{ticks} ticks wrote {len(guarded)} events; "
            f"at most {max_expected} allowed for {elapsed}s of holding"
        )
        assert len(guarded) < ticks // 10, "guard must not write per-tick rows"
        assert json.loads(guarded[0]["payload"])["reason"] == "active_pr"
        assert kb.get_task(conn, tid).status == "ready"


def test_guard_event_repeats_after_the_throttle_window(kanban_home, monkeypatch):
    """A long-lived hold stays observable: the event repeats periodically so
    ``hermes kanban tail`` shows the card is still being held."""
    now = 8_050_000
    with kb.connect() as conn:
        monkeypatch.setattr(kb.time, "time", lambda: now)
        tid = kb.create_task(conn, title="repeat", assignee="alice")
        assert kb._should_log_respawn_guard(conn, tid, "active_pr", now) is True
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'respawn_guarded', ?, ?)",
            (tid, json.dumps({"reason": "active_pr"}), now),
        )
        conn.commit()

        window = kb._RESPAWN_GUARD_EVENT_REPEAT_SECONDS
        assert kb._should_log_respawn_guard(
            conn, tid, "active_pr", now + window - 1,
        ) is False
        assert kb._should_log_respawn_guard(
            conn, tid, "active_pr", now + window,
        ) is True


def test_changed_guard_reason_is_logged_immediately(kanban_home, monkeypatch):
    """State changes are never throttled — a different reason is a different
    fact about the card and must show up on the tick it happens."""
    now = 8_100_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="changed", assignee="alice")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'respawn_guarded', ?, ?)",
            (tid, json.dumps({"reason": "recent_success"}), now),
        )
        conn.commit()
        assert kb._should_log_respawn_guard(conn, tid, "active_pr", now + 5) is True


def test_guard_throttle_survives_unreadable_payload(kanban_home, monkeypatch):
    """A malformed payload must not suppress diagnosis — unknown previous
    reason is treated as a change and logged."""
    now = 8_150_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bad-payload", assignee="alice")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'respawn_guarded', ?, ?)",
            (tid, "not-json", now),
        )
        conn.commit()
        assert kb._should_log_respawn_guard(conn, tid, "active_pr", now + 1) is True


# ---------------------------------------------------------------------------
# 7. End-to-end: the incident cannot recur
# ---------------------------------------------------------------------------

def test_dispatch_then_heartbeat_survives_the_original_ttl(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """Full path: dispatcher claims and spawns, the worker heartbeats on a
    2-minute cadence, and the card is still running (and still owned) long
    after the window it was claimed under."""
    now = 8_200_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "900")
    spawned_pid = 24680
    # The worker process is alive throughout, as it was in the incident.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: int(pid) == spawned_pid)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="e2e", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=lambda task, ws: spawned_pid)
        assert [t[0] for t in res.spawned] == [tid]
        run_id = kb.get_task(conn, tid).current_run_id

        clock = now
        for _ in range(20):  # 40 minutes of heartbeats
            clock += 120
            monkeypatch.setattr(kb.time, "time", lambda c=clock: c)
            assert kb.heartbeat_worker(
                conn, tid, expected_run_id=run_id, note="supervisor alive",
            ) is True
            kb.release_stale_claims(conn)
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == run_id
        assert _worker_pid(conn, tid) == spawned_pid
        assert _events(conn, tid, "reclaimed") == []


# ---------------------------------------------------------------------------
# 8. Ownership adoption is explicit-only
# ---------------------------------------------------------------------------

def test_in_process_heartbeat_paths_do_not_self_adopt(kanban_home, monkeypatch):
    """A nested ``hermes`` run inherits ``HERMES_KANBAN_TASK``, so the
    in-process heartbeat paths must never register their own pid: a
    short-lived child that adopted ownership and exited would be read as a
    crashed worker and lose a live card. Adoption is explicit-only."""
    import tools.kanban_tools as kt

    now = 8_250_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="nested", assignee="alice")
        task = kb.claim_task(conn, tid, ttl_seconds=900)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(kt, "_auto_heartbeat_last_attempt", 0.0)
    assert kt.heartbeat_current_worker_from_env() is True

    with kb.connect() as conn:
        # Liveness was recorded; ownership was not invented.
        assert _worker_pid(conn, tid) is None
        assert len(_events(conn, tid, "heartbeat")) == 1
        assert _events(conn, tid, "ownership_adopted") == []


def test_cli_heartbeat_accepts_and_forwards_worker_pid(kanban_home, monkeypatch):
    """``hermes kanban heartbeat --worker-pid`` is how an out-of-process
    supervisor (which knows it is long-lived) registers ownership."""
    import argparse

    from hermes_cli import kanban as kc

    now = 8_300_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="cli-adopt", assignee="alice")
        kb.claim_task(conn, tid, ttl_seconds=900)

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args([
        "kanban", "heartbeat", tid,
        "--note", "supervisor alive", "--worker-pid", "31313",
    ])
    assert kc.kanban_command(args) == 0

    with kb.connect() as conn:
        assert _worker_pid(conn, tid) == 31313
        assert len(_events(conn, tid, "ownership_adopted")) == 1
        assert _claim_expires(conn, tid) > now
