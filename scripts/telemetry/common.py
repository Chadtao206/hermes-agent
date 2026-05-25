#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from init_self_improvement_db import (
    DEFAULT_TELEMETRY_ROOT,
    EVENTS_DB_NAME,
    EXPERIMENTS_DB_NAME,
    EVENTS_INDEXES,
    EVENTS_SCHEMA,
    EVENTS_SCHEMA_VERSION,
    EXPERIMENTS_INDEXES,
    EXPERIMENTS_SCHEMA,
    EXPERIMENTS_SCHEMA_VERSION,
    ensure_directories,
    initialize_db,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_telemetry_root(raw: str | None = None) -> Path:
    value = raw or str(DEFAULT_TELEMETRY_ROOT)
    return Path(os.path.expanduser(value)).resolve()


def ensure_initialized(telemetry_root: Path) -> None:
    ensure_directories(telemetry_root)
    initialize_db(telemetry_root / EVENTS_DB_NAME, EVENTS_SCHEMA, EVENTS_INDEXES, EVENTS_SCHEMA_VERSION)
    initialize_db(telemetry_root / EXPERIMENTS_DB_NAME, EXPERIMENTS_SCHEMA, EXPERIMENTS_INDEXES, EXPERIMENTS_SCHEMA_VERSION)


@contextmanager
def events_connection(telemetry_root: Path):
    ensure_initialized(telemetry_root)
    conn = sqlite3.connect(telemetry_root / EVENTS_DB_NAME)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def experiments_connection(telemetry_root: Path):
    ensure_initialized(telemetry_root)
    conn = sqlite3.connect(telemetry_root / EXPERIMENTS_DB_NAME)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def append_jsonl(telemetry_root: Path, record: dict[str, Any], filename: str = "events.jsonl") -> Path:
    ensure_directories(telemetry_root)
    path = telemetry_root / filename
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def parse_json_input(raw: str | None, stdin_fallback: bool = True) -> dict[str, Any]:
    if raw:
        return json.loads(raw)
    if stdin_fallback:
        data = input_stream_text()
        if data.strip():
            return json.loads(data)
    raise ValueError("JSON input required via --json or stdin")


def input_stream_text() -> str:
    import sys

    return sys.stdin.read()


def canonical_profile(raw: Any, *, default: str = "default") -> str:
    """Return the canonical telemetry profile identity.

    Jensen is the persona of the default profile. Telemetry should use one
    stable machine identity (`default`) so metrics do not split across
    `default`, `Jensen`, `jensen`, or display aliases such as `CEO`.
    """
    if raw is None:
        return default
    value = str(raw).strip()
    if not value:
        return default
    lowered = value.lower()
    if lowered in {"default", "jensen", "ceo"}:
        return "default"
    return value


def canonical_profiles(values: Iterable[Any] | None) -> list[str]:
    """Normalize and de-duplicate a list of profile labels for telemetry."""
    if not values:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = canonical_profile(item)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def fetch_one(conn: sqlite3.Connection, query: str, params: Iterable[Any]) -> Any:
    row = conn.execute(query, tuple(params)).fetchone()
    return row[0] if row else None
