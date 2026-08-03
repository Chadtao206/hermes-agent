"""Behavioral tests for blocked-recovery primitives in the Kanban core."""

from __future__ import annotations

import concurrent.futures
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
