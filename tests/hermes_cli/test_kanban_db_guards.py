"""Fork tests: respawn-guard semantics and the low-disk watermark (Ylopo fork).

Ported from the fork's additions to test_kanban_db.py during the 2026-09 upstream
sync; the code under test lives in hermes_cli/kanban_db_guards.py and its hook
points in kanban_db_dispatch / kanban_db_workspace.
"""
from __future__ import annotations
import subprocess
import time
import types
from pathlib import Path
import shutil
from dataclasses import replace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_guards as kbg


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_has_spawnable_ready_false_when_every_real_profile_task_is_guarded(
    kanban_home, monkeypatch,
):
    """A guard-held card is not spawnable and must not trigger the gateway's
    repeated "ready queue stuck" warning."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="awaiting PR review", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'done', 'blocked', ?, ?)",
            (task_id, now - 120, now - 60),
        )
        kb.add_comment(
            conn, task_id, "alice",
            "Opened https://github.com/acme/widgets/pull/7",
        )

        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        assert kbd.has_spawnable_ready(conn) is False


def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "recent_success"


def test_respawn_guard_recent_success_bypassed_by_requeue(kanban_home):
    """An explicit re-queue after a recent success (operator done->ready,
    promote, unblock, reclaim) is a deliberate re-run and must bypass the
    recent_success guard — otherwise a manual done->ready just sits there
    until the window elapses."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="rerun-me", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        # Baseline: a recent completion defers the respawn.
        assert kbd.check_respawn_guard(conn, t) == "recent_success"
        # Operator drags done -> ready: a 'status' event after completion.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'status', ?)",
            (t, now - 10),
        )
        assert kbd.check_respawn_guard(conn, t) is None


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kbd._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A PR URL posted after a prior run triggers active_pr."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'done', 'blocked', ?, ?)",
            (t, now - 120, now - 60),
        )
        kb.add_comment(
            conn, t, "alice",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """A GitHub PR URL in a comment older than the PR window does not block."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="old-pr", assignee="alice")
        old_ts = int(time.time()) - kbd._RESPAWN_GUARD_PR_WINDOW - 60
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', "
            "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
            (t, old_ts),
        )
        reason = kbd.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kbc.connect() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kbc.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kbc.connect() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kbc.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips a previously-run task with an active PR comment."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kbc.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'alice', 'done', 'blocked', ?, ?)",
            (t, now - 120, now - 60),
        )
        kb.add_comment(
            conn, t, "alice",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kbc.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kbd.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kbc.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kbc.connect() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


def test_low_disk_watermark_blocks_new_scratch_but_allows_existing(
    kanban_home,
    monkeypatch,
):
    """The watermark stops growth without stranding an existing workspace."""
    gib = 1024 ** 3
    monkeypatch.setattr(kbg, "_configured_min_free_disk_gb",
        lambda: 20.0,
        raising=False,
    )
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(total=100 * gib, used=90 * gib, free=10 * gib),
    )

    with kbc.connect() as conn:
        new_id = kb.create_task(conn, title="new scratch")
        new_task = kb.get_task(conn, new_id)
        assert new_task is not None
        new_path = kb.workspaces_root() / new_id
        with pytest.raises(kbg.WorkspaceDiskSpaceError, match="20.*GiB"):
            kbw.resolve_workspace(new_task)
        assert not new_path.exists()

        existing_id = kb.create_task(conn, title="existing scratch")
        existing_path = kb.workspaces_root() / existing_id
        existing_path.mkdir(parents=True)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(existing_path), existing_id),
        )
        conn.commit()
        existing_task = kb.get_task(conn, existing_id)
        assert existing_task is not None
        assert kbw.resolve_workspace(existing_task) == existing_path


@pytest.mark.parametrize("failure", ["disk", "workspace", "spawn"])
def test_dispatch_waits_for_disk_but_counts_real_failures(
    kanban_home, all_assignees_spawnable, monkeypatch, failure,
):
    gib = 1024 ** 3
    capacity = {"free": (10 if failure == "disk" else 30) * gib}
    monkeypatch.setattr(kbg, "_configured_min_free_disk_gb", lambda: 20.0)
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    def disk_usage(_path):
        if failure == "workspace":
            raise PermissionError("filesystem unavailable")
        return types.SimpleNamespace(
            total=100 * gib, used=100 * gib - capacity["free"], free=capacity["free"],
        )

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    spawned = []

    def spawn(task, workspace):
        if failure == "spawn":
            raise RuntimeError("worker launch failed")
        spawned.append(task.id)

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="disk admission wait", assignee="alice")
        if failure != "disk":
            for count in (1, 2):
                result = kbd.dispatch_once(conn, spawn_fn=spawn, failure_limit=2)
                task = kb.get_task(conn, task_id)
                assert task.consecutive_failures == count
                assert task.current_run_id is None
                assert task.claim_lock is None
            assert task.status == "blocked"
            assert result.auto_blocked == [task_id]
            assert not spawned
            return
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) "
            "VALUES (?, 'alice', 'crashed', 'crashed', ?, ?, ?)",
            (task_id, int(time.time()) - 120, int(time.time()) - 60, '{"protocol_violation": true}'),
        )
        conn.commit()
        for _ in range(3):
            result = kbd.dispatch_once(conn, spawn_fn=spawn, failure_limit=2)
            task = kb.get_task(conn, task_id)
            assert task.status == "ready"
            assert task.consecutive_failures == 0
            assert task.current_run_id is None
            assert task.claim_lock is None
            assert not result.auto_blocked
            assert not spawned
            assert not (kb.workspaces_root() / task_id).exists()
        assert conn.execute(
            "SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,),
        ).fetchone()[0] == 2
        assert kbd._protocol_violation_streak(conn, task_id) == 1
        assert conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='respawn_guarded'",
            (task_id,),
        ).fetchone()[0] == 1

        capacity["free"] = 30 * gib
        kbd.dispatch_once(conn, spawn_fn=spawn, failure_limit=2)
        assert spawned == [task_id]
        assert kb.get_task(conn, task_id).status == "running"


@pytest.mark.parametrize("phase", ["ready", "review"])
@pytest.mark.parametrize("ownership", ["owned", "stale_claim", "stale_run", "worker_started"])
def test_disk_wait_only_releases_owned_unstarted_run(kanban_home, phase, ownership):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="owned disk wait", assignee="alice")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (phase, task_id))
        conn.commit()
        claim = kb.claim_review_task if phase == "review" else kb.claim_task
        task = claim(conn, task_id)
        assert task is not None
        if ownership == "stale_claim":
            task = replace(task, claim_lock="former-owner")
        elif ownership == "stale_run":
            task = replace(task, current_run_id=task.current_run_id + 1)
        elif ownership == "worker_started":
            conn.execute("UPDATE tasks SET worker_pid=42 WHERE id=?", (task_id,))
            conn.commit()
        before = tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        error = kbg.WorkspaceDiskSpaceError("capacity low", path=kb.workspaces_root() / task_id)
        changed = kbg.defer_workspace_capacity(conn, task, error)
        current = kb.get_task(conn, task_id)
        if ownership == "owned":
            assert changed
            assert current.status == phase
            assert current.current_run_id is None
            assert current.claim_lock is None
            assert current.consecutive_failures == 0
        else:
            assert not changed
            assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()) == before
            assert conn.execute(
                "SELECT ended_at FROM task_runs WHERE task_id=?", (task_id,),
            ).fetchone()[0] is None


def test_disk_wait_follows_changed_board_workspace(kanban_home, tmp_path, monkeypatch, all_assignees_spawnable):
    full_repo, healthy_repo = tmp_path / "full", tmp_path / "healthy"
    _init_git_repo(full_repo)
    _init_git_repo(healthy_repo)
    gib = 1024 ** 3
    monkeypatch.setattr(kbg, "_configured_min_free_disk_gb", lambda: 20.0)
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)

    def disk_usage(path):
        free = (10 if Path(path).is_relative_to(full_repo) else 30) * gib
        return types.SimpleNamespace(total=100 * gib, used=100 * gib - free, free=free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    spawns = []
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="move board volume", assignee="alice", workspace_kind="worktree")
        kb.write_board_metadata(kb.get_current_board(), default_workdir=str(full_repo))
        kbd.dispatch_once(conn, spawn_fn=lambda task, path: spawns.append(path))
        assert kb.get_task(conn, task_id).status == "ready"
        assert not spawns
        kb.write_board_metadata(kb.get_current_board(), default_workdir=str(healthy_repo))
        kbd.dispatch_once(conn, spawn_fn=lambda task, path: spawns.append(path))
        assert spawns == [str(healthy_repo / ".worktrees" / task_id)]
        assert kb.get_task(conn, task_id).consecutive_failures == 0
        assert not (full_repo / ".worktrees" / task_id).exists()


def test_low_disk_watermark_blocks_new_git_worktree(
    kanban_home,
    tmp_path,
    monkeypatch,
):
    """Automated worktree materialization uses the same disk-growth guard."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    gib = 1024 ** 3
    monkeypatch.setattr(kbg, "_configured_min_free_disk_gb",
        lambda: 20.0,
        raising=False,
    )
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: types.SimpleNamespace(total=100 * gib, used=90 * gib, free=10 * gib),
    )

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="new worktree",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        with pytest.raises(kbg.WorkspaceDiskSpaceError, match="20.*GiB"):
            kbw.resolve_workspace(task)

    assert not (repo / ".worktrees" / task_id).exists()


