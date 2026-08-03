"""Behavioral tests for blocked-recovery primitives in the Kanban core."""

from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home: Path):
    with kb.connect_closing() as connection:
        yield connection


def _running_task(conn, title: str = "review") -> str:
    task_id = kb.create_task(conn, title=title, assignee="reviewer")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    assert kb.claim_task(conn, task_id, claimer="reviewer") is not None
    return task_id


def _review_metadata(
    *,
    verdict: str = "APPROVED",
    findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "review": {
            "schema_version": 1,
            "verdict": verdict,
            "target": {
                "kind": "git",
                "identity": "example/repository",
                "revision": "a" * 40,
            },
            "findings": [] if findings is None else findings,
        }
    }


def _completed_event(conn, task_id: str) -> kb.Event:
    return [event for event in kb.list_events(conn, task_id) if event.kind == "completed"][-1]


def _latest_event(conn, task_id: str) -> kb.Event:
    return kb.list_events(conn, task_id)[-1]


def _latest_event_signature(conn, task_id: str) -> str:
    return kb.canonical_event_signature(_latest_event(conn, task_id))


def _operation_events(conn, task_id: str, operation_id: str) -> list[kb.Event]:
    return [
        event
        for event in kb.list_events(conn, task_id)
        if event.payload and event.payload.get("operation_id") == operation_id
    ]


def _blocked_task(conn, *, kind: str | None = None) -> str:
    task_id = kb.create_task(conn, title="blocked recovery target")
    assert kb.block_task(conn, task_id, reason="recovery fixture", kind=kind)
    return task_id


def _failed_review_generation(conn) -> tuple[str, kb.Event]:
    created = kb.create_review_generation_task(conn, _review_generation_request())
    assert kb.claim_task(conn, created.task_id, claimer="reviewer") is not None
    assert kb._record_spawn_failure(
        conn,
        created.task_id,
        "review worker exited",
        failure_limit=1,
    )
    event = _latest_event(conn, created.task_id)
    assert event.kind == "gave_up"
    return created.task_id, event


def _review_generation_request(
    *,
    key: str = "review-lane:default::t_parent:g1",
    tenant: str | None = None,
) -> kb.ReviewGenerationCreate:
    return kb.ReviewGenerationCreate(
        board="default",
        tenant=tenant,
        key=key,
        title="Review parent generation g1",
        body="Review the immutable target and report a structured verdict.",
        assignee="reviewer",
        created_by="blocked-recovery-router",
    )


def test_review_approval_is_copied_to_exact_completed_event_and_run(conn):
    reviewer = _running_task(conn)
    metadata = _review_metadata()

    assert kb.complete_task(
        conn,
        reviewer,
        summary="approved",
        metadata=metadata,
    )

    event = _completed_event(conn, reviewer)
    run = kb.get_run(conn, event.run_id)
    evidence = kb.get_terminal_review_evidence(conn, reviewer, event.id)
    assert event.payload["review"] == metadata["review"]
    assert event.run_id is not None
    assert run is not None
    assert (run.task_id, run.outcome, run.status) == (reviewer, "completed", "done")
    assert evidence == kb.ReviewCompletionEvidence(
        schema_version=1,
        verdict="APPROVED",
        target_kind="git",
        target_identity="example/repository",
        target_revision="a" * 40,
        findings=(),
    )
    assert evidence.approved is True


def test_post_completion_edit_cannot_rewrite_terminal_review_evidence(conn):
    reviewer = _running_task(conn)
    assert kb.complete_task(conn, reviewer, metadata=_review_metadata())
    completed = _completed_event(conn, reviewer)

    assert kb.edit_completed_task_result(
        conn,
        reviewer,
        result="changed",
        metadata=_review_metadata(verdict="REJECTED"),
    )

    persisted = next(
        event for event in kb.list_events(conn, reviewer) if event.id == completed.id
    )
    evidence = kb.get_terminal_review_evidence(conn, reviewer, completed.id)
    assert persisted.payload["review"]["verdict"] == "APPROVED"
    assert evidence is not None
    assert evidence.approved is True


@pytest.mark.parametrize(
    "review",
    [
        {"schema_version": 1, "verdict": "APPROVED", "findings": []},
        {
            "schema_version": True,
            "verdict": "APPROVED",
            "target": {
                "kind": "git",
                "identity": "example/repository",
                "revision": "a" * 40,
            },
            "findings": [],
        },
        {
            "schema_version": 1,
            "verdict": "APPROVED",
            "target": {
                "kind": "mutable-url",
                "identity": "example/repository",
                "revision": "a" * 40,
            },
            "findings": [],
        },
        {
            "schema_version": 1,
            "verdict": "APPROVED",
            "target": {
                "kind": "git",
                "identity": "example/repository",
                "revision": "a" * 40,
            },
            "findings": ["blocking prose"],
        },
        {
            "schema_version": 1,
            "verdict": [],
            "target": {
                "kind": "git",
                "identity": "example/repository",
                "revision": "a" * 40,
            },
            "findings": [],
        },
        {
            "schema_version": 1,
            "verdict": "APPROVED",
            "target": {
                "kind": {},
                "identity": "example/repository",
                "revision": "a" * 40,
            },
            "findings": [],
        },
    ],
)
def test_invalid_reserved_review_metadata_cannot_complete_task(conn, review):
    reviewer = _running_task(conn)
    run_id = kb.get_task(conn, reviewer).current_run_id

    with pytest.raises(ValueError, match="metadata.review"):
        kb.complete_task(conn, reviewer, metadata={"review": review})

    task = kb.get_task(conn, reviewer)
    run = kb.get_run(conn, run_id)
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert run is not None
    assert run.ended_at is None
    assert not [event for event in kb.list_events(conn, reviewer) if event.kind == "completed"]


def test_conflicting_approval_findings_are_not_usable(conn):
    reviewer = _running_task(conn)
    metadata = _review_metadata(
        findings=[{"severity": "blocking", "summary": "missing test"}]
    )
    assert kb.complete_task(conn, reviewer, metadata=metadata)
    event = _completed_event(conn, reviewer)

    assert kb.get_terminal_review_evidence(conn, reviewer, event.id) is None


def test_explicit_nonapproval_remains_structured_terminal_evidence(conn):
    reviewer = _running_task(conn)
    metadata = _review_metadata(
        verdict="CHANGES_REQUESTED",
        findings=[{"severity": "blocking", "summary": "missing test"}],
    )
    assert kb.complete_task(conn, reviewer, metadata=metadata)
    event = _completed_event(conn, reviewer)

    evidence = kb.get_terminal_review_evidence(conn, reviewer, event.id)

    assert evidence is not None
    assert evidence.verdict == "CHANGES_REQUESTED"
    assert evidence.findings == ({"severity": "blocking", "summary": "missing test"},)
    assert evidence.approved is False


def test_terminal_review_evidence_requires_the_selected_completed_event(conn):
    reviewer = _running_task(conn)
    claimed_event = kb.list_events(conn, reviewer)[-1]
    assert claimed_event.kind == "claimed"
    assert kb.complete_task(conn, reviewer, metadata=_review_metadata())

    assert kb.get_terminal_review_evidence(conn, reviewer, claimed_event.id) is None


def test_review_generation_create_is_permanent_and_single_attempt(conn):
    request = _review_generation_request()

    result = kb.create_review_generation_task(conn, request)

    task = kb.get_task(conn, result.task_id)
    record = kb.lookup_review_generation(
        conn,
        board=request.board,
        tenant=request.tenant,
        key=request.key,
    )
    assert result.created is True
    assert task is not None
    assert task.status == "ready"
    assert task.tenant is None
    assert task.idempotency_key == request.key
    assert task.max_runtime_seconds == 3600
    assert task.max_retries == 1
    assert kb.parent_ids(conn, task.id) == []
    assert record is not None
    assert record.task_id == task.id
    assert (record.board, record.tenant, record.key) == (
        "default",
        "",
        request.key,
    )


def test_review_generation_key_survives_archival(conn):
    request = _review_generation_request()
    first = kb.create_review_generation_task(conn, request)
    assert kb.archive_task(conn, first.task_id)

    replay = kb.create_review_generation_task(conn, request)

    assert replay == kb.ReviewGenerationCreateResult(
        task_id=first.task_id,
        created=False,
    )
    assert kb.get_task(conn, replay.task_id).status == "archived"


def test_review_generation_lookup_is_scoped_by_tenant(conn):
    request = _review_generation_request(
        key="review-lane:default:acme:t_parent:g1",
        tenant=" acme ",
    )
    created = kb.create_review_generation_task(conn, request)

    assert kb.get_task(conn, created.task_id).tenant == "acme"
    assert kb.lookup_review_generation(
        conn,
        board="default",
        tenant="acme",
        key=request.key,
    ).task_id == created.task_id
    assert kb.lookup_review_generation(
        conn,
        board="default",
        tenant="other",
        key=request.key,
    ) is None


def test_review_generation_create_rejects_malformed_reserved_key(conn):
    request = _review_generation_request(key="review-lane:malformed")

    with pytest.raises(ValueError, match="key"):
        kb.create_review_generation_task(conn, request)

    assert conn.execute(
        "SELECT COUNT(*) FROM kanban_scoped_idempotency"
    ).fetchone()[0] == 0


def test_concurrent_review_generation_create_returns_one_task(
    conn,
    kanban_home: Path,
):
    request = _review_generation_request()
    db_path = kb.kanban_db_path(board="default")

    def create_once() -> str:
        with kb.connect_closing(db_path) as other:
            return kb.create_review_generation_task(other, request).task_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        task_ids = list(executor.map(lambda _index: create_once(), range(8)))

    assert len(set(task_ids)) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
        (request.key,),
    ).fetchone()[0] == 1


def test_unique_legacy_review_generation_key_is_backfilled(conn):
    key = "review-lane:default::t_parent:legacy"
    legacy_task = kb.create_task(conn, title="legacy reviewer", idempotency_key=key)
    db_path = kb.kanban_db_path(board="default")
    conn.commit()

    kb.init_db(db_path)

    with kb.connect_closing(db_path) as migrated:
        record = kb.lookup_review_generation(
            migrated,
            board="default",
            tenant=None,
            key=key,
        )
    assert record is not None
    assert record.task_id == legacy_task


def test_duplicate_legacy_review_generation_keys_fail_migration_preflight(conn):
    key = "review-lane:default::t_parent:duplicate"
    first = kb.create_task(conn, title="first legacy reviewer", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second legacy reviewer", idempotency_key=key)
    assert second != first
    db_path = kb.kanban_db_path(board="default")
    conn.commit()

    with pytest.raises(kb.ReviewGenerationMigrationConflict, match="duplicate"):
        kb.init_db(db_path)

    raw = kb._sqlite_connect(db_path)
    try:
        count = raw.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            (key,),
        ).fetchone()[0]
    finally:
        raw.close()
    assert count == 2


def test_canonical_event_signature_is_stable_for_canonical_payload_order():
    first = kb.Event(
        id=42,
        task_id="t_example",
        kind="blocked",
        payload={"z": "last", "a": 1},
        created_at=1_700_000_000,
        run_id=7,
    )
    second = replace(first, payload={"a": 1, "z": "last"})

    assert kb.canonical_event_signature(first) == (
        "0a26d8de65aaa3c1a13809e06158e1f59518ea69854f14c5b3a5d3bdddb3983a"
    )
    assert kb.canonical_event_signature(second) == kb.canonical_event_signature(first)


@pytest.mark.parametrize(
    "changed",
    [
        {"task_id": "t_other"},
        {"id": 43},
        {"kind": "gave_up"},
        {"payload": {"a": 2, "z": "last"}},
        {"run_id": 8},
        {"created_at": 1_700_000_001},
    ],
)
def test_canonical_event_signature_covers_every_signed_field(changed):
    event = kb.Event(
        id=42,
        task_id="t_example",
        kind="blocked",
        payload={"a": 1, "z": "last"},
        created_at=1_700_000_000,
        run_id=7,
    )

    assert kb.canonical_event_signature(replace(event, **changed)) != (
        kb.canonical_event_signature(event)
    )


def test_reclassify_requires_exact_latest_block_signature(conn):
    task_id = _blocked_task(conn)
    events_before = kb.list_events(conn, task_id)
    request = kb.BlockReclassification(
        task_id=task_id,
        expected_block_event_signature="0" * 64,
        block_kind="review",
        operation_id="reclassify-stale",
    )

    result = kb.reclassify_block(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).block_kind is None
    assert kb.list_events(conn, task_id) == events_before


def test_operation_replay_returns_original_committed_event(conn):
    task_id = _blocked_task(conn)
    request = kb.BlockReclassification(
        task_id=task_id,
        expected_block_event_signature=_latest_event_signature(conn, task_id),
        block_kind="review",
        operation_id="reclassify-once",
    )

    first = kb.reclassify_block(conn, request)
    second = kb.reclassify_block(conn, request)

    assert first == second
    assert first.outcome == "committed"
    assert first.event_id is not None
    assert len(_operation_events(conn, task_id, "reclassify-once")) == 1
    assert kb.get_task(conn, task_id).block_kind == "review"


def test_reclassify_cannot_create_blocked_dependency_state(conn):
    task_id = _blocked_task(conn)
    request = kb.BlockReclassification(
        task_id=task_id,
        expected_block_event_signature=_latest_event_signature(conn, task_id),
        block_kind="dependency",
        operation_id="invalid-dependency-classification",
    )

    with pytest.raises(ValueError, match="dependency"):
        kb.reclassify_block(conn, request)

    task = kb.get_task(conn, task_id)
    assert (task.status, task.block_kind) == ("blocked", None)


def test_guarded_requeue_preserves_review_attempt_history(conn):
    task_id, terminal_event = _failed_review_generation(conn)
    runs_before = kb.list_runs(conn, task_id)
    request = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_status="blocked",
        expected_event_signature=kb.canonical_event_signature(terminal_event),
        operation_id="review-requeue-1",
    )

    result = kb.requeue_review_lane(conn, request)

    task = kb.get_task(conn, task_id)
    assert result.outcome == "committed"
    assert kb.list_runs(conn, task_id) == runs_before
    assert task.status == "ready"
    assert task.current_run_id is None
    assert task.consecutive_failures == 0
    event = _operation_events(conn, task_id, "review-requeue-1")[0]
    assert event.kind == "review_requeued"
    assert event.payload["source_terminal_signature"] == request.expected_event_signature


def test_review_terminal_generation_cannot_be_requeued_twice(conn):
    task_id, terminal_event = _failed_review_generation(conn)
    terminal_signature = kb.canonical_event_signature(terminal_event)
    first = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_status="blocked",
        expected_event_signature=terminal_signature,
        operation_id="review-requeue-first",
    )
    assert kb.requeue_review_lane(conn, first).outcome == "committed"
    second = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_status="ready",
        expected_event_signature=terminal_signature,
        operation_id="review-requeue-second",
    )

    result = kb.requeue_review_lane(conn, second)

    assert result.outcome == "precondition_changed"
    assert len(
        [event for event in kb.list_events(conn, task_id) if event.kind == "review_requeued"]
    ) == 1


def test_archived_review_generation_cannot_be_resurrected_by_requeue(conn):
    task_id, terminal_event = _failed_review_generation(conn)
    assert kb.archive_task(conn, task_id)
    request = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_status="archived",
        expected_event_signature=kb.canonical_event_signature(terminal_event),
        operation_id="review-requeue-archived",
    )

    result = kb.requeue_review_lane(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "archived"


def test_guarded_archive_requires_exact_review_generation_state(conn):
    created = kb.create_review_generation_task(
        conn,
        _review_generation_request(key="review-lane:default::t_parent:archive"),
    )
    request = kb.ReviewLaneMutation(
        task_id=created.task_id,
        expected_status="ready",
        expected_event_signature=_latest_event_signature(conn, created.task_id),
        operation_id="review-archive-1",
    )

    first = kb.archive_review_lane(conn, request)
    replay = kb.archive_review_lane(conn, request)

    assert first == replay
    assert first.outcome == "committed"
    assert kb.get_task(conn, created.task_id).status == "archived"
    event = _operation_events(conn, created.task_id, "review-archive-1")[0]
    assert event.kind == "review_archived"
    assert event.payload["source_event_signature"] == request.expected_event_signature


def test_transient_retry_requires_exact_terminal_signature(conn):
    task_id = _blocked_task(conn, kind="transient")
    events_before = kb.list_events(conn, task_id)
    request = kb.TransientRetryRequest(
        task_id=task_id,
        expected_terminal_signature="0" * 64,
        retry_ordinal=1,
        operation_id="transient-stale",
    )

    result = kb.retry_transient_task(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "blocked"
    assert kb.list_events(conn, task_id) == events_before


def test_transient_retry_ordinal_is_permanent_and_monotonic(conn):
    task_id = _blocked_task(conn, kind="transient")
    first = kb.TransientRetryRequest(
        task_id=task_id,
        expected_terminal_signature=_latest_event_signature(conn, task_id),
        retry_ordinal=1,
        operation_id="transient-1",
    )
    assert kb.retry_transient_task(conn, first).outcome == "committed"
    # A different human-block cycle resets the recurrence cause, but it must
    # not reset the permanent transient-retry ordinal reconstructed from events.
    assert kb.block_task(conn, task_id, reason="needs a decision", kind="needs_input")
    assert kb.unblock_task(conn, task_id)
    assert kb.block_task(conn, task_id, reason="transient again", kind="transient")

    repeated_ordinal = kb.TransientRetryRequest(
        task_id=task_id,
        expected_terminal_signature=_latest_event_signature(conn, task_id),
        retry_ordinal=1,
        operation_id="transient-repeated-ordinal",
    )
    assert kb.retry_transient_task(conn, repeated_ordinal).outcome == (
        "precondition_changed"
    )

    second = replace(
        repeated_ordinal,
        retry_ordinal=2,
        operation_id="transient-2",
    )
    assert kb.retry_transient_task(conn, second).outcome == "committed"
    retry_events = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == "transient_retry_authorized"
    ]
    assert [event.payload["retry_ordinal"] for event in retry_events] == [1, 2]
    assert kb.get_task(conn, task_id).status == "ready"


def test_escalation_acknowledgement_is_exact_and_lifecycle_neutral(conn):
    task_id = _blocked_task(conn, kind="needs_input")
    before = kb.get_task(conn, task_id)
    runs_before = kb.list_runs(conn, task_id)
    request = kb.EscalationAcknowledgement(
        task_id=task_id,
        escalation_signature="a" * 64,
        expected_event_signature=_latest_event_signature(conn, task_id),
        actor="operator",
        operation_id="ack-1",
    )

    result = kb.acknowledge_escalation(conn, request)

    after = kb.get_task(conn, task_id)
    assert result.outcome == "committed"
    assert (
        after.status,
        after.block_kind,
        after.block_recurrences,
        after.consecutive_failures,
        after.current_run_id,
        after.claim_lock,
        after.claim_expires,
        after.worker_pid,
        after.assignee,
    ) == (
        before.status,
        before.block_kind,
        before.block_recurrences,
        before.consecutive_failures,
        before.current_run_id,
        before.claim_lock,
        before.claim_expires,
        before.worker_pid,
        before.assignee,
    )
    assert kb.list_runs(conn, task_id) == runs_before
    event = _operation_events(conn, task_id, "ack-1")[0]
    assert event.kind == "escalation_acknowledged"
    assert event.payload["signature"] == "a" * 64
    assert event.payload["actor"] == "operator"


def test_operation_keyed_link_and_unlink_replay_original_events(conn):
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child")

    linked = kb.link_tasks(conn, parent, child, operation_id="link-once")
    linked_replay = kb.link_tasks(conn, parent, child, operation_id="link-once")

    assert linked == linked_replay
    assert linked.outcome == "committed"
    assert kb.parent_ids(conn, child) == [parent]
    assert len(_operation_events(conn, child, "link-once")) == 1

    unlinked = kb.unlink_tasks(conn, parent, child, operation_id="unlink-once")
    unlinked_replay = kb.unlink_tasks(
        conn,
        parent,
        child,
        operation_id="unlink-once",
    )

    assert unlinked == unlinked_replay
    assert unlinked.outcome == "committed"
    assert kb.parent_ids(conn, child) == []
    assert len(_operation_events(conn, child, "unlink-once")) == 1


def test_operation_keyed_completion_replays_without_second_terminal_event(conn):
    task_id = kb.create_task(conn, title="automation completion")

    first = kb.complete_task(
        conn,
        task_id,
        result="done",
        operation_id="complete-once",
    )
    second = kb.complete_task(
        conn,
        task_id,
        result="done",
        operation_id="complete-once",
    )

    assert first is True
    assert second is True
    completed = [
        event for event in kb.list_events(conn, task_id) if event.kind == "completed"
    ]
    assert len(completed) == 1
    assert completed[0].payload["operation_id"] == "complete-once"
