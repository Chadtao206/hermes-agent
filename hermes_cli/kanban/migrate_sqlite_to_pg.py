"""Read-only SQLite -> Postgres migrator for the kanban board (Phase 4).

Single-board only: refuses if kanban_db.list_boards() returns >1 board (the
PG IDENTITY ids are global across boards; multi-board remap is deferred).
Reads the source READ-ONLY; never mutates a source board DB.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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


def _decode_and_validate(table: str, row: dict, errors: list[str]) -> dict:
    out: dict = {}
    rid = (row.get("id") or row.get("task_id") or row.get("event_id")
           or row.get("parent_id") or "<unknown>")
    for col, val in row.items():
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{table}(id={rid!r}).{col}: non-utf-8 bytes")
                val = val.decode("utf-8", "replace")
        out[col] = val
    for jc in JSON_COLUMNS.get(table, ()):  # validate JSON parseability
        v = out.get(jc)
        if v in (None, ""):
            out[jc] = None
            continue
        try:
            json.loads(v)
        except (ValueError, TypeError) as e:
            errors.append(f"{table}(id={rid!r}).{jc}: invalid JSON ({e})")
    return out


def read_source(sqlite_path: Path) -> dict[str, tuple[list[str], list[dict]]]:
    """Read all 9 migrated tables READ-ONLY. Decode TEXT as strict UTF-8 and
    validate JSON columns; collect ALL offenders and raise (no partial output)."""
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    con.text_factory = bytes  # surface non-UTF-8 so we can detect it
    con.row_factory = sqlite3.Row
    errors: list[str] = []
    data: dict[str, tuple[list[str], list[dict]]] = {}
    try:
        for table in MIGRATED_TABLES:
            info = con.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                raise MigrationError(
                    f"source DB is missing table {table!r}; run the kanban DB "
                    "migrations on the source before migrating.")
            cols = [r["name"].decode() if isinstance(r["name"], bytes) else r["name"]
                    for r in info]
            rows = [
                _decode_and_validate(table, {c: r[c] for c in cols}, errors)
                for r in con.execute(f"SELECT {', '.join(cols)} FROM {table}")
            ]
            data[table] = (cols, rows)
    finally:
        con.close()
    if errors:
        raise MigrationError(
            "source contains data Postgres cannot store; scrub the source and "
            "re-run. Offenders:\n  " + "\n  ".join(errors))
    return data
