"""Tests for the ``hermes kanban profile-subs`` CLI surface.

Covers:
- ``_parse_event_kinds_flag`` (comma + space-separated, dedup, empty -> None)
- ``_parse_profile_sub_id`` (TASK_ID:PROFILE[:NAME], rejects malformed)
- ``profile-subs add/list/remove`` via ``run_slash`` (the same entry point CLI
  and gateway use), exercising idempotent add, custom kinds, ``--no-wake`` /
  ``--include-children`` / ``--disabled`` flag round-trips, and ``--all``.
"""

from __future__ import annotations

import json
import re
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


# ---------------------------------------------------------------------------
# Pure parser helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "values,expected",
    [
        (None, None),
        ([], None),
        (["completed"], ["completed"]),
        (["completed,blocked"], ["completed", "blocked"]),
        (["completed", "blocked"], ["completed", "blocked"]),
        (["completed,blocked", "gave_up"], ["completed", "blocked", "gave_up"]),
        ([" completed , ", " blocked"], ["completed", "blocked"]),
        (["completed,completed,blocked"], ["completed", "blocked"]),
        ([",,,"], None),
        ([""], None),
    ],
)
def test_parse_event_kinds_flag(values, expected):
    assert kc._parse_event_kinds_flag(values) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("t_abc:jensen",            ("t_abc", "jensen", "")),
        ("t_abc:jensen:lane-a",     ("t_abc", "jensen", "lane-a")),
        ("t_abc:jensen:lane:extra", ("t_abc", "jensen", "lane:extra")),
    ],
)
def test_parse_profile_sub_id_valid(value, expected):
    assert kc._parse_profile_sub_id(value) == expected


@pytest.mark.parametrize("bad", ["", "  ", "t_abc", ":jensen", "t_abc:", ":"])
def test_parse_profile_sub_id_rejects(bad):
    with pytest.raises(ValueError):
        kc._parse_profile_sub_id(bad)


def test_format_profile_sub_id_with_and_without_name():
    assert kc._format_profile_sub_id("t_abc", "jensen", "") == "t_abc:jensen"
    assert (
        kc._format_profile_sub_id("t_abc", "jensen", "lane-a")
        == "t_abc:jensen:lane-a"
    )


# ---------------------------------------------------------------------------
# `profile-subs add` end-to-end (via run_slash)
# ---------------------------------------------------------------------------

def test_profile_subs_add_creates_subscription(kanban_home):
    tid = _create_task()
    out = kc.run_slash(f"profile-subs add {tid} jensen --events completed,blocked")
    assert f"Added profile-sub {tid}:jensen" in out

    with kb.connect() as conn:
        subs = kb.list_profile_event_subs(conn, task_id=tid)
    assert len(subs) == 1
    row = subs[0]
    assert row["profile"] == "jensen"
    assert (row.get("name") or "") == ""
    assert kb._parse_event_kinds_column(row["event_kinds"]) == ["completed", "blocked"]
    assert int(row["include_children"]) == 0
    assert int(row["wake_agent"]) == 1
    assert int(row["enabled"]) == 1
    assert row["wake_prompt"] is None


def test_profile_subs_add_supports_space_separated_events(kanban_home):
    tid = _create_task()
    out = kc.run_slash(
        f"profile-subs add {tid} jensen --events completed blocked"
    )
    assert "Added profile-sub" in out
    with kb.connect() as conn:
        row = kb.list_profile_event_subs(conn, task_id=tid)[0]
    assert kb._parse_event_kinds_column(row["event_kinds"]) == [
        "completed", "blocked",
    ]


def test_profile_subs_add_flags_roundtrip(kanban_home):
    tid = _create_task()
    out = kc.run_slash(
        f"profile-subs add {tid} jensen --name lane-a "
        f"--events completed --include-children --no-wake "
        f"--wake-prompt 'hello world'"
    )
    assert f"Added profile-sub {tid}:jensen:lane-a" in out

    with kb.connect() as conn:
        rows = kb.list_profile_event_subs(
            conn, task_id=tid, profile="jensen", enabled_only=False,
        )
    assert len(rows) == 1
    row = rows[0]
    assert (row.get("name") or "") == "lane-a"
    assert int(row["include_children"]) == 1
    assert int(row["wake_agent"]) == 0
    assert int(row["enabled"]) == 1
    assert row["wake_prompt"] == "hello world"


def test_profile_subs_add_disabled_hidden_from_default_list(kanban_home):
    tid = _create_task()
    kc.run_slash(f"profile-subs add {tid} jensen --disabled")
    assert "no profile event subscriptions" in kc.run_slash("profile-subs list").lower()
    listing = kc.run_slash("profile-subs list --all")
    assert f"{tid}:jensen" in listing
    assert "disabled" in listing


def test_profile_subs_add_idempotent_preserves_existing_options(kanban_home):
    """Re-running add without options must not clobber existing settings.

    Mirrors the DB-level invariant covered by
    test_profile_event_sub_readd_preserves_existing_options.
    """
    tid = _create_task()
    kc.run_slash(
        f"profile-subs add {tid} jensen --events completed --include-children "
        f"--no-wake --wake-prompt 'keep me' --disabled"
    )
    out = kc.run_slash(f"profile-subs add {tid} jensen")
    assert f"Updated profile-sub {tid}:jensen" in out

    with kb.connect() as conn:
        row = kb.list_profile_event_subs(
            conn, task_id=tid, profile="jensen", enabled_only=False,
        )[0]
    assert kb._parse_event_kinds_column(row["event_kinds"]) == ["completed"]
    assert int(row["include_children"]) == 1
    assert int(row["wake_agent"]) == 0
    assert int(row["enabled"]) == 0
    assert row["wake_prompt"] == "keep me"


def test_profile_subs_add_unknown_task_errors(kanban_home):
    out = kc.run_slash("profile-subs add t_doesnotexist jensen")
    assert "no such task" in out.lower()
    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, enabled_only=False) == []


def test_profile_subs_add_json_surfaces_sub_id(kanban_home):
    tid = _create_task()
    out = kc.run_slash(f"profile-subs add {tid} jensen --json")
    # run_slash returns text; the JSON block is the stdout body.
    payload = json.loads(out)
    assert payload["sub_id"] == f"{tid}:jensen"
    assert payload["existed"] is False
    assert payload["sub"]["profile"] == "jensen"

    out2 = kc.run_slash(f"profile-subs add {tid} jensen --json")
    payload2 = json.loads(out2)
    assert payload2["existed"] is True


# ---------------------------------------------------------------------------
# `profile-subs list` filters
# ---------------------------------------------------------------------------

def test_profile_subs_list_filters_by_task_and_profile(kanban_home):
    t1 = _create_task("task one")
    t2 = _create_task("task two")
    kc.run_slash(f"profile-subs add {t1} jensen")
    kc.run_slash(f"profile-subs add {t1} other-profile")
    kc.run_slash(f"profile-subs add {t2} jensen")

    out_all = kc.run_slash("profile-subs list")
    # Three lines, one per sub.
    listed = [line for line in out_all.splitlines() if ":" in line]
    assert len(listed) == 3

    out_task = kc.run_slash(f"profile-subs list --task {t1}")
    assert f"{t1}:jensen" in out_task
    assert f"{t1}:other-profile" in out_task
    assert f"{t2}:" not in out_task

    out_profile = kc.run_slash("profile-subs list --profile jensen")
    assert f"{t1}:jensen" in out_profile
    assert f"{t2}:jensen" in out_profile
    assert "other-profile" not in out_profile


def test_profile_subs_list_json_emits_array(kanban_home):
    tid = _create_task()
    kc.run_slash(f"profile-subs add {tid} jensen --events completed")
    out = kc.run_slash("profile-subs list --json")
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert payload[0]["profile"] == "jensen"
    assert payload[0]["task_id"] == tid


# ---------------------------------------------------------------------------
# `profile-subs remove`
# ---------------------------------------------------------------------------

def test_profile_subs_remove_by_sub_id(kanban_home):
    tid = _create_task()
    kc.run_slash(f"profile-subs add {tid} jensen --name lane-a")
    kc.run_slash(f"profile-subs add {tid} jensen --name lane-b")

    out = kc.run_slash(f"profile-subs remove {tid}:jensen:lane-a")
    assert f"Removed profile-sub {tid}:jensen:lane-a" in out

    with kb.connect() as conn:
        rows = kb.list_profile_event_subs(conn, task_id=tid, enabled_only=False)
    names = sorted((r.get("name") or "") for r in rows)
    assert names == ["lane-b"]


def test_profile_subs_remove_unknown_reports_error(kanban_home):
    tid = _create_task()
    out = kc.run_slash(f"profile-subs remove {tid}:nobody")
    assert "no such profile-sub" in out.lower()


def test_profile_subs_remove_malformed_sub_id(kanban_home):
    out = kc.run_slash("profile-subs remove not-a-valid-id")
    assert "invalid sub_id" in out.lower() or "expected task_id" in out.lower()


def test_profile_subs_remove_empty_name_matches_unnamed_sub(kanban_home):
    tid = _create_task()
    kc.run_slash(f"profile-subs add {tid} jensen")
    out = kc.run_slash(f"profile-subs remove {tid}:jensen")
    assert "Removed profile-sub" in out
    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, enabled_only=False) == []


# ---------------------------------------------------------------------------
# Parser registration smoke
# ---------------------------------------------------------------------------

def test_profile_subs_parser_help_lists_subcommands(kanban_home):
    out = kc.run_slash("profile-subs --help")
    # Expect each subcommand to appear in help text.
    assert "list" in out
    assert "add" in out
    assert "remove" in out


def test_profile_subs_bare_shows_usage(kanban_home):
    out = kc.run_slash("profile-subs")
    # Either prints usage stub or returns the subparsers help.
    assert re.search(r"usage|list\b.*add\b.*remove", out, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Reversible updates via `profile-subs add` (Boris-blocked scope)
# ---------------------------------------------------------------------------

def _get_sub(tid: str, profile: str = "jensen", name: str = "") -> dict:
    with kb.connect() as conn:
        rows = kb.list_profile_event_subs(
            conn, task_id=tid, profile=profile, enabled_only=False,
        )
    matches = [r for r in rows if (r.get("name") or "") == name]
    assert matches, f"expected one sub for {tid}:{profile}:{name}, got {rows!r}"
    return matches[0]


def test_profile_subs_add_disabled_then_enable(kanban_home):
    """A disabled sub can be re-enabled via --enable without other clobbers."""
    tid = _create_task()
    kc.run_slash(
        f"profile-subs add {tid} jensen --disabled --events completed"
    )
    assert int(_get_sub(tid)["enabled"]) == 0

    out = kc.run_slash(f"profile-subs add {tid} jensen --enable")
    assert f"Updated profile-sub {tid}:jensen" in out
    row = _get_sub(tid)
    assert int(row["enabled"]) == 1
    # other fields preserved
    assert kb._parse_event_kinds_column(row["event_kinds"]) == ["completed"]


def test_profile_subs_add_disable_alias_matches_disabled(kanban_home):
    """`--disable` reaches the same code path as the legacy `--disabled`."""
    tid = _create_task()
    kc.run_slash(f"profile-subs add {tid} jensen")
    assert int(_get_sub(tid)["enabled"]) == 1
    kc.run_slash(f"profile-subs add {tid} jensen --disable")
    assert int(_get_sub(tid)["enabled"]) == 0


def test_profile_subs_add_no_wake_then_wake(kanban_home):
    """`--no-wake` -> `--wake` flips wake_agent without touching other fields."""
    tid = _create_task()
    kc.run_slash(
        f"profile-subs add {tid} jensen --no-wake --events completed"
    )
    assert int(_get_sub(tid)["wake_agent"]) == 0

    kc.run_slash(f"profile-subs add {tid} jensen --wake")
    row = _get_sub(tid)
    assert int(row["wake_agent"]) == 1
    assert kb._parse_event_kinds_column(row["event_kinds"]) == ["completed"]


def test_profile_subs_add_no_include_children_resets_to_false(kanban_home):
    """`--include-children` can be reverted with `--no-include-children`."""
    tid = _create_task()
    kc.run_slash(f"profile-subs add {tid} jensen --include-children")
    assert int(_get_sub(tid)["include_children"]) == 1

    kc.run_slash(f"profile-subs add {tid} jensen --no-include-children")
    assert int(_get_sub(tid)["include_children"]) == 0


def test_profile_subs_add_clear_wake_prompt(kanban_home):
    """`--clear-wake-prompt` resets the body back to the default (NULL)."""
    tid = _create_task()
    kc.run_slash(
        f"profile-subs add {tid} jensen --wake-prompt 'custom hint'"
    )
    assert _get_sub(tid)["wake_prompt"] == "custom hint"

    kc.run_slash(f"profile-subs add {tid} jensen --clear-wake-prompt")
    assert _get_sub(tid)["wake_prompt"] is None


def test_profile_subs_add_default_events_resets_to_null(kanban_home):
    """`--default-events` clears custom kinds (column goes back to NULL)."""
    tid = _create_task()
    kc.run_slash(
        f"profile-subs add {tid} jensen --events completed,blocked"
    )
    row = _get_sub(tid)
    assert kb._parse_event_kinds_column(row["event_kinds"]) == [
        "completed", "blocked",
    ]

    kc.run_slash(f"profile-subs add {tid} jensen --default-events")
    row = _get_sub(tid)
    # Stored as NULL means "use default terminal kinds" (parsed as None).
    assert row["event_kinds"] is None
    assert kb._parse_event_kinds_column(row["event_kinds"]) is None


def test_profile_subs_add_events_and_default_events_conflict(kanban_home):
    """The two event flags are mutually exclusive."""
    tid = _create_task()
    out = kc.run_slash(
        f"profile-subs add {tid} jensen --events completed --default-events"
    )
    assert "mutually exclusive" in out.lower()
    # nothing persisted
    with kb.connect() as conn:
        assert kb.list_profile_event_subs(conn, enabled_only=False) == []


def test_profile_subs_add_empty_update_preserves_all_fields(kanban_home):
    """Re-adding without any explicit flag must not touch existing values."""
    tid = _create_task()
    kc.run_slash(
        f"profile-subs add {tid} jensen --events completed --include-children "
        f"--no-wake --wake-prompt 'keep me' --disabled"
    )
    before = _get_sub(tid)

    kc.run_slash(f"profile-subs add {tid} jensen")
    after = _get_sub(tid)

    for field in (
        "event_kinds", "include_children", "wake_agent",
        "wake_prompt", "enabled",
    ):
        assert before[field] == after[field], (
            f"field {field!r} changed unexpectedly: "
            f"before={before[field]!r}, after={after[field]!r}"
        )


# ---------------------------------------------------------------------------
# /kanban help discoverability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rest", ["", "help", "--help", "-h", "?"])
def test_kanban_help_mentions_profile_subs(rest, kanban_home):
    out = kc.run_slash(rest)
    assert "profile-subs" in out, (
        f"/kanban {rest!r} help should advertise profile-subs; got:\n{out}"
    )


def test_profile_subs_add_help_documents_reverse_flags(kanban_home):
    out = kc.run_slash("profile-subs add --help")
    for flag in (
        "--enable", "--disable", "--wake", "--no-wake",
        "--include-children", "--no-include-children",
        "--wake-prompt", "--clear-wake-prompt",
        "--events", "--default-events",
    ):
        assert flag in out, f"profile-subs add help missing {flag}"


# ---------------------------------------------------------------------------
# DB helper: `event_kinds=None` now means "reset to default" (NULL)
# ---------------------------------------------------------------------------

def test_add_profile_event_sub_helper_resets_event_kinds_with_none(kanban_home):
    """The helper must accept an explicit NULL reset without clobbering peers."""
    tid = _create_task()
    with kb.connect() as conn:
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
            include_children=True,
            wake_agent=False,
            wake_prompt="keep me",
        )
        # Explicit reset of just event_kinds.
        kb.add_profile_event_sub(
            conn, task_id=tid, profile="jensen", event_kinds=None,
        )
        row = kb.list_profile_event_subs(
            conn, task_id=tid, profile="jensen", enabled_only=False,
        )[0]
    assert row["event_kinds"] is None
    # Other fields preserved.
    assert int(row["include_children"]) == 1
    assert int(row["wake_agent"]) == 0
    assert row["wake_prompt"] == "keep me"
