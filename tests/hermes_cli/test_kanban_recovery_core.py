"""Behavioral tests for blocked-recovery primitives in the Kanban core."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
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
