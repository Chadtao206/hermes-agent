"""Cross-backend conformance for the triage composite writes (specify/decompose).

The composite methods live on PostgresKanbanStore only; the sqlite equivalents
are kanban_db.specify_triage_task / decompose_triage_task. These tests drive each
backend the backend-appropriate way and assert identical resulting state + event
shapes, so PG parity with sqlite is pinned.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban.store_postgres import PostgresKanbanStore


def _specify(store, task_id, **kw):
    """Specify on whichever backend `store` is."""
    if isinstance(store, PostgresKanbanStore):
        return store.specify_triage_task(task_id, **kw)
    with kb.connect_closing() as conn:
        return kb.specify_triage_task(conn, task_id, **kw)


def _kinds(store, task_id):
    return [e.kind for e in store.list_events(task_id)]


def test_specify_promotes_triage_to_todo_with_changes(store):
    tid = store.create_task(title="rough idea", triage=True)
    assert store.get_task(tid).status == "triage"
    ok = _specify(store, tid, title="Tightened title", body="**Goal** ...",
                  author="alice")
    assert ok is True
    t = store.get_task(tid)
    # recompute_ready() runs after the txn, so a parentless task lands in 'ready'
    assert t.status in ("todo", "ready")
    assert t.title == "Tightened title"
    assert t.body == "**Goal** ..."
    kinds = _kinds(store, tid)
    assert "specified" in kinds
    bodies = [c.body for c in store.list_comments(tid)]
    assert any("Specified" in b for b in bodies)
    # exact payload + comment parity (highest-risk drift dimension)
    specified = [e for e in store.list_events(tid) if e.kind == "specified"]
    assert len(specified) == 1
    assert specified[0].payload == {"changed_fields": ["title", "body"]}
    assert any(c.body == "Specified — updated title, body and promoted to todo."
               for c in store.list_comments(tid))


def test_specify_status_only_no_comment_no_changed_fields(store):
    tid = store.create_task(title="keep title", triage=True)
    ok = _specify(store, tid, author="alice")  # nothing changes but status
    assert ok is True
    # recompute_ready() runs after the txn, so a parentless task lands in 'ready'
    assert store.get_task(tid).status in ("todo", "ready")
    assert store.list_comments(tid) == []  # no audit comment for status-only


def test_specify_returns_false_when_not_in_triage(store):
    tid = store.create_task(title="already live")  # default status ready
    assert _specify(store, tid, title="x") is False
    assert store.get_task(tid).status != "triage"


def test_specify_blank_title_raises(store):
    tid = store.create_task(title="x", triage=True)
    with pytest.raises(ValueError):
        _specify(store, tid, title="   ")


def test_specify_with_open_parent_lands_in_todo(store):
    parent = store.create_task(title="open parent")  # ready, not done
    tid = store.create_task(title="gated idea", triage=True, parents=[parent])
    ok = _specify(store, tid, title="Specced", author="alice")
    assert ok is True
    # parent is not done -> recompute_ready must NOT promote past todo
    assert store.get_task(tid).status == "todo"
