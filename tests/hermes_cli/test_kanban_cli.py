"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


def test_run_slash_create_worktree_path_and_branch(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    target_arg = target.as_posix()
    out = kc.run_slash(
        f"create 'ship worktree' --workspace worktree:{target_arg} --branch wt/t6-wire"
    )
    assert "Created" in out

    with kbc.connect() as conn:
        tasks = kb.list_tasks(conn)
    task = tasks[0]
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == target_arg
    assert task.branch_name == "wt/t6-wire"


def test_run_slash_rejects_branch_without_worktree(kanban_home):
    out = kc.run_slash("create 'bad branch' --workspace scratch --branch wt/bad")
    assert "--branch is only valid with --workspace worktree" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list




def _lease_cli(
    board: str,
    action: str,
    *,
    token: str = "router-token",
    operation_id: str = "lease-operation-1",
    holder_epoch: int | None = None,
) -> dict[str, object]:
    epoch = "" if holder_epoch is None else f" --holder-epoch {holder_epoch}"
    return json.loads(kc.run_slash(
        f"--board {board} controller-lease {action} "
        "--controller-id blocked-recovery "
        f"--holder-token {token}{epoch} --ttl-seconds 300 "
        f"--census-signature {'c' * 64} --operation-id {operation_id} --json"
    ))










def _planned_controller_mutation_cli(kanban_home):
    with kbc.connect_closing(board="default") as connection:
        task_id = kb.create_task(
            connection, title="guarded lifecycle cli", tenant="alpha",
        )
        assert kb.block_task(connection, task_id, reason="awaiting adjudication")
        snapshot = kb.read_recovery_snapshot(connection, now=1)
        input_signature = kb.recovery_task_input_signature(snapshot, task_id)
        generation_signature = kb.recovery_task_generation_signature(
            snapshot, task_id,
        )
    lease = _lease_cli("default", "acquire", token="claim-cli-token")
    claim_operation_id = "claim-cli-mutate"
    claim_command = (
        f"--board default controller-claim publish {task_id} "
        "--expected-tenant alpha --controller-id blocked-recovery "
        f"--generation-signature '{generation_signature}' --authority mutate "
        f"--input-signature {input_signature} "
        f"--lease-epoch {lease['holder_epoch']} "
        f"--expires-at {lease['expires_at']} "
        f"--operation-id {claim_operation_id} --json"
    )
    assert json.loads(kc.run_slash(claim_command))["outcome"] == "committed"
    with kbc.connect_closing(board="default") as connection:
        authority_signature = kb.recovery_task_authority_signature(
            kb.read_recovery_snapshot(connection, now=lease["issued_at"]), task_id,
        )
    mutate_command = (
        f"--board default controller-claim mutate {task_id} "
        "--expected-tenant alpha --controller-id blocked-recovery "
        f"--generation-signature '{generation_signature}' "
        f"--input-signature {input_signature} "
        f"--authority-signature {authority_signature} "
        f"--claim-operation-id {claim_operation_id} "
        f"--claim-expires-at {lease['expires_at']} "
        "--lease-holder-token claim-cli-token "
        f"--lease-epoch {lease['holder_epoch']} "
        f"--lease-issued-at {lease['issued_at']} "
        f"--lease-expires-at {lease['expires_at']} "
        f"--lease-census-signature {'c' * 64} "
        f"--lease-operation-id {lease['operation_id']} "
        "--verb unblock --operation-id guarded-cli-unblock --json"
    )

    return task_id, lease, generation_signature, input_signature, mutate_command














def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show




def test_run_slash_comment_max_len_trims_long_body(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} '{'x' * 30}' --max-len 20")
    show = kc.run_slash(f"show {tid}")
    assert "trimmed to 20 chars by --max-len" in show
    assert "x" * 30 not in show


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")




def _managed_review_cli_state(task_id):
    with kbc.connect_closing() as conn:
        task = conn.execute(
            "SELECT status, current_run_id, claim_lock, claim_expires, worker_pid, "
            "block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return (
            tuple(task),
            conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0],
        )








def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def test_run_slash_session_filter(kanban_home):
    """`hermes kanban list --session <id>` filters by the originating
    chat session id stamped on tasks created from inside an ACP loop."""
    from hermes_cli import kanban_db as kb
    with kbc.connect() as conn:
        kb.create_task(
            conn, title="from sess-1 a", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-1 b", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-2", assignee="alice", session_id="sess-2"
        )
        kb.create_task(conn, title="cli only", assignee="alice")
    out_1 = kc.run_slash("list --session sess-1")
    out_2 = kc.run_slash("list --session sess-2")
    assert "from sess-1 a" in out_1
    assert "from sess-1 b" in out_1
    assert "from sess-2" not in out_1
    assert "cli only" not in out_1
    assert "from sess-2" in out_2
    assert "from sess-1 a" not in out_2


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_run_slash_stats_text_includes_review_lane(kanban_home):
    """``stats`` text output must surface the ``review`` lane: board_stats() counts
    every non-archived status, so the ``By status:`` block must list ``review`` too
    or its count silently disappears while ``--json`` still shows it."""
    import re

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs review", assignee="alice")
        assert kb.request_review(conn, tid, summary="s", force=True)

    assert re.search(r"review\s+1", kc.run_slash("stats"))
    assert json.loads(kc.run_slash("stats --json"))["by_status"].get("review") == 1


def test_kanban_show_json_includes_runtime_limit(kanban_home):
    with kbc.connect() as conn:
        bounded_id = kb.create_task(
            conn, title="bounded task", max_runtime_seconds=2700
        )
        uncapped_id = kb.create_task(conn, title="uncapped task")

    bounded = json.loads(kc.run_slash(f"show {bounded_id} --json"))
    uncapped = json.loads(kc.run_slash(f"show {uncapped_id} --json"))

    assert bounded["task"]["max_runtime_seconds"] == 2700
    assert uncapped["task"]["max_runtime_seconds"] is None


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kbc.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert "child task" in output
    assert parent_id in output
    assert "Cannot operate on a closed database" not in output


def test_kanban_show_text_renders_legacy_iso_completed_at(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="legacy stamp")
        kb.complete_task(conn, task_id, result="ok", force=True)
        conn.execute("UPDATE tasks SET completed_at = '2026-09-30T14:22:11' WHERE id = ?", (task_id,))
        conn.commit()

    output = kc.run_slash(f"show {task_id}")

    assert "completed: 2026-09-30 14:22" in " ".join(output.split())


def test_show_survives_iso_strings_in_integer_timestamp_columns(kanban_home):
    """`kanban show` must not crash on ISO-8601 strings written into the
    INTEGER timestamp columns of tasks / task_runs / task_events by raw SQL
    outside the mutators (#97455): both output modes stay readable and serve
    epoch ints instead of raising ``invalid literal for int()``."""
    iso_utc = "2026-08-28T14:35:22+00:00"
    iso_z = "2026-08-28T16:58:56.613Z"
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="corrupted card", assignee="alice")
        assert kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="done")
        conn.execute("UPDATE task_runs SET ended_at = ? WHERE task_id = ?", (iso_utc, task_id))
        conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (iso_z, task_id))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (iso_z, task_id))
        conn.commit()

    output = kc.run_slash(f"show {task_id}")
    assert f"Task {task_id}: corrupted card" in output
    assert "invalid literal" not in output

    payload = json.loads(kc.run_slash(f"show {task_id} --json"))
    assert payload["task"]["id"] == task_id
    assert [r["ended_at"] for r in payload["runs"]] == [1787927722]  # iso_utc, not a 0 fallback
    assert payload["events"] and {e["created_at"] for e in payload["events"]} == {1787936336}  # iso_z


def test_kanban_edit_updates_documented_task_fields(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="old title", body="old body", priority=2)

    kc.run_slash(
        f"edit {task_id} --title 'new title' --body 'new body' --priority 70"
    )

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
    assert (task.title, task.body, task.priority) == ("new title", "new body", 70)
    assert any(event.kind == "reprioritized" for event in events)


def test_worker_link_preserves_foreign_child_rules(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        worker = kb.create_task(conn, title="worker")
        assert kb.claim_task(conn, worker, claimer="worker") is not None
        worker_run_id = kb.get_task(conn, worker).current_run_id
        parent = kb.create_task(conn, title="unfinished parent")
        ready_child = kb.create_task(conn, title="foreign ready child")
        running_child = kb.create_task(conn, title="foreign running child")
        assert kb.claim_task(conn, running_child, claimer="other") is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", worker)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(worker_run_id))

    assert kc._cmd_link(argparse.Namespace(
        parent_id=parent, child_id=ready_child,
    )) == 0
    with pytest.raises(ValueError, match="child is already running"):
        kc._cmd_link(argparse.Namespace(
            parent_id=parent, child_id=running_child,
        ))
    # Owner handoff: the worker links its own running card, proving ownership
    # with HERMES_KANBAN_RUN_ID — the one path _cmd_link forwards a run id for.
    assert kc._cmd_link(argparse.Namespace(
        parent_id=parent, child_id=worker,
    )) == 0

    with kbc.connect_closing() as conn:
        assert kb.parent_ids(conn, ready_child) == [parent]
        assert kb.parent_ids(conn, running_child) == []
        assert kb.parent_ids(conn, worker) == [parent]


def _kanban_args(*tokens: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser.parse_args(["kanban", *tokens])


def _latest_signature(conn, task_id: str) -> str:
    return kb.canonical_event_signature(kb.list_events(conn, task_id)[-1])


def _review_parent_preconditions(conn, parent_task_id: str) -> kb.ReviewParentPreconditions:
    return kb.ReviewParentPreconditions(
        task_id=parent_task_id,
        expected_status="blocked",
        expected_block_kind="review",
        expected_event_signature=_latest_signature(conn, parent_task_id),
        expected_review_input_digest=kb.review_input_digest(conn, parent_task_id),
    )


def _review_parent_cli_flags(preconditions: kb.ReviewParentPreconditions) -> str:
    return (
        f"--parent-task-id {preconditions.task_id} "
        f"--expected-parent-status {preconditions.expected_status} "
        f"--expected-parent-block-kind {preconditions.expected_block_kind} "
        f"--expected-parent-event-signature {preconditions.expected_event_signature} "
        f"--expected-review-input-digest {preconditions.expected_review_input_digest}"
    )


def _create_review_lane_fixture(
    conn,
    *,
    tenant: str | None = None,
    generation: str,
) -> tuple[str, kb.ReviewParentPreconditions, str]:
    parent = kb.create_task(conn, title="implementation awaiting review", tenant=tenant)
    assert kb.block_task(conn, parent, reason="review handoff", kind="review")
    preconditions = _review_parent_preconditions(conn, parent)
    request = kb.ReviewGenerationCreate(
        board="default",
        tenant=tenant,
        key=f"review-lane:default:{tenant or ''}:{parent}:{generation}",
        title="review lane",
        assignee="reviewer",
        parent_preconditions=preconditions,
        operation_id=f"create-{generation}",
    )
    task_id = kb.create_review_generation_task(conn, request).task_id
    return task_id, preconditions, parent




















def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kbc.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kbc.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db_connect as kbc

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kbc.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


