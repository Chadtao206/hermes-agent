"""Tests for the ``hermes kanban wake-arm`` helper command."""

from __future__ import annotations

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


def _create_task(title: str = "demo", assignee: str = "alice") -> str:
    with kb.connect() as conn:
        return kb.create_task(conn, title=title, assignee=assignee)


def _get_named_sub(task_id: str, profile: str, name: str) -> dict:
    with kb.connect() as conn:
        rows = kb.list_profile_event_subs(
            conn, task_id=task_id, profile=profile, enabled_only=False,
        )
    matches = [r for r in rows if (r.get("name") or "") == name]
    assert matches, f"expected sub {task_id}:{profile}:{name}, got {rows!r}"
    return matches[0]


def test_wake_arm_adds_preset_subscription(kanban_home):
    tid = _create_task()
    out = kc.run_slash(f"wake-arm {tid}")
    assert f"Added kanban wake-arm {tid}:default:jensen-orchestrator" in out

    row = _get_named_sub(tid, profile="default", name="jensen-orchestrator")
    assert int(row["include_children"]) == 1
    assert int(row["wake_agent"]) == 1
    assert int(row["enabled"]) == 1
    assert kb._parse_event_kinds_column(row["event_kinds"]) == list(
        kc._WAKE_ARM_EVENT_KINDS
    )
    assert "Do NOT create cron jobs" in (row["wake_prompt"] or "")


def test_wake_arm_updates_existing_sub_to_preset(kanban_home):
    tid = _create_task()
    with kb.connect() as conn:
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="default",
            name="jensen-orchestrator",
            event_kinds=["completed"],
            include_children=False,
            wake_agent=False,
            wake_prompt="old prompt",
            enabled=False,
        )

    out = kc.run_slash(f"wake-arm {tid}")
    assert f"Updated kanban wake-arm {tid}:default:jensen-orchestrator" in out

    row = _get_named_sub(tid, profile="default", name="jensen-orchestrator")
    assert kb._parse_event_kinds_column(row["event_kinds"]) == list(
        kc._WAKE_ARM_EVENT_KINDS
    )
    assert int(row["include_children"]) == 1
    assert int(row["wake_agent"]) == 1
    assert int(row["enabled"]) == 1
    assert row["wake_prompt"] == kc._WAKE_ARM_PROMPT


def test_wake_arm_refuses_recursive_wake_context(kanban_home, monkeypatch):
    tid = _create_task()
    monkeypatch.setenv("HERMES_KANBAN_EVENT_WAKE", "1")

    out = kc.run_slash(f"wake-arm {tid}")
    assert "refusing to arm subscriptions from inside an event-wake run" in out.lower()

    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, task_id=tid, enabled_only=False) == []


def test_wake_arm_errors_on_missing_task(kanban_home):
    out = kc.run_slash("wake-arm t_doesnotexist")
    assert "no such task" in out.lower()


def test_wake_arm_errors_on_unknown_profile(kanban_home):
    tid = _create_task()
    out = kc.run_slash(f"wake-arm {tid} --profile not-a-real-profile")
    assert "not found on disk" in out.lower()

    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, task_id=tid, enabled_only=False) == []


def test_wake_arm_only_touches_target_task(kanban_home):
    t1 = _create_task("task one")
    t2 = _create_task("task two")

    kc.run_slash(f"wake-arm {t1}")

    with kb.connect() as conn:
        t1_subs = kb.list_profile_event_subs(conn, task_id=t1, enabled_only=False)
        t2_subs = kb.list_profile_event_subs(conn, task_id=t2, enabled_only=False)
        all_subs = kb.list_profile_event_subs(conn, enabled_only=False)

    assert len(t1_subs) == 1
    assert t2_subs == []
    assert len(all_subs) == 1


def _enable_auto_wake_arm(home: Path, *, profile: str = "default", name: str = "jensen-orchestrator") -> None:
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  auto_wake_arm_roots: true\n"
        f"  wake_arm_profile: {profile}\n"
        f"  wake_arm_name: {name}\n",
        encoding="utf-8",
    )


def test_create_task_does_not_auto_arm_by_default(kanban_home):
    tid = _create_task()
    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, task_id=tid, enabled_only=False) == []


def test_create_root_auto_arms_configured_orchestrator(kanban_home):
    _enable_auto_wake_arm(kanban_home)

    root = _create_task(title="root", assignee="default")
    child = None
    with kb.connect() as conn:
        child = kb.create_task(conn, title="child", assignee="alice", parents=[root])
        root_subs = kb.list_profile_event_subs(
            conn, task_id=root, profile="default", enabled_only=False,
        )
        child_subs = kb.list_profile_event_subs(conn, task_id=child, enabled_only=False)

    assert child
    assert len(root_subs) == 1
    row = root_subs[0]
    assert (row.get("name") or "") == "jensen-orchestrator"
    assert kb._parse_event_kinds_column(row["event_kinds"]) == list(kb.WAKE_ARM_EVENT_KINDS)
    assert int(row["include_children"]) == 1
    assert int(row["wake_agent"]) == 1
    assert int(row["enabled"]) == 1
    assert row["wake_prompt"] == kb.WAKE_ARM_PROMPT
    assert child_subs == []


def test_auto_wake_arm_respects_profile_and_name_config(kanban_home):
    _enable_auto_wake_arm(kanban_home, profile="ops", name="ops-watch")

    tid = _create_task()
    with kb.connect() as conn:
        rows = kb.list_profile_event_subs(conn, task_id=tid, enabled_only=False)

    assert len(rows) == 1
    assert rows[0]["profile"] == "ops"
    assert (rows[0].get("name") or "") == "ops-watch"


def test_auto_wake_arm_skips_inside_event_wake_context(kanban_home, monkeypatch):
    _enable_auto_wake_arm(kanban_home)
    monkeypatch.setenv("HERMES_KANBAN_EVENT_WAKE", "1")

    tid = _create_task()
    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, task_id=tid, enabled_only=False) == []


def test_auto_wake_arm_idempotency_return_arms_existing_root(kanban_home):
    root = None
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="idempotent", assignee="default", idempotency_key="same",
        )
        assert kb.list_profile_event_subs(conn, task_id=root, enabled_only=False) == []

    _enable_auto_wake_arm(kanban_home)
    with kb.connect() as conn:
        same = kb.create_task(
            conn, title="idempotent", assignee="default", idempotency_key="same",
        )
        rows = kb.list_profile_event_subs(conn, task_id=root, enabled_only=False)

    assert same == root
    assert len(rows) == 1
