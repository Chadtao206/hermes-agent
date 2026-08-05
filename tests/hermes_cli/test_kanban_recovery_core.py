"""Behavioral tests for blocked-recovery primitives in the Kanban core."""

from __future__ import annotations

import concurrent.futures
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

import pytest
import hermes_time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc


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
    created = kb.create_review_generation_task(conn, _review_generation_request(conn, ))
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
    conn,
    *,
    key: str = "review-lane:default::t_parent:g1",
    tenant: str | None = None,
) -> kb.ReviewGenerationCreate:
    request = kb.ReviewGenerationCreate(
        board="default",
        tenant=tenant,
        key=key,
        title="Review parent generation g1",
        body="Review the immutable target and report a structured verdict.",
        assignee="reviewer",
        created_by="blocked-recovery-router",
    )
    try:
        _board, normalized_tenant, parent_task_id, _generation = (
            kb._review_generation_identity_from_key(key)
        )
    except kb.ReviewGenerationMigrationConflict:
        return request
    parent = kb.get_task(conn, parent_task_id)
    if parent is None:
        generated_id = kb.create_task(
            conn,
            title="implementation awaiting review",
            tenant=normalized_tenant,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET id = ? WHERE id = ?",
                (parent_task_id, generated_id),
            )
            conn.execute(
                "UPDATE task_events SET task_id = ? WHERE task_id = ?",
                (parent_task_id, generated_id),
            )
        parent = kb.get_task(conn, parent_task_id)
    if parent.status != "blocked":
        assert kb.block_task(
            conn,
            parent_task_id,
            reason="review handoff",
            kind="review",
        )
    return replace(
        request,
        parent_preconditions=_review_parent_preconditions(conn, parent_task_id),
        operation_id="test-create:" + hashlib.sha256(key.encode()).hexdigest(),
    )


def _review_parent_preconditions(
    conn,
    parent_task_id: str,
) -> kb.ReviewParentPreconditions:
    latest = _latest_event(conn, parent_task_id)
    return kb.ReviewParentPreconditions(
        task_id=parent_task_id,
        expected_status="blocked",
        expected_block_kind="review",
        expected_event_signature=kb.canonical_event_signature(latest),
        expected_review_input_digest=kb.review_input_digest(conn, parent_task_id),
    )


def _review_lane_parent_preconditions(
    conn,
    task_id: str,
) -> kb.ReviewParentPreconditions:
    row = conn.execute(
        "SELECT key FROM kanban_scoped_idempotency "
        "WHERE namespace = 'review-lane' AND task_id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    _board, _tenant, parent_task_id, _generation = (
        kb._review_generation_identity_from_key(row["key"])
    )
    return _review_parent_preconditions(conn, parent_task_id)


def _guarded_review_generation_request(
    conn,
    *,
    parent_task_id: str | None = None,
    generation: str = "g1",
    operation_id: str = "create-review-generation-1",
    tenant: str | None = None,
) -> tuple[str, kb.ReviewGenerationCreate]:
    parent = parent_task_id or kb.create_task(
        conn,
        title="implementation awaiting review",
        tenant=tenant,
    )
    if kb.get_task(conn, parent).status != "blocked":
        assert kb.block_task(conn, parent, reason="review handoff", kind="review")
    request = replace(
        _review_generation_request(
            conn,
            key=f"review-lane:default:{tenant or ''}:{parent}:{generation}",
            tenant=tenant,
        ),
        parent_preconditions=_review_parent_preconditions(conn, parent),
        operation_id=operation_id,
    )
    return parent, request


def _guarded_review_lane_request(
    conn,
    *,
    mutation,
    operation_id: str,
) -> tuple[str, str, kb.ReviewLaneMutation]:
    parent, create_request = _guarded_review_generation_request(
        conn,
        operation_id=f"create-for-{operation_id}",
    )
    created = kb.create_review_generation_task(conn, create_request)
    assert created.outcome == "committed"
    if mutation is kb.requeue_review_lane:
        assert kb.claim_task(conn, created.task_id, claimer="reviewer") is not None
        assert kb._record_spawn_failure(
            conn,
            created.task_id,
            "review worker exited",
            failure_limit=1,
        )
    task = kb.get_task(conn, created.task_id)
    return parent, created.task_id, kb.ReviewLaneMutation(
        task_id=created.task_id,
        expected_tenant="",
        expected_status=task.status,
        expected_event_signature=_latest_event_signature(conn, created.task_id),
        operation_id=operation_id,
        parent_preconditions=create_request.parent_preconditions,
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


def test_ready_review_completion_synthesizes_usable_terminal_run(conn):
    reviewer = kb.create_task(conn, title="manual reviewer", assignee="reviewer")

    assert kb.complete_task(conn, reviewer, metadata=_review_metadata())

    event = _completed_event(conn, reviewer)
    run = kb.get_run(conn, event.run_id)
    evidence = kb.get_terminal_review_evidence(conn, reviewer, event.id)
    assert run is not None
    assert (run.status, run.outcome) == ("done", "completed")
    assert evidence is not None and evidence.approved


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
    request = _review_generation_request(conn, )

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
    request = _review_generation_request(conn, )
    first = kb.create_review_generation_task(conn, request)
    assert kb.archive_task(conn, first.task_id)

    replay = kb.create_review_generation_task(conn, request)

    assert replay.task_id == first.task_id
    assert replay.created is False
    assert replay.outcome == "deduplicated"
    assert replay.operation_id == request.operation_id
    assert kb.get_task(conn, replay.task_id).status == "archived"


def test_review_generation_project_source_gets_fresh_task_keyed_worktree(conn, tmp_path):
    repository = tmp_path / "repository"
    source = kb.create_task(
        conn,
        title="implementation",
        project_id="project-1",
        workspace_kind="worktree",
        workspace_path=str(repository / ".worktrees" / "placeholder"),
        branch_name="project-1/placeholder",
    )
    source_path = repository / ".worktrees" / source
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ?, branch_name = ?, project_id = ? WHERE id = ?",
            (str(source_path), f"project-1/{source}", "project-1", source),
        )
    request = _review_generation_request(
        conn,
        key=f"review-lane:default::${source}:g-project".replace("$", ""),
    )
    request = replace(
        request,
        project_id="project-1",
        project_source_task_id=source,
    )

    created = kb.create_review_generation_task(conn, request)
    task = kb.get_task(conn, created.task_id)

    assert task is not None
    assert task.project_id == "project-1"
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repository / ".worktrees" / created.task_id)
    assert task.workspace_path != str(source_path)
    assert task.branch_name is not None
    assert task.branch_name.startswith(f"project-1/{created.task_id}-")


def test_review_generation_lookup_is_scoped_by_tenant(conn):
    request = _review_generation_request(
        conn,
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
    request = _review_generation_request(conn, key="review-lane:malformed")

    with pytest.raises(ValueError, match="key"):
        kb.create_review_generation_task(conn, request)

    assert conn.execute(
        "SELECT COUNT(*) FROM kanban_scoped_idempotency"
    ).fetchone()[0] == 0


def test_concurrent_review_generation_create_returns_one_task(
    conn,
    kanban_home: Path,
):
    request = _review_generation_request(conn, )
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


def test_duplicate_legacy_review_generation_keys_are_quarantined(conn):
    key = "review-lane:default::t_parent:duplicate"
    first = kb.create_task(conn, title="first legacy reviewer", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second legacy reviewer", idempotency_key=key)
    assert second != first
    db_path = kb.kanban_db_path(board="default")
    conn.execute("DROP INDEX IF EXISTS idx_events_run")
    conn.execute("ALTER TABLE task_events DROP COLUMN run_id")
    conn.commit()

    kb.init_db(db_path)

    with kb.connect_closing(db_path) as migrated:
        rows = migrated.execute(
            "SELECT id, idempotency_key FROM tasks WHERE id IN (?, ?) ORDER BY id",
            (first, second),
        ).fetchall()
        assert all(
            row["idempotency_key"].startswith("quarantined-review-lane:")
            for row in rows
        )
        assert len({row["idempotency_key"] for row in rows}) == 2
        assert kb.lookup_review_generation(
            migrated,
            board="default",
            tenant=None,
            key=key,
        ) is None
        for task_id in (first, second):
            event = kb.list_events(migrated, task_id)[-1]
            assert event.kind == "review_generation_key_quarantined"
            assert event.payload["original_key"] == key

        replacement = kb.create_review_generation_task(
            migrated,
            _review_generation_request(conn, key=key),
        )
        replay = kb.create_review_generation_task(
            migrated,
            _review_generation_request(conn, key=key),
        )
        assert replacement.created is True
        assert replay.task_id == replacement.task_id


def test_duplicate_review_key_quarantine_rolls_back_as_one_group(conn, monkeypatch):
    key = "review-lane:default::t_parent:crash-group"
    first = kb.create_task(conn, title="first duplicate", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second duplicate", idempotency_key=key)
    real_append = kb._append_event
    quarantine_events = 0

    def fail_after_first_quarantine(
        connection,
        task_id,
        kind,
        payload=None,
        *,
        run_id=None,
    ):
        nonlocal quarantine_events
        event_id = real_append(
            connection,
            task_id,
            kind,
            payload,
            run_id=run_id,
        )
        if kind == "review_generation_key_quarantined":
            quarantine_events += 1
            if quarantine_events == 1:
                raise RuntimeError("simulated migration crash")
        return event_id

    monkeypatch.setattr(kb, "_append_event", fail_after_first_quarantine)

    with pytest.raises(RuntimeError, match="simulated migration crash"):
        kb._migrate_review_generation_registry(conn)

    rows = conn.execute(
        "SELECT id, idempotency_key FROM tasks WHERE id IN (?, ?) ORDER BY id",
        (first, second),
    ).fetchall()
    assert [row["idempotency_key"] for row in rows] == [key, key]
    assert conn.execute(
        "SELECT COUNT(*) FROM kanban_scoped_idempotency WHERE key = ?",
        (key,),
    ).fetchone()[0] == 0
    assert not any(
        event.kind == "review_generation_key_quarantined"
        for task_id in (first, second)
        for event in kb.list_events(conn, task_id)
    )


def test_duplicate_review_key_quarantine_repairs_partial_legacy_migration(conn):
    key = "review-lane:default::t_parent:partial-migration"
    first = kb.create_task(conn, title="first duplicate", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second duplicate", idempotency_key=key)
    digest = hashlib.sha256(f"{key}\0{first}".encode()).hexdigest()
    quarantined_key = f"quarantined-review-lane:{digest}:{first}"
    conn.execute(
        "UPDATE tasks SET idempotency_key = ? WHERE id = ?",
        (quarantined_key, first),
    )
    kb._append_event(
        conn,
        first,
        "review_generation_key_quarantined",
        {
            "original_key": key,
            "quarantined_key": quarantined_key,
            "reason": "duplicate reserved review generation key",
        },
    )
    conn.commit()

    kb._migrate_review_generation_registry(conn)

    assert kb.get_task(conn, second).idempotency_key.startswith(
        "quarantined-review-lane:"
    )
    assert kb.lookup_review_generation(
        conn,
        board="default",
        tenant=None,
        key=key,
    ) is None


def test_historical_duplicate_quarantine_does_not_poison_replacement(conn):
    key = "review-lane:default::t_parent:replacement"
    first = kb.create_task(conn, title="first duplicate", idempotency_key=key)
    assert kb.archive_task(conn, first)
    kb.create_task(conn, title="second duplicate", idempotency_key=key)
    kb._migrate_review_generation_registry(conn)
    replacement = kb.create_review_generation_task(
        conn,
        _review_generation_request(conn, key=key),
    )

    kb._migrate_review_generation_registry(conn)

    assert kb.get_task(conn, replacement.task_id).idempotency_key == key
    record = kb.lookup_review_generation(
        conn,
        board="default",
        tenant=None,
        key=key,
    )
    assert record is not None
    assert record.task_id == replacement.task_id


def test_duplicate_quarantine_repairs_eventless_partial_legacy_migration(conn):
    key = "review-lane:default::t_parent:eventless-partial"
    first = kb.create_task(conn, title="first duplicate", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second duplicate", idempotency_key=key)
    digest = hashlib.sha256(f"{key}\0{first}".encode()).hexdigest()
    quarantined_key = f"quarantined-review-lane:{digest}:{first}"
    conn.execute(
        "UPDATE tasks SET idempotency_key = ? WHERE id = ?",
        (quarantined_key, first),
    )
    conn.commit()

    kb._migrate_review_generation_registry(conn)

    assert kb.get_task(conn, second).idempotency_key.startswith(
        "quarantined-review-lane:"
    )
    assert kb.lookup_review_generation(
        conn,
        board="default",
        tenant=None,
        key=key,
    ) is None
    event = kb.list_events(conn, first)[-1]
    assert event.kind == "review_generation_key_quarantined"
    assert event.payload["original_key"] == key
    replacement = kb.create_review_generation_task(
        conn,
        _review_generation_request(conn, key=key),
    )

    kb._migrate_review_generation_registry(conn)

    assert kb.get_task(conn, replacement.task_id).idempotency_key == key


def test_eventless_final_duplicate_rewrite_does_not_poison_replacement(conn):
    key = "review-lane:default::t_parent:eventless-final"
    first = kb.create_task(conn, title="first duplicate", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second duplicate", idempotency_key=key)
    for task_id in (first, second):
        digest = hashlib.sha256(f"{key}\0{task_id}".encode()).hexdigest()
        quarantined_key = f"quarantined-review-lane:{digest}:{task_id}"
        conn.execute(
            "UPDATE tasks SET idempotency_key = ? WHERE id = ?",
            (quarantined_key, task_id),
        )
        if task_id == first:
            kb._append_event(
                conn,
                first,
                "review_generation_key_quarantined",
                {
                    "original_key": key,
                    "quarantined_key": quarantined_key,
                    "reason": "duplicate reserved review generation key",
                },
            )
    conn.commit()

    kb._migrate_review_generation_registry(conn)
    replacement = kb.create_review_generation_task(
        conn,
        _review_generation_request(conn, key=key),
    )

    kb._migrate_review_generation_registry(conn)

    assert kb.get_task(conn, replacement.task_id).idempotency_key == key


def test_replacement_cannot_race_between_quarantine_classification_and_repair(
    conn,
    monkeypatch,
):
    key = "review-lane:default::t_parent:classification-race"
    first = kb.create_task(conn, title="first duplicate", idempotency_key=key)
    assert kb.archive_task(conn, first)
    second = kb.create_task(conn, title="second duplicate", idempotency_key=key)
    for task_id in (first, second):
        digest = hashlib.sha256(f"{key}\0{task_id}".encode()).hexdigest()
        quarantined_key = f"quarantined-review-lane:{digest}:{task_id}"
        conn.execute(
            "UPDATE tasks SET idempotency_key = ? WHERE id = ?",
            (quarantined_key, task_id),
        )
        if task_id == first:
            kb._append_event(
                conn,
                first,
                "review_generation_key_quarantined",
                {
                    "original_key": key,
                    "quarantined_key": quarantined_key,
                    "reason": "duplicate reserved review generation key",
                },
            )
    conn.commit()
    replacement_request = _review_generation_request(conn, key=key)

    classification_reached = threading.Event()
    writer_attempted = threading.Event()
    thread_role = threading.local()
    real_quarantine_key = kb._quarantined_review_generation_key
    real_write_txn = kb.write_txn
    paused = False

    def pause_during_classification(original_key, task_id):
        nonlocal paused
        result = real_quarantine_key(original_key, task_id)
        if original_key == key and not paused:
            paused = True
            classification_reached.set()
            assert writer_attempted.wait(timeout=5)
        return result

    @contextlib.contextmanager
    def observed_write_txn(connection):
        if getattr(thread_role, "replacement_writer", False):
            writer_attempted.set()
        with real_write_txn(connection) as transaction:
            yield transaction

    monkeypatch.setattr(
        kb,
        "_quarantined_review_generation_key",
        pause_during_classification,
    )
    monkeypatch.setattr(kb, "write_txn", observed_write_txn)
    db_path = kb.kanban_db_path(board="default")

    def migrate_once() -> None:
        with kb.connect_closing(db_path) as migration_conn:
            kb._migrate_review_generation_registry(migration_conn)

    def create_replacement() -> str:
        thread_role.replacement_writer = True
        assert classification_reached.wait(timeout=5)
        with kb.connect_closing(db_path) as writer_conn:
            return kb.create_review_generation_task(
                writer_conn,
                replacement_request,
            ).task_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        migration = executor.submit(migrate_once)
        replacement = executor.submit(create_replacement)
        migration.result(timeout=10)
        replacement_id = replacement.result(timeout=10)

    kb._migrate_review_generation_registry(conn)

    assert kb.get_task(conn, replacement_id).idempotency_key == key


def test_cross_board_legacy_review_key_is_quarantined(conn):
    key = "review-lane:other-board::t_parent:g1"
    task_id = kb.create_task(conn, title="mislabeled reviewer", idempotency_key=key)

    kb._migrate_review_generation_registry(conn)

    task = kb.get_task(conn, task_id)
    assert task.idempotency_key.startswith("quarantined-review-lane:")
    event = kb.list_events(conn, task_id)[-1]
    assert event.kind == "review_generation_key_quarantined"
    assert event.payload["reason"] == "review generation board mismatch"
    assert kb.lookup_review_generation(
        conn,
        board="other-board",
        tenant=None,
        key=key,
    ) is None


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


def test_operation_replay_conflicts_when_mutation_intent_changes(conn):
    task_id = _blocked_task(conn)
    original_signature = _latest_event_signature(conn, task_id)
    first = kb.BlockReclassification(
        task_id=task_id,
        expected_block_event_signature=original_signature,
        block_kind="review",
        operation_id="reclassify-bound-intent",
    )
    changed = replace(first, block_kind="needs_input")

    assert kb.reclassify_block(conn, first).outcome == "committed"
    replay = kb.reclassify_block(conn, changed)

    assert replay.outcome == "conflict"
    assert kb.get_task(conn, task_id).block_kind == "review"


def test_reclassify_fences_against_the_latest_reclassification(conn):
    task_id = _blocked_task(conn)
    original_signature = _latest_event_signature(conn, task_id)
    first = kb.reclassify_block(
        conn,
        kb.BlockReclassification(
            task_id=task_id,
            expected_block_event_signature=original_signature,
            block_kind="review",
            operation_id="reclassify-review",
        ),
    )
    assert first.outcome == "committed"
    reclassified_signature = _latest_event_signature(conn, task_id)

    stale = kb.reclassify_block(
        conn,
        kb.BlockReclassification(
            task_id=task_id,
            expected_block_event_signature=original_signature,
            block_kind="needs_input",
            operation_id="reclassify-stale-source",
        ),
    )
    current = kb.reclassify_block(
        conn,
        kb.BlockReclassification(
            task_id=task_id,
            expected_block_event_signature=reclassified_signature,
            block_kind="needs_input",
            operation_id="reclassify-current-source",
        ),
    )

    assert stale.outcome == "precondition_changed"
    assert current.outcome == "committed"
    assert kb.get_task(conn, task_id).block_kind == "needs_input"


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
        expected_tenant="",
        expected_status="blocked",
        expected_event_signature=kb.canonical_event_signature(terminal_event),
        operation_id="review-requeue-1",
        parent_preconditions=_review_lane_parent_preconditions(conn, task_id),
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
        expected_tenant="",
        expected_status="blocked",
        expected_event_signature=terminal_signature,
        operation_id="review-requeue-first",
        parent_preconditions=_review_lane_parent_preconditions(conn, task_id),
    )
    assert kb.requeue_review_lane(conn, first).outcome == "committed"
    second = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_tenant="",
        expected_status="ready",
        expected_event_signature=terminal_signature,
        operation_id="review-requeue-second",
        parent_preconditions=_review_lane_parent_preconditions(conn, task_id),
    )

    result = kb.requeue_review_lane(conn, second)

    assert result.outcome == "precondition_changed"
    assert len(
        [event for event in kb.list_events(conn, task_id) if event.kind == "review_requeued"]
    ) == 1


def test_review_requeue_rejects_a_later_manual_reclassification(conn):
    task_id, terminal_event = _failed_review_generation(conn)
    terminal_signature = kb.canonical_event_signature(terminal_event)
    reclassified = kb.reclassify_block(
        conn,
        kb.BlockReclassification(
            task_id=task_id,
            expected_block_event_signature=terminal_signature,
            block_kind="needs_input",
            operation_id="manual-review-lane-reclassification",
        ),
    )
    assert reclassified.outcome == "committed"
    request = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_tenant="",
        expected_status="blocked",
        expected_event_signature=terminal_signature,
        operation_id="stale-review-requeue",
        parent_preconditions=_review_lane_parent_preconditions(conn, task_id),
    )

    result = kb.requeue_review_lane(conn, request)

    task = kb.get_task(conn, task_id)
    assert result.outcome == "precondition_changed"
    assert (task.status, task.block_kind) == ("blocked", "needs_input")


def test_archived_review_generation_cannot_be_resurrected_by_requeue(conn):
    task_id, terminal_event = _failed_review_generation(conn)
    assert kb.archive_task(conn, task_id)
    request = kb.ReviewLaneMutation(
        task_id=task_id,
        expected_tenant="",
        expected_status="archived",
        expected_event_signature=kb.canonical_event_signature(terminal_event),
        operation_id="review-requeue-archived",
        parent_preconditions=_review_lane_parent_preconditions(conn, task_id),
    )

    result = kb.requeue_review_lane(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "archived"


def test_guarded_archive_requires_exact_review_generation_state(conn):
    created = kb.create_review_generation_task(
        conn,
        _review_generation_request(conn, key="review-lane:default::t_parent:archive"),
    )
    request = kb.ReviewLaneMutation(
        task_id=created.task_id,
        expected_tenant="",
        expected_status="ready",
        expected_event_signature=_latest_event_signature(conn, created.task_id),
        operation_id="review-archive-1",
        parent_preconditions=_review_lane_parent_preconditions(conn, created.task_id),
    )

    first = kb.archive_review_lane(conn, request)
    replay = kb.archive_review_lane(conn, request)

    assert first == replay
    assert first.outcome == "committed"
    assert kb.get_task(conn, created.task_id).status == "archived"
    event = _operation_events(conn, created.task_id, "review-archive-1")[0]
    assert event.kind == "review_archived"
    assert event.payload["source_event_signature"] == request.expected_event_signature


@pytest.mark.parametrize("mutation", [kb.requeue_review_lane, kb.archive_review_lane])
def test_review_lane_mutation_rejects_stale_named_tenant_without_event(conn, mutation):
    created = kb.create_review_generation_task(
        conn,
        _review_generation_request(
            conn,
            key="review-lane:default:tenant-a:t_parent:tenant-guard",
            tenant="tenant-a",
        ),
    )
    if mutation is kb.requeue_review_lane:
        assert kb.claim_task(conn, created.task_id, claimer="reviewer") is not None
        assert kb._record_spawn_failure(conn, created.task_id, "failed", failure_limit=1)
    before = kb.list_events(conn, created.task_id)
    task = kb.get_task(conn, created.task_id)
    request = kb.ReviewLaneMutation(
        task_id=created.task_id,
        expected_tenant="tenant-b",
        expected_status=task.status,
        expected_event_signature=kb.canonical_event_signature(before[-1]),
        operation_id=f"stale-tenant-{mutation.__name__}",
        parent_preconditions=_review_lane_parent_preconditions(conn, created.task_id),
    )

    result = mutation(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, created.task_id).status == task.status
    assert kb.list_events(conn, created.task_id) == before


def test_review_lane_mutation_accepts_exact_named_and_empty_tenants(conn):
    named = kb.create_review_generation_task(
        conn,
        _review_generation_request(
            conn,
            key="review-lane:default:tenant-a:t_parent:named-tenant",
            tenant="tenant-a",
        ),
    )
    empty = kb.create_review_generation_task(
        conn,
        _review_generation_request(
            conn,
            key="review-lane:default::t_parent_empty:empty-tenant",
        ),
    )

    for task_id, tenant in ((named.task_id, "tenant-a"), (empty.task_id, "")):
        request = kb.ReviewLaneMutation(
            task_id=task_id,
            expected_tenant=tenant,
            expected_status="ready",
            expected_event_signature=_latest_event_signature(conn, task_id),
            operation_id=f"exact-tenant-{tenant or 'empty'}",
            parent_preconditions=_review_lane_parent_preconditions(conn, task_id),
        )
        assert kb.archive_review_lane(conn, request).outcome == "committed"


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


def test_transient_retry_rejects_reclassification_aba(conn):
    task_id = _blocked_task(conn, kind="transient")
    terminal_signature = _latest_event_signature(conn, task_id)
    first = kb.reclassify_block(
        conn,
        kb.BlockReclassification(
            task_id=task_id,
            expected_block_event_signature=terminal_signature,
            block_kind="needs_input",
            operation_id="transient-to-human",
        ),
    )
    assert first.outcome == "committed"
    second = kb.reclassify_block(
        conn,
        kb.BlockReclassification(
            task_id=task_id,
            expected_block_event_signature=_latest_event_signature(conn, task_id),
            block_kind="transient",
            operation_id="human-back-to-transient",
        ),
    )
    assert second.outcome == "committed"
    request = kb.TransientRetryRequest(
        task_id=task_id,
        expected_terminal_signature=terminal_signature,
        retry_ordinal=1,
        operation_id="stale-transient-retry",
    )

    result = kb.retry_transient_task(conn, request)

    task = kb.get_task(conn, task_id)
    assert result.outcome == "precondition_changed"
    assert (task.status, task.block_kind) == ("blocked", "transient")


@pytest.mark.parametrize("assignee", [None, ""], ids=["unknown", "empty"])
def test_transient_retry_fails_closed_on_comment_with_unknown_assignee(
    conn,
    assignee,
):
    task_id = kb.create_task(
        conn,
        title="unknown worker identity",
        assignee=None,
    )
    if assignee == "":
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = '' WHERE id = ?",
                (task_id,),
            )
    assert kb.block_task(conn, task_id, reason="temporary", kind="transient")
    terminal_signature = _latest_event_signature(conn, task_id)
    kb.add_comment(conn, task_id, "operator", "durable note after planning")

    result = kb.retry_transient_task(
        conn,
        kb.TransientRetryRequest(
            task_id=task_id,
            expected_terminal_signature=terminal_signature,
            retry_ordinal=1,
            operation_id=f"unknown-assignee-{assignee!r}",
        ),
    )

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "blocked"
    assert not _operation_events(conn, task_id, f"unknown-assignee-{assignee!r}")


def test_transient_retry_rechecks_assignee_comment_inside_write_transaction(conn):
    task_id = kb.create_task(
        conn,
        title="worker commented after planning",
        assignee="reviewer",
    )
    assert kb.block_task(conn, task_id, reason="temporary", kind="transient")
    terminal_signature = _latest_event_signature(conn, task_id)
    kb.add_comment(conn, task_id, "reviewer", "durable work after planning")

    result = kb.retry_transient_task(
        conn,
        kb.TransientRetryRequest(
            task_id=task_id,
            expected_terminal_signature=terminal_signature,
            retry_ordinal=1,
            operation_id="comment-race",
        ),
    )

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "blocked"
    assert not _operation_events(conn, task_id, "comment-race")


def test_transient_retry_rechecks_respawn_guard_inside_write_transaction(conn):
    task_id = kb.create_task(
        conn,
        title="respawn guarded after planning",
        assignee="reviewer",
    )
    assert kb.block_task(conn, task_id, reason="temporary", kind="transient")
    terminal_signature = _latest_event_signature(conn, task_id)
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            "respawn_guarded",
            {"reason": "durable worker side effect"},
        )

    result = kb.retry_transient_task(
        conn,
        kb.TransientRetryRequest(
            task_id=task_id,
            expected_terminal_signature=terminal_signature,
            retry_ordinal=1,
            operation_id="respawn-race",
        ),
    )

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, task_id).status == "blocked"
    assert not _operation_events(conn, task_id, "respawn-race")


def test_transient_retry_allows_demonstrably_foreign_comment(conn):
    task_id = kb.create_task(
        conn,
        title="operator comment is not worker output",
        assignee="reviewer",
    )
    assert kb.block_task(conn, task_id, reason="temporary", kind="transient")
    terminal_signature = _latest_event_signature(conn, task_id)
    kb.add_comment(conn, task_id, "operator", "administrative note")

    result = kb.retry_transient_task(
        conn,
        kb.TransientRetryRequest(
            task_id=task_id,
            expected_terminal_signature=terminal_signature,
            retry_ordinal=1,
            operation_id="foreign-comment",
        ),
    )

    assert result.outcome == "committed"
    assert kb.get_task(conn, task_id).status == "ready"
    assert len(_operation_events(conn, task_id, "foreign-comment")) == 1


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


def test_operation_keyed_unlink_replay_repairs_child_readiness(conn, monkeypatch):
    parent = kb.create_task(conn, title="blocking parent")
    child = kb.create_task(conn, title="child", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    real_recompute = kb.recompute_ready
    monkeypatch.setattr(
        kb,
        "recompute_ready",
        lambda connection: (_ for _ in ()).throw(RuntimeError("post-commit crash")),
    )

    with pytest.raises(RuntimeError, match="post-commit crash"):
        kb.unlink_tasks(conn, parent, child, operation_id="unlink-crash-replay")

    assert kb.parent_ids(conn, child) == []
    assert kb.get_task(conn, child).status == "todo"
    monkeypatch.setattr(kb, "recompute_ready", real_recompute)

    replay = kb.unlink_tasks(
        conn,
        parent,
        child,
        operation_id="unlink-crash-replay",
    )

    assert replay.outcome == "committed"
    assert kb.get_task(conn, child).status == "ready"


def test_operation_keyed_link_replay_rejects_a_different_parent(conn):
    first_parent = kb.create_task(conn, title="first parent")
    second_parent = kb.create_task(conn, title="second parent")
    child = kb.create_task(conn, title="child")

    assert kb.link_tasks(
        conn,
        first_parent,
        child,
        operation_id="link-bound-intent",
    ).outcome == "committed"
    replay = kb.link_tasks(
        conn,
        second_parent,
        child,
        operation_id="link-bound-intent",
    )

    assert replay.outcome == "conflict"
    assert kb.parent_ids(conn, child) == [first_parent]


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


def test_operation_keyed_completion_rejects_a_different_result(conn):
    task_id = kb.create_task(conn, title="automation completion")
    assert kb.complete_task(
        conn,
        task_id,
        result="original",
        operation_id="complete-bound-intent",
    )

    with pytest.raises(ValueError, match="already bound"):
        kb.complete_task(
            conn,
            task_id,
            result="changed",
            operation_id="complete-bound-intent",
        )

    assert kb.get_task(conn, task_id).result == "original"


def test_operation_keyed_completion_replay_repairs_child_readiness(
    conn,
    monkeypatch,
):
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child", parents=[parent])
    real_recompute = kb.recompute_ready
    cleanup_transactions: list[bool] = []
    monkeypatch.setattr(
        kb,
        "_cleanup_workspace",
        lambda connection, _task_id: cleanup_transactions.append(
            connection.in_transaction
        ),
    )
    monkeypatch.setattr(
        kb,
        "recompute_ready",
        lambda connection: (_ for _ in ()).throw(RuntimeError("post-commit crash")),
    )

    with pytest.raises(RuntimeError, match="post-commit crash"):
        kb.complete_task(
            conn,
            parent,
            result="done",
            operation_id="complete-crash-replay",
        )

    assert kb.get_task(conn, parent).status == "done"
    assert kb.get_task(conn, child).status == "todo"
    monkeypatch.setattr(kb, "recompute_ready", real_recompute)

    assert kb.complete_task(
        conn,
        parent,
        result="done",
        operation_id="complete-crash-replay",
    )
    assert kb.get_task(conn, child).status == "ready"
    assert cleanup_transactions == [False]


def _lease_request(holder_token: str, *, operation_id: str) -> kb.LeaseRequest:
    return kb.LeaseRequest(
        controller_id="blocked-recovery",
        board="default",
        holder_token=holder_token,
        ttl_seconds=300,
        census_signature="c" * 64,
        operation_id=operation_id,
    )


def _clock(monkeypatch: pytest.MonkeyPatch, epoch: int) -> list[int]:
    current = [epoch]
    monkeypatch.setattr(
        hermes_time,
        "now",
        lambda: datetime.fromtimestamp(current[0], tz=timezone.utc),
    )
    return current


def test_second_holder_cannot_replace_fresh_controller_lease(conn, monkeypatch):
    now = _clock(monkeypatch, 100)
    first = kb.acquire_controller_lease(
        conn,
        _lease_request("router-a", operation_id="lease-a"),
    )
    now[0] = 101

    second = kb.acquire_controller_lease(
        conn,
        _lease_request("router-b", operation_id="lease-b"),
    )

    assert first.acquired is True
    assert second.acquired is False
    assert second.holder_token == "router-a"
    assert second.holder_epoch == first.holder_epoch
    assert second.expires_at == first.expires_at


def test_controller_lease_rejects_connection_board_mismatch(conn, monkeypatch):
    _clock(monkeypatch, 100)
    kb.create_board("other-board")
    request = replace(
        _lease_request("router-a", operation_id="wrong-board-lease"),
        board="other-board",
    )

    with pytest.raises(ValueError, match="connected database"):
        kb.acquire_controller_lease(conn, request)

    assert conn.execute(
        "SELECT COUNT(*) FROM kanban_controller_leases"
    ).fetchone()[0] == 0


def test_stale_epoch_cannot_renew_after_controller_lease_takeover(conn, monkeypatch):
    now = _clock(monkeypatch, 100)
    old = kb.acquire_controller_lease(
        conn,
        _lease_request("router-a", operation_id="lease-old"),
    )
    now[0] = 401
    new = kb.acquire_controller_lease(
        conn,
        _lease_request("router-b", operation_id="lease-new"),
    )
    now[0] = 402

    renewed = kb.renew_controller_lease(
        conn,
        kb.LeaseRenewal(
            controller_id="blocked-recovery",
            board="default",
            holder_token="router-a",
            holder_epoch=old.holder_epoch,
            ttl_seconds=300,
            census_signature="d" * 64,
            operation_id="renew-stale",
        ),
    )

    assert old.acquired is True
    assert new.acquired is True
    assert new.holder_epoch > old.holder_epoch
    assert renewed.acquired is False
    assert renewed.holder_token == "router-b"
    assert renewed.holder_epoch == new.holder_epoch


def test_stale_controller_epoch_cannot_publish_claim(conn, monkeypatch):
    now = _clock(monkeypatch, 100)
    task_id = kb.create_task(conn, title="claim target")
    old = kb.acquire_controller_lease(
        conn,
        _lease_request("router-a", operation_id="claim-lease-old"),
    )
    claim = kb.ControllerClaim(
        controller_id="blocked-recovery",
        board="default",
        task_id=task_id,
        generation_signature="e" * 64,
        authority="mutate",
        input_signature="f" * 64,
        lease_epoch=old.holder_epoch,
        expires_at=350,
        operation_id="claim-old",
    )
    assert kb.publish_controller_claim(conn, claim).outcome == "committed"
    now[0] = 401
    new = kb.acquire_controller_lease(
        conn,
        _lease_request("router-b", operation_id="claim-lease-new"),
    )
    now[0] = 402

    stale = kb.publish_controller_claim(
        conn,
        replace(claim, operation_id="claim-stale-after-takeover", expires_at=500),
    )

    assert new.holder_epoch > old.holder_epoch
    assert stale.outcome == "precondition_changed"
    assert not _operation_events(conn, task_id, "claim-stale-after-takeover")


def test_recovery_snapshot_reads_board_state_with_reproducible_signature(
    conn,
    monkeypatch,
):
    _clock(monkeypatch, 200)
    task_id = kb.create_task(conn, title="snapshot target")
    lease = kb.acquire_controller_lease(
        conn,
        _lease_request("router-a", operation_id="snapshot-lease"),
    )
    assert lease.acquired is True

    snapshot = kb.read_recovery_snapshot(conn, now=200)

    assert snapshot.board == "default"
    assert any(task["id"] == task_id for task in snapshot.tasks)
    assert snapshot.revision_signature == kb.snapshot_signature(snapshot)
    assert conn.in_transaction is False


def test_recovery_snapshot_uses_connected_board_not_ambient_selection(
    conn,
    monkeypatch,
):
    kb.create_board("other-board")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    with kb.connect_closing(kb.kanban_db_path(board="other-board")) as other:
        task_id = kb.create_task(other, title="other board task")

        snapshot = kb.read_recovery_snapshot(other, now=200)

    assert snapshot.board == "other-board"
    assert any(task["id"] == task_id for task in snapshot.tasks)


def test_recovery_snapshot_preserves_block_event_after_later_comment(conn):
    task_id = _blocked_task(conn, kind="needs_input")
    block_event = _latest_event(conn, task_id)
    before = kb.read_recovery_snapshot(conn, now=200)

    kb.add_comment(conn, task_id, "operator", "still waiting")
    after = kb.read_recovery_snapshot(conn, now=201)

    latest = {event.task_id: event for event in after.latest_events}
    blocks = {event.task_id: event for event in after.latest_block_events}
    assert latest[task_id].id != block_event.id
    assert blocks[task_id] == block_event
    assert after.revision_signature != before.revision_signature


def test_recovery_snapshot_exposes_complete_scoped_transient_retry_ledger(
    conn,
):
    first_task = _blocked_task(conn, kind="transient")
    first_retry = kb.TransientRetryRequest(
        task_id=first_task,
        expected_terminal_signature=_latest_event_signature(conn, first_task),
        retry_ordinal=1,
        operation_id="snapshot-transient-first-1",
    )
    assert kb.retry_transient_task(conn, first_retry).outcome == "committed"

    second_task = _blocked_task(conn, kind="transient")
    second_retry = kb.TransientRetryRequest(
        task_id=second_task,
        expected_terminal_signature=_latest_event_signature(conn, second_task),
        retry_ordinal=1,
        operation_id="snapshot-transient-second-1",
    )
    assert kb.retry_transient_task(conn, second_retry).outcome == "committed"

    assert kb.block_task(
        conn,
        first_task,
        reason="different human-block cycle",
        kind="needs_input",
    )
    assert kb.unblock_task(conn, first_task)
    assert kb.block_task(
        conn,
        first_task,
        reason="second transient cycle",
        kind="transient",
    )
    assert kb.retry_transient_task(
        conn,
        replace(
            first_retry,
            expected_terminal_signature=_latest_event_signature(conn, first_task),
            retry_ordinal=2,
            operation_id="snapshot-transient-first-2",
        ),
    ).outcome == "committed"
    kb.add_comment(conn, first_task, "operator", "newer unrelated event")

    kb.create_board("snapshot-other-board")
    with kb.connect_closing(kb.kanban_db_path(board="snapshot-other-board")) as other:
        other_task = _blocked_task(other, kind="transient")
        assert kb.retry_transient_task(
            other,
            kb.TransientRetryRequest(
                task_id=other_task,
                expected_terminal_signature=_latest_event_signature(other, other_task),
                retry_ordinal=1,
                operation_id="snapshot-transient-other-1",
            ),
        ).outcome == "committed"

    snapshot = kb.read_recovery_snapshot(conn, now=200)
    expected = tuple(sorted(
        (
            event
            for task_id in (first_task, second_task)
            for event in kb.list_events(conn, task_id)
            if event.kind == "transient_retry_authorized"
        ),
        key=lambda event: (event.task_id, event.id),
    ))

    assert snapshot.transient_retry_events == expected
    assert [
        event.payload["retry_ordinal"]
        for event in snapshot.transient_retry_events
        if event.task_id == first_task
    ] == [1, 2]
    assert all(event.task_id != other_task for event in snapshot.transient_retry_events)
    assert snapshot.revision_signature == kb.snapshot_signature(snapshot)

    tampered_event = replace(
        snapshot.transient_retry_events[0],
        payload={"retry_ordinal": 99},
    )
    tampered = replace(
        snapshot,
        transient_retry_events=(
            tampered_event,
            *snapshot.transient_retry_events[1:],
        ),
    )
    assert kb.snapshot_signature(tampered) != snapshot.revision_signature


def test_recovery_snapshot_exposes_complete_scoped_durable_side_effects(conn):
    first_task = kb.create_task(conn, title="first side-effect task")
    second_task = kb.create_task(conn, title="second side-effect task")
    kb.add_comment(conn, first_task, "worker-a", "durable worker note")
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            second_task,
            "respawn_guarded",
            {"reason": "structured guard"},
        )
        kb._append_event(
            conn,
            first_task,
            "heartbeat",
            {"source": "newer unrelated event"},
        )

    kb.create_board("side-effect-other-board")
    with kb.connect_closing(kb.kanban_db_path(board="side-effect-other-board")) as other:
        other_task = kb.create_task(other, title="other board side-effect task")
        kb.add_comment(other, other_task, "worker-other", "other board note")

    snapshot = kb.read_recovery_snapshot(conn, now=200)
    expected = tuple(sorted(
        (
            event
            for task_id in (first_task, second_task)
            for event in kb.list_events(conn, task_id)
            if event.kind in {"commented", "respawn_guarded"}
        ),
        key=lambda event: (event.task_id, event.id),
    ))

    assert snapshot.durable_side_effect_events == expected
    assert {event.kind for event in snapshot.durable_side_effect_events} == {
        "commented",
        "respawn_guarded",
    }
    assert all(
        event.task_id != other_task
        for event in snapshot.durable_side_effect_events
    )
    assert snapshot.revision_signature == kb.snapshot_signature(snapshot)

    tampered_event = replace(
        snapshot.durable_side_effect_events[0],
        payload={"author": "tampered"},
    )
    tampered = replace(
        snapshot,
        durable_side_effect_events=(
            tampered_event,
            *snapshot.durable_side_effect_events[1:],
        ),
    )
    assert kb.snapshot_signature(tampered) != snapshot.revision_signature


def test_recovery_snapshot_includes_ended_terminal_run_and_current_generation(conn):
    task_id, terminal_event = _failed_review_generation(conn)
    terminal_run = kb.latest_run(conn, task_id)
    before = kb.read_recovery_snapshot(conn, now=200)

    kb.add_comment(conn, task_id, "operator", "failure acknowledged")
    after = kb.read_recovery_snapshot(conn, now=201)

    terminals = {event.task_id: event for event in after.latest_terminal_events}
    ended_runs = {run.task_id: run for run in after.latest_terminal_runs}
    generations = {
        generation.task_id: generation for generation in after.review_generations
    }
    assert terminals[task_id] == terminal_event
    assert ended_runs[task_id].id == terminal_run.id
    assert ended_runs[task_id].ended_at is not None
    assert generations[task_id].parent_task_id == "t_parent"
    assert generations[task_id].generation == "g1"
    assert generations[task_id].current is True
    assert after.revision_signature != before.revision_signature


def test_recovery_snapshot_filters_legacy_cross_board_control_rows(conn):
    task_id = kb.create_task(conn, title="cross-board claim target")
    generation_key = "review-lane:other-board::t_parent:g1"
    conn.execute(
        "INSERT INTO kanban_scoped_idempotency "
        "(namespace, board, tenant, key, task_id, created_at) "
        "VALUES ('review-lane', 'other-board', '', ?, ?, 100)",
        (generation_key, task_id),
    )
    conn.execute(
        "INSERT INTO kanban_controller_leases "
        "(controller_id, board, holder_token, holder_epoch, issued_at, "
        "expires_at, census_signature, operation_id) "
        "VALUES ('router', 'other-board', 'holder', 1, 100, 400, ?, 'lease')",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO kanban_controller_claims "
        "(controller_id, board, task_id, generation_signature, authority, "
        "input_signature, lease_epoch, expires_at, operation_id) "
        "VALUES ('router', 'other-board', ?, '', 'mutate', ?, 1, 400, 'claim')",
        (task_id, "b" * 64),
    )
    conn.commit()

    snapshot = kb.read_recovery_snapshot(conn, now=200)

    assert snapshot.board == "default"
    assert snapshot.review_generations == ()
    assert snapshot.claims == ()
    assert snapshot.leases == ()
    assert any(
        "other-board" in violation for violation in snapshot.invariant_violations
    )


def _reviewed_completion_request(
    conn,
    *,
    reclassified_parent: bool = False,
) -> kb.ReviewedCompletionRequest:
    parent = kb.create_task(conn, title="ship reviewed change", body="all tests pass")
    block_kind = None if reclassified_parent else "review"
    assert kb.block_task(conn, parent, reason="review handoff", kind=block_kind)
    if reclassified_parent:
        result = kb.reclassify_block(
            conn,
            kb.BlockReclassification(
                task_id=parent,
                expected_block_event_signature=_latest_event_signature(conn, parent),
                block_kind="review",
                operation_id="classify-parent-review",
            ),
        )
        assert result.outcome == "committed"
    parent_event = _latest_event(conn, parent)
    reviewer = kb.create_review_generation_task(
        conn,
        _review_generation_request(
            conn,
            key=f"review-lane:default::{parent}:g1",
        ),
    ).task_id
    assert kb.complete_task(conn, reviewer, summary="approved", metadata=_review_metadata())
    reviewer_event = _completed_event(conn, reviewer)
    target = {
        "kind": "git",
        "identity": "example/repository",
        "revision": "a" * 40,
    }
    preconditions = kb.ReviewedCompletionPreconditions(
        tenant="",
        parent_block_event_id=parent_event.id,
        parent_block_event_signature=kb.canonical_event_signature(parent_event),
        review_input_digest=kb.review_input_digest(conn, parent),
        target_signature=kb.canonical_payload_signature(target),
        reviewer_task_id=reviewer,
        reviewer_completion_event_id=reviewer_event.id,
        reviewer_completion_event_signature=kb.canonical_event_signature(reviewer_event),
        reviewer_approval_signature=kb.canonical_payload_signature(
            reviewer_event.payload["review"]
        ),
        operation_id="reviewed-completion-1",
    )
    request = kb.ReviewedCompletionRequest(
        task_id=parent,
        result="shipped",
        summary="reviewed and verified",
        metadata={"tests_run": 12},
        target_verification=target,
        preconditions=preconditions,
        judge_input_signature="",
        post_judge_verification_signature="c" * 64,
    )
    judge_input = kb.build_reviewed_completion_judge_input(conn, request)
    assert judge_input is not None
    return replace(request, judge_input_signature=judge_input.signature())


def test_review_generation_for_one_parent_cannot_authorize_another(conn):
    first = _reviewed_completion_request(conn)
    second_parent = kb.create_task(conn, title="different reviewed change")
    assert kb.block_task(
        conn,
        second_parent,
        reason="review handoff",
        kind="review",
    )
    second_event = _latest_event(conn, second_parent)
    forged = replace(
        first,
        task_id=second_parent,
        preconditions=replace(
            first.preconditions,
            parent_block_event_id=second_event.id,
            parent_block_event_signature=kb.canonical_event_signature(second_event),
            review_input_digest=kb.review_input_digest(conn, second_parent),
            operation_id="cross-parent-review-completion",
        ),
    )

    assert kb.build_reviewed_completion_judge_input(conn, forged) is None
    result = kb.complete_reviewed_task_guarded(
        conn,
        replace(forged, judge_input_signature="0" * 64),
    )

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, second_parent).status == "blocked"


def test_review_generation_create_rejects_parent_edit_race(conn):
    parent, request = _guarded_review_generation_request(conn)
    assert kb.assign_task(conn, parent, "different-engineer")

    result = kb.create_review_generation_task(conn, request)

    assert result.outcome == "precondition_changed"
    assert result.created is False
    assert result.task_id == ""
    assert kb.lookup_review_generation(
        conn,
        "default",
        "",
        request.key,
    ) is None


def test_review_generation_permanent_replay_rechecks_parent_lifecycle(conn):
    parent, request = _guarded_review_generation_request(conn)
    first = kb.create_review_generation_task(conn, request)
    assert first.outcome == "committed"
    assert kb.unblock_task(conn, parent)

    replay = kb.create_review_generation_task(conn, request)

    assert replay.outcome == "precondition_changed"
    assert replay.created is False
    assert replay.task_id == ""
    assert kb.lookup_review_generation(conn, "default", "", request.key).task_id == first.task_id


@pytest.mark.parametrize("mutation", [kb.requeue_review_lane, kb.archive_review_lane])
def test_review_lane_mutation_rejects_parent_edit_race(conn, mutation):
    parent, task_id, request = _guarded_review_lane_request(
        conn,
        mutation=mutation,
        operation_id=f"parent-race-{mutation.__name__}",
    )
    before = kb.list_events(conn, task_id)
    assert kb.assign_task(conn, parent, "different-engineer")

    result = mutation(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.list_events(conn, task_id) == before


@pytest.mark.parametrize("mutation", [kb.requeue_review_lane, kb.archive_review_lane])
def test_review_lane_replay_rechecks_parent_lifecycle(conn, mutation):
    parent, task_id, request = _guarded_review_lane_request(
        conn,
        mutation=mutation,
        operation_id=f"parent-replay-{mutation.__name__}",
    )
    first = mutation(conn, request)
    assert first.outcome == "committed"
    assert kb.unblock_task(conn, parent)

    replay = mutation(conn, request)

    assert replay.outcome == "precondition_changed"
    assert replay.event_id is None
    assert len(_operation_events(conn, task_id, request.operation_id)) == 1


def test_review_generation_must_remain_unparented_after_judging(conn):
    request = _reviewed_completion_request(conn)
    unrelated = kb.create_task(conn, title="unrelated completed parent")
    assert kb.complete_task(conn, unrelated)
    kb.link_tasks(conn, unrelated, request.preconditions.reviewer_task_id)

    result = kb.complete_reviewed_task_guarded(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, request.task_id).status == "blocked"


def test_older_review_generation_cannot_complete_after_successor_exists(conn):
    request = _reviewed_completion_request(conn)
    successor = kb.create_review_generation_task(
        conn,
        _review_generation_request(
            conn,
            key=f"review-lane:default::{request.task_id}:g2",
        ),
    )
    assert successor.created is True

    assert kb.build_reviewed_completion_judge_input(conn, request) is None
    result = kb.complete_reviewed_task_guarded(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, request.task_id).status == "blocked"


def test_guarded_completion_replay_repairs_child_readiness(conn, monkeypatch):
    request = _reviewed_completion_request(conn)
    child = kb.create_task(conn, title="dependent child", parents=[request.task_id])
    real_recompute = kb.recompute_ready
    cleanup_transactions: list[bool] = []
    monkeypatch.setattr(
        kb,
        "_cleanup_workspace",
        lambda connection, _task_id: cleanup_transactions.append(
            connection.in_transaction
        ),
    )
    monkeypatch.setattr(
        kb,
        "recompute_ready",
        lambda connection: (_ for _ in ()).throw(RuntimeError("post-commit crash")),
    )

    with pytest.raises(RuntimeError, match="post-commit crash"):
        kb.complete_reviewed_task_guarded(conn, request)

    assert kb.get_task(conn, request.task_id).status == "done"
    assert kb.get_task(conn, child).status == "todo"
    monkeypatch.setattr(kb, "recompute_ready", real_recompute)

    replay = kb.complete_reviewed_task_guarded(conn, request)

    assert replay.outcome == "committed"
    assert kb.get_task(conn, child).status == "ready"
    assert cleanup_transactions == [False]


def test_guarded_reviewed_completion_accepts_exact_signed_inputs(conn):
    request = _reviewed_completion_request(conn)

    first = kb.complete_reviewed_task_guarded(conn, request)
    replay = kb.complete_reviewed_task_guarded(conn, request)

    assert first == replay
    assert first.outcome == "committed"


def test_guarded_reviewed_completion_rejects_a_tenant_change(conn):
    request = _reviewed_completion_request(conn)

    result = kb.complete_reviewed_task_guarded(
        conn,
        replace(request, preconditions=replace(request.preconditions, tenant="other")),
    )

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, request.task_id).status == "blocked"


def test_reviewed_completion_preverification_replay_recovers_an_exact_commit(conn):
    request = _reviewed_completion_request(conn)
    assert kb.complete_reviewed_task_guarded(conn, request).outcome == "committed"

    replay = kb.replay_reviewed_completion_before_target_verification(
        conn,
        replace(request, post_judge_verification_signature="d" * 64),
    )

    assert replay is not None
    assert replay.outcome == "committed"
    assert replay.task_id == request.task_id
    assert kb.get_task(conn, request.task_id).status == "done"
    event = _operation_events(conn, request.task_id, request.preconditions.operation_id)[0]
    assert event.kind == "completed"
    assert event.payload["judge_input_signature"] == request.judge_input_signature


def test_guarded_reviewed_completion_fires_completion_hook_once(conn, monkeypatch):
    request = _reviewed_completion_request(conn)
    fired: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: fired.append((event, task_id, fields)),
    )

    first = kb.complete_reviewed_task_guarded(conn, request)
    replay = kb.complete_reviewed_task_guarded(conn, request)

    assert first == replay
    assert [(event, task_id) for event, task_id, _fields in fired] == [
        ("kanban_task_completed", request.task_id)
    ]
    assert fired[0][2]["summary"] == request.summary


def test_guarded_reviewed_completion_accepts_reclassified_review_block(conn):
    request = _reviewed_completion_request(conn, reclassified_parent=True)

    result = kb.complete_reviewed_task_guarded(conn, request)

    assert result.outcome == "committed"
    assert kb.get_task(conn, request.task_id).status == "done"


def test_guarded_reviewed_completion_rechecks_state_after_judging(conn):
    request = _reviewed_completion_request(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            ("acceptance changed after judging", request.task_id),
        )
    events_before = kb.list_events(conn, request.task_id)

    result = kb.complete_reviewed_task_guarded(conn, request)

    assert result.outcome == "precondition_changed"
    assert kb.get_task(conn, request.task_id).status == "blocked"
    assert kb.list_events(conn, request.task_id) == events_before


def _test_payload_signature(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_recovery_pair(conn) -> tuple[str, str, kb.RecoveryManifest]:
    reviewer = kb.create_task(conn, title="legacy approved review", assignee="reviewer")
    child = kb.create_task(conn, title="legacy child awaiting review")
    kb.link_tasks(conn, reviewer, child)
    target = {
        "kind": "git",
        "identity": "example/repository",
        "revision": "b" * 40,
    }
    target_evidence = ({**target, "signature": _test_payload_signature(target)},)
    unsigned_payload = {
        "schema_version": 1,
        "reviewer_task_id": reviewer,
        "reviewer_status": kb.get_task(conn, reviewer).status,
        "reviewer_event_signature": _latest_event_signature(conn, reviewer),
        "child_task_id": child,
        "child_status": "todo",
        "child_event_signature": _latest_event_signature(conn, child),
        "expected_parent_ids": [reviewer],
        "target_evidence": [dict(item) for item in target_evidence],
        "operation_id": "legacy-recovery-manifest-1",
    }
    manifest = kb.RecoveryManifest(
        schema_version=1,
        reviewer_task_id=reviewer,
        reviewer_status=unsigned_payload["reviewer_status"],
        reviewer_event_signature=unsigned_payload["reviewer_event_signature"],
        child_task_id=child,
        child_status="todo",
        child_event_signature=unsigned_payload["child_event_signature"],
        expected_parent_ids=(reviewer,),
        target_evidence=target_evidence,
        approval_signature=_test_payload_signature(unsigned_payload),
        operation_id="legacy-recovery-manifest-1",
    )
    return reviewer, child, manifest


def _legacy_pair_snapshot(conn, reviewer: str, child: str) -> dict[str, object]:
    return {
        "reviewer": kb.get_task(conn, reviewer),
        "child": kb.get_task(conn, child),
        "reviewer_events": kb.list_events(conn, reviewer),
        "child_events": kb.list_events(conn, child),
        "reviewer_runs": kb.list_runs(conn, reviewer),
        "child_runs": kb.list_runs(conn, child),
        "parents": kb.parent_ids(conn, child),
    }


def test_manifest_finalizes_reviewer_and_parks_child_atomically(conn):
    reviewer, child, manifest = _legacy_recovery_pair(conn)

    first = kb.apply_recovery_manifest(conn, manifest)
    replay = kb.apply_recovery_manifest(conn, manifest)

    assert first == replay
    assert first.outcome == "committed"
    assert kb.get_task(conn, reviewer).status == "done"
    parked = kb.get_task(conn, child)
    assert (parked.status, parked.block_kind) == ("blocked", "needs_input")
    reviewer_event = _completed_event(conn, reviewer)
    evidence = kb.get_terminal_review_evidence(conn, reviewer, reviewer_event.id)
    assert evidence is not None and evidence.approved
    assert (evidence.target_kind, evidence.target_identity, evidence.target_revision) == (
        "git",
        "example/repository",
        "b" * 40,
    )
    assert len(_operation_events(conn, reviewer, manifest.operation_id)) == 1
    assert len(_operation_events(conn, child, manifest.operation_id)) == 1


@pytest.mark.parametrize(
    "boundary",
    ["reviewer", "child", "graph", "target", "reviewer_event", "child_event"],
)
def test_manifest_stale_precondition_changes_nothing(conn, boundary):
    reviewer, child, manifest = _legacy_recovery_pair(conn)
    if boundary == "reviewer":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (reviewer,))
    elif boundary == "child":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    elif boundary == "graph":
        other = kb.create_task(conn, title="unexpected parent")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (other, child),
            )
    elif boundary == "target":
        changed = dict(manifest.target_evidence[0])
        changed["revision"] = "c" * 40
        manifest = replace(manifest, target_evidence=(changed,))
    elif boundary == "reviewer_event":
        kb.add_comment(conn, reviewer, "operator", "reviewer changed")
    else:
        kb.add_comment(conn, child, "operator", "child changed")
    before = _legacy_pair_snapshot(conn, reviewer, child)

    result = kb.apply_recovery_manifest(conn, manifest)

    assert result.outcome == "precondition_changed"
    assert _legacy_pair_snapshot(conn, reviewer, child) == before


def test_manifest_cannot_finalize_an_actively_running_reviewer(conn):
    reviewer, child, manifest = _legacy_recovery_pair(conn)
    assert kb.claim_task(conn, reviewer, claimer="reviewer") is not None
    candidate = replace(
        manifest,
        reviewer_status="running",
        reviewer_event_signature=_latest_event_signature(conn, reviewer),
        approval_signature="0" * 64,
    )
    candidate = replace(
        candidate,
        approval_signature=kb.recovery_manifest_signature(candidate),
    )
    before = _legacy_pair_snapshot(conn, reviewer, child)

    result = kb.apply_recovery_manifest(conn, candidate)

    assert result.outcome == "precondition_changed"
    assert _legacy_pair_snapshot(conn, reviewer, child) == before


def test_manifest_exception_rolls_back_both_tasks(conn, monkeypatch):
    reviewer, child, manifest = _legacy_recovery_pair(conn)
    before = _legacy_pair_snapshot(conn, reviewer, child)
    real_append = kb._append_event

    def fail_on_child(connection, task_id, kind, payload=None, *, run_id=None):
        if task_id == child:
            raise RuntimeError("simulated child-event failure")
        return real_append(
            connection,
            task_id,
            kind,
            payload,
            run_id=run_id,
        )

    monkeypatch.setattr(kb, "_append_event", fail_on_child)

    with pytest.raises(RuntimeError, match="simulated child-event failure"):
        kb.apply_recovery_manifest(conn, manifest)

    assert _legacy_pair_snapshot(conn, reviewer, child) == before


def _write_recovery_manifest(path: Path, manifest: kb.RecoveryManifest) -> None:
    payload = kb.recovery_manifest_unsigned_payload(manifest)
    payload["approval_signature"] = manifest.approval_signature
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_apply_recovery_manifest_cli_emits_structured_result(conn, tmp_path):
    reviewer, child, manifest = _legacy_recovery_pair(conn)
    path = tmp_path / "recovery-manifest.json"
    _write_recovery_manifest(path, manifest)

    output = kc.run_slash(
        f"apply-recovery-manifest '{path}' "
        f"--approval-signature {manifest.approval_signature} --json"
    )

    payload = json.loads(output)
    assert payload["outcome"] == "committed"
    assert payload["task_id"] == child
    assert kb.get_task(conn, reviewer).status == "done"
    assert kb.get_task(conn, child).status == "blocked"


def test_apply_recovery_manifest_cli_rejects_unapproved_file(conn, tmp_path):
    reviewer, child, manifest = _legacy_recovery_pair(conn)
    path = tmp_path / "recovery-manifest.json"
    _write_recovery_manifest(path, manifest)
    before = _legacy_pair_snapshot(conn, reviewer, child)

    output = kc.run_slash(
        f"apply-recovery-manifest '{path}' "
        f"--approval-signature {'0' * 64} --json"
    )

    payload = json.loads(output)
    assert payload["outcome"] == "precondition_changed"
    assert _legacy_pair_snapshot(conn, reviewer, child) == before


def test_apply_recovery_manifest_cli_rejects_worker_context(
    conn,
    tmp_path,
    monkeypatch,
):
    reviewer, child, manifest = _legacy_recovery_pair(conn)
    path = tmp_path / "recovery-manifest.json"
    _write_recovery_manifest(path, manifest)
    before = _legacy_pair_snapshot(conn, reviewer, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)

    output = kc.run_slash(
        f"apply-recovery-manifest '{path}' "
        f"--approval-signature {manifest.approval_signature} --json"
    )

    assert "cannot run in a worker task" in output
    assert _legacy_pair_snapshot(conn, reviewer, child) == before
