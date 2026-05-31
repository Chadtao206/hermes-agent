"""Read-only SQLite -> Postgres migrator for the kanban board (Phase 4).

Single-board only: refuses if kanban_db.list_boards() returns >1 board (the
PG IDENTITY ids are global across boards; multi-board remap is deferred).
Reads the source READ-ONLY; never mutates a source board DB.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb

# Parent-first load order (PG has no FK constraints, so order is cosmetic).
MIGRATED_TABLES: tuple[str, ...] = (
    "tasks", "task_runs", "task_events", "task_comments", "task_links",
    "kanban_notify_subs", "kanban_profile_event_subs",
    "kanban_profile_event_claims", "kanban_profile_wake_events",
)
# Reverse order for board-scoped deletes under --force.
DELETE_ORDER: tuple[str, ...] = tuple(reversed(MIGRATED_TABLES))
IDENTITY_TABLES: tuple[str, ...] = (
    "task_comments", "task_events", "task_runs", "kanban_profile_wake_events",
)
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "task_events": frozenset({"payload"}),
    "task_runs": frozenset({"metadata"}),
}


class MigrationError(Exception):
    """Fatal precondition failure (multi-board, bad source data, target guard)."""


def enumerate_board() -> str:
    """Return the single board slug to migrate, or raise if >1 board exists."""
    boards = kb.list_boards()
    if not boards:
        raise MigrationError("refusing to migrate: no boards found on disk.")
    if len(boards) > 1:
        slugs = ", ".join(sorted(b["slug"] for b in boards))
        raise MigrationError(
            f"refusing to migrate: found more than one board ({slugs}). "
            "The PG schema uses global IDENTITY ids; multi-board migration is "
            "deferred. Migrate with exactly one board on disk."
        )
    return boards[0]["slug"]
