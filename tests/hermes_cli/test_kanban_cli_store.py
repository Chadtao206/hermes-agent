"""Tests that CLI command handlers route through kanban_store instead of
direct connect_closing() calls.

Modelled on tests/hermes_cli/test_kanban_cli.py for dispatch/arg shape.
"""
from __future__ import annotations

import argparse
import sys
from io import StringIO

import pytest


# ---------------------------------------------------------------------------
# Shared fake store
# ---------------------------------------------------------------------------

class FakeStore:
    """Minimal stand-in for SqliteKanbanStore.  Each test customises it."""

    board = None

    def __init__(self):
        self.closed = False
        self.calls: list[tuple[str, dict]] = []
        self._tasks: dict[str, object] = {}
        self._runs: list = []
        self._comments: list = []
        self._events: list = []

    # --- task ops ----------------------------------------------------------
    def create_task(self, **kw) -> str:
        self.calls.append(("create_task", kw))
        return "t_fake"

    def get_task(self, task_id: str):
        self.calls.append(("get_task", {"task_id": task_id}))

        class _FakeTask:
            id = task_id
            status = "ready"
            assignee = "engineer"
            title = "demo"
            body = None
            priority = 0
            tenant = None
            workspace_kind = "scratch"
            workspace_path = None
            branch_name = None
            created_by = "user"
            created_at = 0
            started_at = None
            completed_at = None
            result = None
            skills: list = []
            max_retries = None
            session_id = None
            workflow_template_id = None
            current_step_key = None
            model_override = None

        return _FakeTask()

    def list_tasks(self, **kw):
        self.calls.append(("list_tasks", kw))
        return []

    def complete_task(self, task_id: str, **kw) -> bool:
        self.calls.append(("complete_task", {"task_id": task_id, **kw}))
        return True

    def block_task(self, task_id: str, **kw) -> bool:
        self.calls.append(("block_task", {"task_id": task_id, **kw}))
        return True

    def unblock_task(self, task_id: str) -> bool:
        self.calls.append(("unblock_task", {"task_id": task_id}))
        return True

    def schedule_task(self, task_id: str, **kw) -> bool:
        self.calls.append(("schedule_task", {"task_id": task_id, **kw}))
        return True

    def archive_task(self, task_id: str) -> bool:
        self.calls.append(("archive_task", {"task_id": task_id}))
        return True

    def assign_task(self, task_id: str, profile) -> bool:
        self.calls.append(("assign_task", {"task_id": task_id, "profile": profile}))
        return True

    def reassign_task(self, task_id: str, profile, **kw) -> bool:
        self.calls.append(("reassign_task", {"task_id": task_id, "profile": profile, **kw}))
        return True

    def reclaim_task(self, task_id: str, **kw) -> bool:
        self.calls.append(("reclaim_task", {"task_id": task_id, **kw}))
        return True

    def edit_task_fields(self, task_id: str, **kw) -> bool:
        self.calls.append(("edit_task_fields", {"task_id": task_id, **kw}))
        return True

    def edit_completed_task_result(self, task_id: str, **kw) -> bool:
        self.calls.append(("edit_completed_task_result", {"task_id": task_id, **kw}))
        return True

    def promote_task(self, task_id: str, **kw):
        self.calls.append(("promote_task", {"task_id": task_id, **kw}))
        return (True, None)

    def link_tasks(self, parent_id: str, child_id: str, **kw) -> None:
        self.calls.append(("link_tasks", {"parent_id": parent_id, "child_id": child_id, **kw}))

    def unlink_tasks(self, parent_id: str, child_id: str, **kw) -> bool:
        self.calls.append(("unlink_tasks", {"parent_id": parent_id, "child_id": child_id, **kw}))
        return True

    def parent_ids(self, task_id: str) -> list:
        self.calls.append(("parent_ids", {"task_id": task_id}))
        return []

    def child_ids(self, task_id: str) -> list:
        self.calls.append(("child_ids", {"task_id": task_id}))
        return []

    def add_comment(self, task_id: str, *, author: str, body: str) -> int:
        self.calls.append(("add_comment", {"task_id": task_id, "author": author, "body": body}))
        return 1

    def list_comments(self, task_id: str) -> list:
        self.calls.append(("list_comments", {"task_id": task_id}))
        return []

    def list_events(self, task_id: str, **kw) -> list:
        self.calls.append(("list_events", {"task_id": task_id, **kw}))
        return []

    def gc_events(self, **kw) -> int:
        self.calls.append(("gc_events", kw))
        return 0

    def list_runs(self, task_id: str) -> list:
        self.calls.append(("list_runs", {"task_id": task_id}))
        return []

    def get_run(self, run_id: int):
        self.calls.append(("get_run", {"run_id": run_id}))
        return None

    def latest_run(self, task_id: str):
        self.calls.append(("latest_run", {"task_id": task_id}))
        return None

    def latest_summary(self, task_id: str):
        self.calls.append(("latest_summary", {"task_id": task_id}))
        return None

    def latest_summaries(self, task_ids) -> dict:
        self.calls.append(("latest_summaries", {"task_ids": task_ids}))
        return {}

    def add_notify_sub(self, **kw) -> int:
        self.calls.append(("add_notify_sub", kw))
        return 1

    def remove_notify_sub(self, **kw) -> bool:
        self.calls.append(("remove_notify_sub", kw))
        return True

    def list_notify_subs(self, task_id=None) -> list:
        self.calls.append(("list_notify_subs", {"task_id": task_id}))
        return []

    def claim_unseen_events_for_sub(self, **kw):
        self.calls.append(("claim_unseen_events_for_sub", kw))
        return ([], 0)

    def add_profile_event_sub(self, **kw):
        self.calls.append(("add_profile_event_sub", kw))

    def remove_profile_event_sub(self, **kw) -> bool:
        self.calls.append(("remove_profile_event_sub", kw))
        return True

    def list_profile_event_subs(self, **kw) -> list:
        self.calls.append(("list_profile_event_subs", kw))
        return []

    def record_notifier_heartbeat(self, **kw):
        self.calls.append(("record_notifier_heartbeat", kw))

    def list_notifier_heartbeats(self, **kw) -> list:
        self.calls.append(("list_notifier_heartbeats", kw))
        return []

    def heartbeat_worker(self, **kw) -> bool:
        self.calls.append(("heartbeat_worker", kw))
        return True

    def recompute_ready(self) -> int:
        self.calls.append(("recompute_ready", {}))
        return 0

    def has_spawnable_ready(self) -> bool:
        self.calls.append(("has_spawnable_ready", {}))
        return False

    def board_stats(self) -> dict:
        self.calls.append(("board_stats", {}))
        return {"by_status": {}, "by_assignee": {}, "oldest_ready_age_seconds": None}

    def known_assignees(self) -> list:
        self.calls.append(("known_assignees", {}))
        return []

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Helper: build a minimal create Namespace that _cmd_create accepts
# ---------------------------------------------------------------------------

def _create_ns(**overrides):
    defaults = dict(
        title="demo",
        body=None,
        assignee="engineer",
        created_by=None,
        workspace="scratch",
        branch=None,
        tenant=None,
        priority=0,
        parent=[],
        triage=False,
        idempotency_key=None,
        max_runtime=None,
        skills=[],
        max_retries=None,
        initial_status="running",
        json=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Step 1 (TDD): _cmd_create routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_create_uses_store(monkeypatch):
    """_cmd_create must call kanban_store() and route create_task through it."""
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr(
        "hermes_cli.kanban.kanban_store",
        lambda board=None: store,
        raising=False,
    )

    ns = _create_ns()
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_create(ns)

    assert rc == 0
    # The store received a create_task call
    create_calls = [c for c in store.calls if c[0] == "create_task"]
    assert create_calls, "expected create_task to be called on the store"
    kw = create_calls[0][1]
    assert kw["title"] == "demo"
    # And close() was called
    assert store.closed, "store.close() must be called"


# ---------------------------------------------------------------------------
# _cmd_complete routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_complete_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_ids=["t_abc"], result=None, summary=None, metadata=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_complete(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "complete_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_block routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_block_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", ids=[], reason=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_block(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "block_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_unblock routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_unblock_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_ids=["t_abc"], reason=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_unblock(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "unblock_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_schedule routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_schedule_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", ids=[], reason=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_schedule(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "schedule_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_assign routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_assign_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", profile="alice", json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_assign(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "assign_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_reassign routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_reassign_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", profile="bob", reclaim=False, reason=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_reassign(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "reassign_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_reclaim routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_reclaim_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", reason=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_reclaim(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "reclaim_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_comment routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_comment_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", text=["hello world"], author=None, max_len=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_comment(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "add_comment" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_link routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_link_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(parent_id="t_a", child_id="t_b", relation_type="dependency", json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_link(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "link_tasks" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_unlink routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_unlink_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(parent_id="t_a", child_id="t_b", relation_type="dependency", json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_unlink(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "unlink_tasks" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_archive routes through kanban_store (archive path)
# ---------------------------------------------------------------------------

def test_cli_archive_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_ids=["t_abc"], purge_ids=[], json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_archive(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "archive_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_edit routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_edit_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", result=None, summary=None, metadata=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_edit(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "edit_completed_task_result" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# _cmd_list routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_list_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(
        assignee=None, mine=False, status=None, tenant=None, session=None,
        archived=False, sort=None, workflow_template_id=None,
        current_step_key=None, json=False,
    )
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_list(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "recompute_ready" in ops
    assert "list_tasks" in ops
    assert store.closed


# Note: `_cmd_show` is intentionally NOT routed through the store. It is a
# multi-read aggregation (task + comments + events + runs + parents/children +
# rollup relations + worker_context); routing each read through the store opens
# a fresh snapshot connection per call (N full DB-file copies under single-writer)
# and loses cross-read consistency. Like the dashboard's task-detail endpoint, it
# keeps all reads on ONE shared snapshot connection. Behavioral coverage for the
# `show` command lives in tests/hermes_cli/test_kanban_cli.py
# (test_run_slash_show_includes_comments).


# ---------------------------------------------------------------------------
# _cmd_runs routes through kanban_store
# ---------------------------------------------------------------------------

def test_cli_runs_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", state_type=None, state_name=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_runs(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "list_runs" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# Optional secondary: _cmd_stats
# ---------------------------------------------------------------------------

def test_cli_stats_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_stats(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "board_stats" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# Optional secondary: _cmd_assignees
# ---------------------------------------------------------------------------

def test_cli_assignees_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_assignees(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "known_assignees" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# Optional secondary: _cmd_heartbeat
# ---------------------------------------------------------------------------

def test_cli_heartbeat_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(task_id="t_abc", note=None, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_heartbeat(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "heartbeat_worker" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# Optional secondary: _cmd_promote
# ---------------------------------------------------------------------------

def test_cli_promote_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(
        task_id="t_abc", ids=[], reason=None, force=False, dry_run=False, json=False
    )
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli._cmd_promote(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "promote_task" in ops
    assert store.closed


# ---------------------------------------------------------------------------
# Optional secondary: _cmd_gc (gc_events path)
# ---------------------------------------------------------------------------

def test_cli_gc_uses_store(monkeypatch):
    import hermes_cli.kanban as cli

    store = FakeStore()
    monkeypatch.setattr("hermes_cli.kanban.kanban_store", lambda board=None: store, raising=False)

    ns = argparse.Namespace(event_retention_days=30, log_retention_days=30, json=False)
    out = StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    # Mock the raw-conn part of gc that lists archived workspaces
    monkeypatch.setattr(
        "hermes_cli.kanban_db.connect_closing",
        lambda: _DummyCm([]),
        raising=True,
    )
    monkeypatch.setattr("hermes_cli.kanban_db.workspaces_root", lambda: __import__("pathlib").Path("/tmp/noexist"), raising=True)
    monkeypatch.setattr("hermes_cli.kanban_db.gc_worker_logs", lambda **kw: 0, raising=True)

    rc = cli._cmd_gc(ns)

    assert rc == 0
    ops = [c[0] for c in store.calls]
    assert "gc_events" in ops
    assert store.closed


class _DummyCm:
    """Context-manager that returns a fake conn with fetchall=list."""
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        class _Conn:
            def execute(self_, sql):
                class _Res:
                    def fetchall(self__):
                        return []
                return _Res()
        return _Conn()

    def __exit__(self, *a):
        pass
