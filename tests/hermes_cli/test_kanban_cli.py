"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


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

@pytest.mark.parametrize(
    "value,expected",
    [
        ("scratch",              ("scratch", None)),
        ("worktree",              ("worktree", None)),
        ("worktree:/tmp/wt",       ("worktree", "/tmp/wt")),
        ("dir:/tmp/work",         ("dir", "/tmp/work")),
    ],
)
def test_parse_workspace_flag_valid(value, expected):
    assert kc._parse_workspace_flag(value) == expected


def test_parse_workspace_flag_expands_user():
    kind, path = kc._parse_workspace_flag("dir:~/vault")
    assert kind == "dir"
    assert path.endswith("/vault")
    assert not path.startswith("~")

    kind, path = kc._parse_workspace_flag("worktree:~/trees/t6-wire")
    assert kind == "worktree"
    assert path.endswith("/trees/t6-wire")
    assert not path.startswith("~")

@pytest.mark.parametrize("bad", ["cloud", "dir:", "worktree:", ""])
def test_parse_workspace_flag_rejects(bad):
    if not bad:
        # Empty -> defaults; not an error.
        assert kc._parse_workspace_flag(bad) == ("scratch", None)
        return
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_workspace_flag(bad)


def test_parse_branch_flag_rejects_empty_and_option_like():
    assert kc._parse_branch_flag(None) is None
    assert kc._parse_branch_flag(" wt/t6-wire ") == "wt/t6-wire"
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("   ")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("-bad")
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_branch_flag("bad branch")


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

    with kb.connect() as conn:
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


def test_create_review_generation_cli_replays_archived_lane_and_isolates_project_workspace(kanban_home, tmp_path):
    repository = tmp_path / "repository"
    with kb.connect_closing() as conn:
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
        assert kb.block_task(conn, source, reason="review handoff", kind="review")
        parent_preconditions = _review_parent_preconditions(conn, source)
    key = f"review-lane:default::${source}:g-cli".replace("$", "")
    command = (
        "create-review-generation 'review immutable target' "
        f"--key {key} --assignee reviewer --project project-1 "
        f"--project-source-task {source} "
        f"{_review_parent_cli_flags(parent_preconditions)} "
        "--operation-id cli-create-review --json"
    )
    first = json.loads(kc.run_slash(command))
    assert first["created"] is True
    with kb.connect_closing() as conn:
        reviewer = kb.get_task(conn, first["task_id"])
        assert reviewer is not None
        assert reviewer.workspace_path == str(repository / ".worktrees" / reviewer.id)
        assert reviewer.workspace_path != str(source_path)
        assert kb.archive_task(conn, reviewer.id)

    replay = json.loads(kc.run_slash(command))
    assert replay == {
        "outcome": "deduplicated",
        "task_id": first["task_id"],
        "created": False,
        "operation_id": "cli-create-review",
        "reason": "",
    }


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


def test_controller_lease_cli_uses_explicit_nondefault_board(kanban_home):
    kb.create_board("alpha")

    result = _lease_cli("alpha", "acquire")

    assert set(result) == {
        "acquired", "controller_id", "board", "holder_token", "holder_epoch",
        "issued_at", "expires_at", "operation_id", "reason",
    }
    assert result["acquired"] is True
    assert result["board"] == "alpha"
    assert result["expires_at"] - result["issued_at"] == 300
    with kb.connect_closing(board="alpha") as connection:
        alpha = kb.read_recovery_snapshot(connection, now=result["issued_at"])
    with kb.connect_closing(board="default") as connection:
        default = kb.read_recovery_snapshot(connection, now=result["issued_at"])
    assert len(alpha.leases) == 1
    assert default.leases == ()


def test_controller_lease_cli_contention_and_same_token_replay(kanban_home):
    first = _lease_cli("default", "acquire")

    replay = _lease_cli("default", "acquire")
    contended = _lease_cli(
        "default", "acquire", token="other-token", operation_id="lease-operation-2",
    )

    assert replay == {
        **first,
        "operation_id": "lease-operation-1",
        "reason": "lease already held by this token",
    }
    assert contended["acquired"] is False
    assert contended["holder_token"] == "router-token"
    assert contended["holder_epoch"] == first["holder_epoch"]
    assert contended["expires_at"] == first["expires_at"]
    assert contended["reason"] == "lease is held by another token"


def test_controller_lease_cli_stale_renewal_is_structured_nonacquisition(kanban_home):
    first = _lease_cli("default", "acquire")

    stale = _lease_cli(
        "default",
        "renew",
        holder_epoch=first["holder_epoch"] + 1,
        operation_id="lease-renew-stale",
    )

    assert stale["acquired"] is False
    assert stale["holder_epoch"] == first["holder_epoch"]
    assert stale["expires_at"] == first["expires_at"]
    assert stale["reason"]


def test_controller_claim_publish_cli_is_tenant_scoped_and_replay_safe(kanban_home):
    with kb.connect_closing(board="default") as connection:
        task_id = kb.create_task(connection, title="claim cli", tenant="alpha")
    lease = _lease_cli("default", "acquire", token="claim-cli-token")
    with kb.connect_closing(board="default") as connection:
        snapshot = kb.read_recovery_snapshot(connection, now=lease["issued_at"])
        generation_signature = kb.recovery_task_generation_signature(
            snapshot, task_id,
        )
        input_signature = kb.recovery_task_input_signature(snapshot, task_id)
    command = (
        f"--board default controller-claim publish {task_id} "
        "--expected-tenant alpha --controller-id blocked-recovery "
        f"--generation-signature '{generation_signature}' --authority mutate "
        f"--input-signature {input_signature} --lease-epoch {lease['holder_epoch']} "
        f"--expires-at {lease['expires_at']} --operation-id claim-cli --json"
    )

    committed = json.loads(kc.run_slash(command))
    replay = json.loads(kc.run_slash(command))

    assert committed["outcome"] == "committed", committed
    assert replay["outcome"] == "deduplicated"
    with kb.connect_closing(board="default") as connection:
        connection.execute("UPDATE tasks SET tenant = 'beta' WHERE id = ?", (task_id,))
    stale = json.loads(kc.run_slash(command))
    assert stale["outcome"] == "precondition_changed"


def _planned_controller_mutation_cli(kanban_home):
    with kb.connect_closing(board="default") as connection:
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
    with kb.connect_closing(board="default") as connection:
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


def test_controller_claim_mutate_cli_replays_the_complete_json_contract(kanban_home):
    task_id, _lease, _generation, _input, mutate_command = (
        _planned_controller_mutation_cli(kanban_home)
    )

    committed = json.loads(kc.run_slash(mutate_command))
    replayed = json.loads(kc.run_slash(mutate_command))

    assert committed == {
        "outcome": "committed",
        "task_id": task_id,
        "operation_id": "guarded-cli-unblock",
        "event_id": committed["event_id"],
        "reason": "",
    }
    assert replayed == committed
    with kb.connect_closing(board="default") as connection:
        assert kb.get_task(connection, task_id).status == "ready"


def test_controller_claim_mutate_cli_rejects_competing_authority_after_planning(
    kanban_home,
):
    task_id, lease, generation_signature, input_signature, mutate_command = (
        _planned_controller_mutation_cli(kanban_home)
    )
    with kb.connect_closing(board="default") as connection:
        competing_lease = kb.acquire_controller_lease(
            connection,
            kb.LeaseRequest(
                controller_id="competing-controller",
                board="default",
                holder_token="competing-holder",
                ttl_seconds=300,
                census_signature="d" * 64,
                operation_id="competing-cli-lease",
            ),
        )
        competing = kb.publish_controller_claim(
            connection,
            kb.ControllerClaim(
                controller_id="competing-controller",
                board="default",
                expected_tenant="alpha",
                task_id=task_id,
                generation_signature=generation_signature,
                authority="mutate",
                input_signature=input_signature,
                lease_epoch=competing_lease.holder_epoch,
                expires_at=competing_lease.expires_at,
                operation_id="competing-cli-claim",
            ),
        )
        assert competing.outcome == "committed"

    payload = json.loads(kc.run_slash(mutate_command))

    assert payload == {
        "outcome": "precondition_changed",
        "task_id": task_id,
        "operation_id": "guarded-cli-unblock",
        "event_id": None,
        "reason": "task authority changed",
    }
    with kb.connect_closing(board="default") as connection:
        assert kb.get_task(connection, task_id).status == "blocked"


def test_controller_claim_mutate_cli_rejects_boris_creator_changed_after_planning(
    kanban_home,
):
    task_id, _lease, _generation, _input, mutate_command = (
        _planned_controller_mutation_cli(kanban_home)
    )
    with kb.connect_closing(board="default") as connection:
        connection.execute(
            "UPDATE tasks SET created_by = ? WHERE id = ?",
            ("boris-slack-intake", task_id),
        )

    payload = json.loads(kc.run_slash(mutate_command))

    assert payload == {
        "outcome": "precondition_changed",
        "task_id": task_id,
        "operation_id": "guarded-cli-unblock",
        "event_id": None,
        "reason": "task is excluded from controller automation",
    }
    with kb.connect_closing(board="default") as connection:
        assert kb.get_task(connection, task_id).status == "blocked"


@pytest.mark.parametrize(
    "authority_signature, expected_error",
    [
        (None, "the following arguments are required: --authority-signature"),
        ("malformed", "authority_signature must be a lowercase SHA-256 digest"),
    ],
)
def test_controller_claim_mutate_cli_requires_valid_authority_signature(
    kanban_home, authority_signature, expected_error,
):
    authority = (
        "" if authority_signature is None
        else f" --authority-signature {authority_signature}"
    )

    output = kc.run_slash(
        "--board default controller-claim mutate missing-task "
        "--expected-tenant alpha --controller-id adjudicator "
        f"--generation-signature {'a' * 64} --input-signature {'b' * 64}"
        f"{authority} --claim-operation-id missing-claim --claim-expires-at 400 "
        "--lease-holder-token missing-holder --lease-epoch 1 "
        "--lease-issued-at 100 --lease-expires-at 400 "
        f"--lease-census-signature {'c' * 64} "
        "--lease-operation-id missing-lease --verb unblock "
        "--operation-id missing-mutation --json"
    )

    assert expected_error in output


@pytest.mark.parametrize(
    "prefix, expected_tenant",
    [
        ("", "alpha"),
        ("--board default ", ""),
    ],
)
def test_controller_claim_publish_cli_requires_board_and_tenant(
    kanban_home, prefix, expected_tenant,
):
    with kb.connect_closing(board="default") as connection:
        task_id = kb.create_task(connection, title="claim cli invalid", tenant="alpha")
    output = kc.run_slash(
        f"{prefix}controller-claim publish {task_id} "
        f"--expected-tenant {expected_tenant} --controller-id blocked-recovery "
        f"--generation-signature {'e' * 64} --authority mutate "
        f"--input-signature {'f' * 64} --lease-epoch 1 --expires-at 9999999999 "
        "--operation-id invalid-claim --json"
    )

    assert "outcome" not in output
    with kb.connect_closing(board="default") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kanban_controller_claims"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("board", "arguments"),
    [
        ("--board default ", "--holder-token token --ttl-seconds 0 --census-signature " + "c" * 64),
        ("--board default ", "--holder-token token --ttl-seconds 300 --census-signature malformed"),
        ("", "--holder-token token --ttl-seconds 300 --census-signature " + "c" * 64),
    ],
)
def test_controller_lease_cli_rejects_malformed_or_unscoped_requests(
    kanban_home,
    board,
    arguments,
):
    output = kc.run_slash(
        f"{board}controller-lease acquire --controller-id blocked-recovery "
        f"{arguments} --operation-id malformed-lease --json"
    )

    assert "acquired" not in output
    with kb.connect_closing(board="default") as connection:
        snapshot = kb.read_recovery_snapshot(connection, now=1)
    assert snapshot.leases == ()


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show


def test_run_slash_recovery_control_set_board_returns_json(kanban_home):
    from hermes_cli.kanban_recovery_config import load_recovery_control

    (kanban_home / "config.yaml").write_text(
        "kanban:\n  blocked_recovery:\n    rollout_mode: live\n", encoding="utf-8"
    )
    current = load_recovery_control()
    out = kc.run_slash(
        "recovery-control set-board "
        f"--expected-digest {current.digest()} --board default "
        "--review-mode live --transient-mode shadow --json"
    )

    result = json.loads(out)
    assert result["outcome"] == "committed"
    assert load_recovery_control().boards["default"].review_mode == "live"


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


def test_block_managed_review_cli_persists_exact_target(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="implementation", tenant="alpha")
        assert kb.claim_task(conn, task_id) is not None
        run = kb.latest_run(conn, task_id)
    target = json.dumps({
        "kind": "pull_request",
        "url": "https://github.com/Ylopo/forge/pull/123",
    })
    result = json.loads(kc.run_slash(
        f"--board default block-managed-review {task_id} --expected-tenant alpha "
        f"--expected-status running --expected-run-id {run.id} "
        f"--review-target '{target}' --operation-id cli-managed-review --json"
    ))
    assert result["outcome"] == "committed"
    with kb.connect_closing() as conn:
        event = kb.list_events(conn, task_id)[-1]
    assert event.payload["review_target"] == json.loads(target)


@pytest.mark.parametrize("command", [
    "--review-target '{not-json}' --operation-id op --json",
    "--review-target '[]' --operation-id op --json",
    "--review-target '{\"kind\": \"pull_request\", \"url\": \"https://github.com/Ylopo/forge/pull/123\"}' --json",
    "--review-target '{\"kind\": \"pull_request\", \"url\": \"https://github.com/Ylopo/forge/pull/123\"}' --operation-id op --ids other --json",
])
def test_block_managed_review_cli_rejects_invalid_or_bulk_input(kanban_home, command):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="implementation", tenant="alpha")
    out = kc.run_slash(
        f"block-managed-review {task_id} --expected-tenant alpha --expected-status ready {command}"
    )
    assert "error" in out.lower() or "usage" in out.lower()


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
    with kb.connect() as conn:
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
    with kb.connect() as conn:
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


def test_run_slash_usage_error_returns_message(kanban_home):
    # Missing required argument for create
    out = kc.run_slash("create")
    assert "usage" in out.lower() or "error" in out.lower()


def test_run_slash_assign_reassigns(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    assert "Assigned" in kc.run_slash(f"assign {tid} bob")
    show = kc.run_slash(f"show {tid}")
    assert "bob" in show


def test_run_slash_link_unlink(kanban_home):
    a = kc.run_slash("create 'a'")
    b = kc.run_slash("create 'b'")
    import re
    ta = re.search(r"(t_[a-f0-9]+)", a).group(1)
    tb = re.search(r"(t_[a-f0-9]+)", b).group(1)
    assert "Linked" in kc.run_slash(f"link {ta} {tb}")
    # After link, b is todo
    show = kc.run_slash(f"show {tb}")
    assert "todo" in show
    assert "Unlinked" in kc.run_slash(f"unlink {ta} {tb}")


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


@pytest.mark.parametrize(
    "command",
    [
        "reclassify-block t_x --expected-block-event-signature " + "0" * 64
        + " --kind review --operation-id op-1",
        "requeue-review t_x --expected-status blocked --expected-event-signature "
        + "0" * 64 + " --tenant '' --operation-id op-2",
        "archive-review t_x --expected-status ready --expected-event-signature "
        + "0" * 64 + " --tenant '' --operation-id op-3",
        "retry-transient t_x --expected-terminal-signature " + "0" * 64
        + " --retry-ordinal 1 --operation-id op-4",
    ],
)
def test_automation_recovery_commands_require_json(kanban_home, command):
    assert "usage error" in kc.run_slash(command).lower()


def test_reclassify_block_cli_serializes_handled_precondition_change(
    kanban_home,
    capsys,
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="legacy blocked")
        assert kb.block_task(conn, task_id, reason="legacy")
    args = _kanban_args(
        "reclassify-block",
        task_id,
        "--expected-block-event-signature",
        "0" * 64,
        "--kind",
        "review",
        "--operation-id",
        "cli-reclassify-stale",
        "--json",
    )

    assert kc.kanban_command(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "outcome",
        "task_id",
        "operation_id",
        "event_id",
        "reason",
    }
    assert payload["outcome"] == "precondition_changed"
    assert payload["event_id"] is None
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).block_kind is None


def test_requeue_review_cli_returns_guarded_json(kanban_home):
    with kb.connect_closing() as conn:
        task_id, parent_preconditions, _parent = _create_review_lane_fixture(
            conn,
            generation="cli-requeue",
        )
        assert kb.claim_task(conn, task_id, claimer="reviewer") is not None
        assert kb._record_spawn_failure(conn, task_id, "failed", failure_limit=1)
        signature = _latest_signature(conn, task_id)

    payload = json.loads(
        kc.run_slash(
            f"requeue-review {task_id} --expected-status blocked "
            f"--expected-event-signature {signature} "
            f"--tenant '' {_review_parent_cli_flags(parent_preconditions)} "
            "--operation-id cli-review-requeue --json"
        )
    )

    assert payload["outcome"] == "committed"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


def test_archive_review_cli_returns_guarded_json(kanban_home):
    with kb.connect_closing() as conn:
        task_id, parent_preconditions, _parent = _create_review_lane_fixture(
            conn,
            generation="cli-archive",
        )
        signature = _latest_signature(conn, task_id)

    payload = json.loads(
        kc.run_slash(
            f"archive-review {task_id} --expected-status ready "
            f"--expected-event-signature {signature} "
            f"--tenant '' {_review_parent_cli_flags(parent_preconditions)} "
            "--operation-id cli-review-archive --json"
        )
    )

    assert payload["outcome"] == "committed"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "archived"


def test_archive_review_cli_guards_named_tenant(kanban_home):
    with kb.connect_closing() as conn:
        task_id, parent_preconditions, _parent = _create_review_lane_fixture(
            conn,
            tenant="tenant-a",
            generation="cli-tenant",
        )
        signature = _latest_signature(conn, task_id)
        events_before = kb.list_events(conn, task_id)

    stale = json.loads(kc.run_slash(
        f"archive-review {task_id} --expected-status ready "
        f"--expected-event-signature {signature} --tenant tenant-b "
        f"{_review_parent_cli_flags(parent_preconditions)} "
        "--operation-id cli-review-stale-tenant --json"
    ))

    assert stale["outcome"] == "precondition_changed"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.list_events(conn, task_id) == events_before

    committed = json.loads(kc.run_slash(
        f"archive-review {task_id} --expected-status ready "
        f"--expected-event-signature {signature} --tenant tenant-a "
        f"{_review_parent_cli_flags(parent_preconditions)} "
        "--operation-id cli-review-exact-tenant --json"
    ))
    assert committed["outcome"] == "committed"


def test_create_review_generation_cli_rejects_parent_edit_race(kanban_home):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="implementation awaiting review")
        assert kb.block_task(conn, parent, reason="review handoff", kind="review")
        preconditions = _review_parent_preconditions(conn, parent)
        assert kb.assign_task(conn, parent, "different-engineer")
    key = f"review-lane:default::{parent}:cli-parent-race"

    payload = json.loads(kc.run_slash(
        "create-review-generation 'review immutable target' "
        f"--key {key} --assignee reviewer "
        f"{_review_parent_cli_flags(preconditions)} "
        "--operation-id cli-create-parent-race --json"
    ))

    assert payload == {
        "outcome": "precondition_changed",
        "task_id": "",
        "created": False,
        "operation_id": "cli-create-parent-race",
        "reason": "review parent event changed",
    }
    with kb.connect_closing() as conn:
        assert kb.lookup_review_generation(conn, "default", "", key) is None


def test_show_cli_rejects_wrong_explicit_tenant_before_readback(
    kanban_home,
    capsys,
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="tenant scoped", tenant="tenant-a")
    args = _kanban_args("show", task_id, "--tenant", "tenant-b", "--json")

    assert kc.kanban_command(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not belong to tenant" in captured.err


def test_retry_transient_cli_returns_guarded_json(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="flaky operation")
        assert kb.block_task(conn, task_id, reason="temporary", kind="transient")
        signature = _latest_signature(conn, task_id)

    payload = json.loads(
        kc.run_slash(
            f"retry-transient {task_id} --expected-terminal-signature {signature} "
            "--retry-ordinal 1 --operation-id cli-transient-1 --json"
        )
    )

    assert payload["outcome"] == "committed"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


def test_acknowledge_escalation_cli_derives_local_guard_and_operation(
    kanban_home,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "local-operator")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="human escalation", tenant="team-a")
        assert kb.block_task(conn, task_id, reason="decision", kind="needs_input")
        status_before = kb.get_task(conn, task_id).status

    command = f"acknowledge-escalation {task_id} --signature {'b' * 64} --json"
    payload = json.loads(kc.run_slash(command))
    replay = json.loads(kc.run_slash(command))

    assert payload["outcome"] == "committed"
    assert payload["operation_id"]
    assert replay["event_id"] == payload["event_id"]
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        events = [event for event in kb.list_events(conn, task_id) if event.kind == "escalation_acknowledged"]
    assert task.status == status_before
    assert len(events) == 1
    assert events[0].payload["actor"] == "local-operator"


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

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------

def test_kanban_is_resolvable():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("kanban")
    assert cmd is not None
    assert cmd.name == "kanban"


def test_kanban_bypasses_active_session_guard():
    from hermes_cli.commands import should_bypass_active_session

    assert should_bypass_active_session("kanban")


def test_kanban_in_autocomplete_table():
    from hermes_cli.commands import COMMANDS, SUBCOMMANDS

    assert "/kanban" in COMMANDS
    subs = SUBCOMMANDS.get("/kanban") or []
    assert "create" in subs
    assert "dispatch" in subs


def test_kanban_autocomplete_includes_live_subcommands():
    from prompt_toolkit.document import Document

    from hermes_cli.commands import SlashCommandCompleter

    completer = SlashCommandCompleter()
    doc = Document("/kanban sp", cursor_position=len("/kanban sp"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "specify" in texts

    doc = Document("/kanban re", cursor_position=len("/kanban re"))
    texts = {c.text for c in completer.get_completions(doc, None)}

    assert "reclaim" in texts
    assert "reassign" in texts


def test_kanban_not_gateway_only():
    # kanban is available in BOTH CLI and gateway surfaces.
    from hermes_cli.commands import COMMAND_REGISTRY

    cmd = next(c for c in COMMAND_REGISTRY if c.name == "kanban")
    assert not cmd.cli_only
    assert not cmd.gateway_only


# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
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


def test_run_slash_reassign_with_reclaim_flag(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'switch model' --assignee orig")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    tid = m.group(1)

    # Simulate a running claim.
    conn = kb.connect()
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

    out = kc.run_slash(f"reassign {tid} newbie --reclaim --reason 'switch'")
    assert "Reassigned" in out, out
    out2 = kc.run_slash(f"show {tid}")
    assert "newbie" in out2


# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_specify_end_to_end(kanban_home, monkeypatch):
    """The /kanban specify slash command routes through run_slash, which
    both the interactive CLI and every gateway platform use. This test
    covers both surfaces."""
    from unittest.mock import MagicMock

    # Create a triage task via the same slash surface.
    create_out = kc.run_slash("create 'rough idea' --triage")
    import re
    m = re.search(r"(t_[a-f0-9]+)", create_out)
    assert m, f"no task id in: {create_out!r}"
    tid = m.group(1)

    # Mock the auxiliary client so we don't hit a real provider.
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = (
        '{"title": "Spec: rough idea", "body": "**Goal**\\nShip it."}'
    )
    # specify_task routes through call_llm now (#35566) — mock it directly.
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        MagicMock(return_value=resp),
    )

    # Specify via slash.
    out = kc.run_slash(f"specify {tid}")
    assert "Specified" in out
    assert tid in out

    # Task is promoted and retitled.
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in {"todo", "ready"}
    assert task.title == "Spec: rough idea"


def test_run_slash_specify_help_is_reachable(kanban_home):
    """`-h`/`--help` on a subcommand returns the actual help text — see
    issue #21794. argparse writes help to stdout and exits 0; run_slash
    must capture both streams and treat exit 0 as success, not error."""
    out = kc.run_slash("specify --help")
    assert "specify" in out.lower()
    # Help dump should NOT come back wrapped as a usage error.
    assert not out.startswith("⚠")


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------

def test_run_slash_bare_returns_curated_help(kanban_home):
    """Bare `/kanban` returns the curated short-help block — not a 5KB
    argparse usage dump."""
    out = kc.run_slash("")
    assert "/kanban" in out
    assert "list" in out
    assert "show" in out
    # Sanity: should be a chat-friendly size, not the raw usage tree.
    assert len(out) < 2000
    # Shouldn't surface argparse's usage-error sentinel.
    assert "usage error" not in out.lower()


@pytest.mark.parametrize("alias", ["help", "--help", "-h", "?"])
def test_run_slash_help_aliases_match_bare(kanban_home, alias):
    """Every documented help alias produces the same curated output."""
    bare = kc.run_slash("")
    out = kc.run_slash(alias)
    assert out == bare


def test_run_slash_subcommand_help_returns_help_text(kanban_home):
    """`/kanban show -h` returns the actual subcommand help, not a
    fake `(usage error: 0)` sentinel."""
    out = kc.run_slash("show -h")
    assert "task_id" in out
    assert "/kanban show" in out
    assert not out.startswith("⚠")


def test_run_slash_unknown_action_friendly_error(kanban_home):
    """Unknown subcommand surfaces a single-line usage error prefixed
    with our marker — no `(usage error: 2)` wrapping, no doubled
    `kanban kanban` prog string."""
    out = kc.run_slash("frobnicate")
    assert "/kanban" in out
    assert "frobnicate" in out
    assert "/kanban-wrap" not in out
    assert "/kanban kanban" not in out
    assert "(usage error: " not in out


def test_run_slash_missing_required_arg_friendly_error(kanban_home):
    """Missing positional argument shows the subcommand-scoped usage
    line, not the top-level kanban tree."""
    out = kc.run_slash("show")
    assert "/kanban show" in out
    assert "task_id" in out


def test_run_slash_board_override_restores_prior_env(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")

    kc.run_slash("--board alpha list")

    assert os.environ.get("HERMES_KANBAN_BOARD") == "beta"


def test_run_slash_board_override_does_not_change_boards_show_current(kanban_home):
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.set_current_board("alpha")

    out = kc.run_slash("--board beta boards show")

    assert "Current board: alpha" in out
