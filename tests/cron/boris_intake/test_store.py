import sys
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Limits
from boris_intake import models
from boris_intake.models import NormalizedEvent, ReviewCommand, ReviewPolicy
from boris_intake.store import Store

L = Limits(per_user_hourly=2, per_user_daily=3, global_concurrent_lanes=1,
           reject_reply_cooldown_seconds=100)


def test_event_dedup(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.seen_event("C1", "1.0") is False
    s.record_event("C1", "1.0")
    assert s.seen_event("C1", "1.0") is True
    assert s.seen_event("C1", "2.0") is False


def test_lane_dedup_returns_existing_id(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.lane_for("k1") is None
    s.record_lane("k1", "t_abc")
    assert s.lane_for("k1") == "t_abc"


def test_hourly_limit(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    for _ in range(2):
        assert s.rate_verdict("U1", L, now) == "ok"
        s.record_request("U1", now)
    assert "hour" in s.rate_verdict("U1", L, now)


def test_hourly_window_rolls_off(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    for _ in range(2):
        s.record_request("U1", now)
    assert s.rate_verdict("U1", L, now + 3601) == "ok"


def test_daily_limit(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    s.record_request("U1", now)
    s.record_request("U1", now + 3601)
    s.record_request("U1", now + 7202)
    assert "day" in s.rate_verdict("U1", L, now + 7203)


def test_limits_are_per_user(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    for _ in range(2):
        s.record_request("U1", now)
    assert s.rate_verdict("U2", L, now) == "ok"


def test_global_concurrency(tmp_path):
    s = Store(tmp_path / "s.db")
    s.set_active_lane_count(1)
    assert "concurrent" in s.rate_verdict("U1", L, 1_000_000.0)


def test_rejection_reply_cooldown(tmp_path):
    s = Store(tmp_path / "s.db")
    now = 1_000_000.0
    assert s.should_reply_rejection("U1", now, 100) is True
    assert s.should_reply_rejection("U1", now + 1, 100) is False
    assert s.should_reply_rejection("U1", now + 101, 100) is True


def event(ts="1.0"):
    return NormalizedEvent(
        channel_id="C1",
        channel_type="channel",
        user_id="U1",
        ts=ts,
        thread_ts=None,
        text="untrusted Slack prose",
        is_bot=False,
        subtype=None,
    )


def command(repo="a", head="a" * 40, *, ts="1.0", ignore_ci=False, rubric="2"):
    return ReviewCommand(
        owner="Ylopo",
        repo=repo,
        pr_number=1,
        head_sha=head,
        pr_author="octocat",
        requester_user_id="U1",
        requester_email="a@ylopo.com",
        channel_id="C1",
        message_ts=ts,
        root_thread_ts=ts,
        permalink=f"https://ylopo.slack.com/archives/C1/p{ts.replace('.', '')}",
        policy=ReviewPolicy(ignore_ci=ignore_ci, rubric_version=rubric),
    )


def snapshot(fingerprint="sha256:gate-a"):
    return models.GateSnapshot(
        fingerprint=fingerprint,
        captured_at="2026-08-14T12:00:00Z",
        checks=(),
    )


def test_admit_event_commits_the_event_and_all_prs_atomically(tmp_path):
    s = Store(tmp_path / "s.db")

    result = s.admit_event(
        event(),
        (command("a"), command("b")),
        (snapshot(), snapshot("sha256:gate-b")),
        now=1_000_000.0,
    )

    assert result.redelivered is False
    assert [request.repo for request in result.admitted] == ["a", "b"]
    assert result.duplicates == ()
    assert s.seen_event("C1", "1.0") is True
    assert [request.repo for request in s.requests_for_event("C1", "1.0")] == ["a", "b"]
    assert "untrusted Slack prose" not in repr(result)


def test_admit_event_rolls_back_partial_rows_when_a_later_insert_fails(tmp_path):
    s = Store(tmp_path / "s.db")
    s._db.execute(
        """
        CREATE TRIGGER abort_second_review BEFORE INSERT ON review_requests
        WHEN NEW.repo = 'b'
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """
    )
    s._db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        s.admit_event(
            event(),
            (command("a"), command("b")),
            (snapshot(), snapshot("sha256:gate-b")),
            now=1_000_000.0,
        )

    assert s.seen_event("C1", "1.0") is False
    assert s.requests_for_event("C1", "1.0") == ()


def test_redelivery_returns_the_original_admissions_without_new_rows(tmp_path):
    s = Store(tmp_path / "s.db")
    first = s.admit_event(event(), (command(),), (snapshot(),), now=1_000_000.0)

    second = s.admit_event(event(), (command(),), (snapshot(),), now=1_000_001.0)

    assert second.redelivered is True
    assert [r.request_id for r in second.admitted] == [first.admitted[0].request_id]
    assert len(s.requests_for_event("C1", "1.0")) == 1


def test_each_pr_gets_distinct_request_and_shared_root_thread_identity(tmp_path):
    s = Store(tmp_path / "s.db")
    root = "0.5"
    source = replace(event(), thread_ts=root)
    commands = (
        replace(command("a"), root_thread_ts=root),
        replace(command("b"), root_thread_ts=root),
    )

    admitted = s.admit_event(
        source, commands, (snapshot(), snapshot("sha256:gate-b")), 1_000_000.0
    ).admitted

    assert len({r.request_id for r in admitted}) == 2
    assert {r.root_thread_ts for r in admitted} == {root}


def test_equivalent_in_flight_request_is_a_duplicate(tmp_path):
    s = Store(tmp_path / "s.db")
    original = s.admit_event(
        event("1.0"), (command(ts="1.0"),), (snapshot(),), 1_000_000.0
    ).admitted[0]
    repeated_command = replace(
        command(ts="2.0"), root_thread_ts="2.0", message_ts="2.0"
    )

    result = s.admit_event(
        event("2.0"), (repeated_command,), (snapshot(),), 1_000_001.0
    )

    assert result.admitted == ()
    assert [r.request_id for r in result.duplicates] == [original.request_id]


def test_equivalent_completed_request_is_still_a_duplicate(tmp_path):
    s = Store(tmp_path / "s.db")
    original = s.admit_event(
        event("1.0"), (command(ts="1.0"),), (snapshot(),), 1_000_000.0
    ).admitted[0]
    transitions = (
        models.WorkflowState.STATUS_MESSAGE_PENDING,
        models.WorkflowState.STARTING_REVIEW,
        models.WorkflowState.REVIEWING,
        models.WorkflowState.DELIVERY_PENDING,
        models.WorkflowState.COMPLETE,
    )
    expected = models.WorkflowState.ADMITTED
    for target in transitions:
        assert s.transition(original.request_id, expected, target)
        expected = target

    result = s.admit_event(
        event("2.0"),
        (replace(command(ts="2.0"), root_thread_ts="2.0"),),
        (snapshot(),),
        1_000_001.0,
    )

    assert result.admitted == ()
    assert result.duplicates[0].workflow_state is models.WorkflowState.COMPLETE


@pytest.mark.parametrize(
    ("command_changes", "gate_fingerprint"),
    (
        ({"head": "b" * 40}, "sha256:gate-a"),
        ({"ignore_ci": True}, "sha256:gate-a"),
        ({"rubric": "3"}, "sha256:gate-a"),
        ({}, "sha256:gate-new"),
    ),
)
def test_changed_review_or_gate_identity_permits_a_new_attempt(
    tmp_path, command_changes, gate_fingerprint
):
    s = Store(tmp_path / "s.db")
    s.admit_event(event("1.0"), (command(ts="1.0"),), (snapshot(),), 1_000_000.0)
    changed_command = command(ts="2.0", **command_changes)
    changed_command = replace(
        changed_command,
        message_ts="2.0",
        root_thread_ts="2.0",
        permalink="https://ylopo.slack.com/archives/C1/p20",
    )

    result = s.admit_event(
        event("2.0"),
        (changed_command,),
        (snapshot(gate_fingerprint),),
        1_000_001.0,
    )

    assert len(result.admitted) == 1
    assert result.duplicates == ()


def test_transition_is_compare_and_swap_and_records_the_change(tmp_path):
    s = Store(tmp_path / "s.db")
    request = s.admit_event(
        event(), (command(),), (snapshot(),), 1_000_000.0
    ).admitted[0]

    assert s.transition(
        request.request_id,
        models.WorkflowState.ADMITTED,
        models.WorkflowState.STATUS_MESSAGE_PENDING,
        status_message_ts="2.0",
    ) is True
    assert s.transition(
        request.request_id,
        models.WorkflowState.ADMITTED,
        models.WorkflowState.STARTING_REVIEW,
    ) is False
    stored = s.get_request(request.request_id)
    assert stored.workflow_state is models.WorkflowState.STATUS_MESSAGE_PENDING
    assert stored.status_message_ts == "2.0"
    assert s.request_events(request.request_id)[-1]["to_state"] == (
        models.WorkflowState.STATUS_MESSAGE_PENDING.value
    )


def test_transition_rejects_skipping_required_lifecycle_states(tmp_path):
    s = Store(tmp_path / "s.db")
    request = s.admit_event(
        event(), (command(),), (snapshot(),), 1_000_000.0
    ).admitted[0]

    with pytest.raises(ValueError, match="admitted -> reviewing"):
        s.transition(
            request.request_id,
            models.WorkflowState.ADMITTED,
            models.WorkflowState.REVIEWING,
        )

    assert s.get_request(request.request_id).workflow_state is models.WorkflowState.ADMITTED


def test_claim_due_excludes_terminal_and_future_requests(tmp_path):
    s = Store(tmp_path / "s.db")
    due = s.admit_event(
        event("1.0"), (command("a", ts="1.0"),), (snapshot(),), 1_000_000.0
    ).admitted[0]
    future = s.admit_event(
        event("2.0"),
        (command("b", ts="2.0"),),
        (snapshot("sha256:gate-b"),),
        1_000_000.0,
    ).admitted[0]
    terminal = s.admit_event(
        event("3.0"),
        (command("c", ts="3.0"),),
        (snapshot("sha256:gate-c"),),
        1_000_000.0,
    ).admitted[0]
    s.record_error(future.request_id, "dispatch", "later", 1_000_100.0)
    s._db.execute(
        "UPDATE review_requests SET workflow_state='complete' WHERE request_id=?",
        (terminal.request_id,),
    )
    s._db.commit()

    assert [r.request_id for r in s.claim_due(1_000_001.0)] == [due.request_id]


def test_claim_due_excludes_live_claims_and_recovers_expired_claims(tmp_path):
    db_path = tmp_path / "s.db"
    first = Store(db_path)
    request = first.admit_event(
        event(), (command(),), (snapshot(),), 1_000_000.0
    ).admitted[0]
    second = Store(db_path)

    assert [
        item.request_id
        for item in first.claim_due(1_000_001.0, lease_seconds=30)
    ] == [request.request_id]
    assert second.claim_due(1_000_030.999, lease_seconds=30) == ()
    assert [
        item.request_id
        for item in second.claim_due(1_000_031.0, lease_seconds=30)
    ] == [request.request_id]
    assert first.transition(
        request.request_id,
        models.WorkflowState.ADMITTED,
        models.WorkflowState.STATUS_MESSAGE_PENDING,
        now=1_000_031.0,
    ) is False
    assert second.transition(
        request.request_id,
        models.WorkflowState.ADMITTED,
        models.WorkflowState.STATUS_MESSAGE_PENDING,
        now=1_000_031.0,
    ) is True


def test_source_reaction_lease_excludes_live_owner_and_recovers_after_expiry(
    tmp_path,
):
    db_path = tmp_path / "s.db"
    first = Store(db_path)
    second = Store(db_path)

    assert first.acquire_source_reaction_lease(
        "C1", "1.0", 1_000_000.0, lease_seconds=30
    ) is True
    assert second.acquire_source_reaction_lease(
        "C1", "1.0", 1_000_029.999, lease_seconds=30
    ) is False
    assert second.acquire_source_reaction_lease(
        "C1", "1.0", 1_000_030.0, lease_seconds=30
    ) is True
    assert first.release_source_reaction_lease("C1", "1.0") is False
    assert second.release_source_reaction_lease("C1", "1.0") is True


def test_lifecycle_mutations_use_the_caller_clock(tmp_path):
    s = Store(tmp_path / "s.db")
    request = s.admit_event(
        event(), (command(),), (snapshot(),), 1_000_000.0
    ).admitted[0]

    assert s.transition(
        request.request_id,
        models.WorkflowState.ADMITTED,
        models.WorkflowState.STATUS_MESSAGE_PENDING,
        now=1_000_010.0,
    ) is True
    assert s.checkpoint(
        request.request_id,
        models.WorkflowState.STATUS_MESSAGE_PENDING,
        next_attempt_at=1_000_020.0,
        now=1_000_011.0,
    ) is True
    assert s.record_error(
        request.request_id,
        "status",
        "temporary failure",
        1_000_025.0,
        now=1_000_012.0,
    ) is True

    stored = s.get_request(request.request_id)
    assert stored.updated_at == 1_000_012.0
    assert [event["at"] for event in s.request_events(request.request_id)[-3:]] == [
        1_000_010.0,
        1_000_011.0,
        1_000_012.0,
    ]


def test_store_migrates_existing_lifecycle_rows_for_leasing(tmp_path):
    db_path = tmp_path / "s.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE review_requests ("
        "request_id TEXT PRIMARY KEY, workflow_state TEXT NOT NULL, "
        "next_attempt_at REAL NOT NULL, created_at REAL NOT NULL, lane_id TEXT)"
    )
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()

    store = Store(db_path)
    columns = {
        str(row["name"])
        for row in store._db.execute("PRAGMA table_info(review_requests)")
    }
    source_lease_columns = {
        str(row["name"])
        for row in store._db.execute("PRAGMA table_info(source_reaction_leases)")
    }

    assert {"lease_owner", "lease_expires_at"} <= columns
    assert "terminal_status_confirmed" in columns
    assert source_lease_columns == {
        "channel",
        "message_ts",
        "lease_owner",
        "lease_expires_at",
    }
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 5
