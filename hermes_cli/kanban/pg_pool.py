# hermes_cli/kanban/pg_pool.py
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from psycopg_pool import ConnectionPool

_SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")

# Process-wide pools keyed by DSN so callers in one process share connections
# (Supabase transaction pooler handles cross-process fan-out).
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()


def resolve_dsn() -> str:
    """Resolve the Postgres DSN for the kanban board.

    Order: HERMES_KANBAN_PG_DSN env -> config kanban.postgres.dsn -> raise.
    (Supabase: use the transaction-pooler connection string. No LISTEN/NOTIFY
    is used, so transaction-mode pooling is safe.)
    """
    dsn = os.environ.get("HERMES_KANBAN_PG_DSN")
    if dsn:
        return dsn
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        pg = ((cfg.get("kanban") or {}).get("postgres") or {})
        dsn = pg.get("dsn")
    except Exception:
        dsn = None
    if not dsn:
        raise RuntimeError(
            "kanban backend=postgres but no DSN configured "
            "(set HERMES_KANBAN_PG_DSN or kanban.postgres.dsn)"
        )
    return dsn


def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    """Create a bounded psycopg ConnectionPool. autocommit=True: each store op
    manages its own transaction explicitly via `with conn.transaction():`."""
    return ConnectionPool(
        conninfo=dsn, min_size=min_size, max_size=max_size,
        kwargs={"autocommit": True}, open=True,
    )


def get_pool(dsn: Optional[str] = None) -> ConnectionPool:
    """Return the shared pool for a DSN (resolving from config/env if omitted)."""
    dsn = dsn or resolve_dsn()
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = make_pool(dsn)
            _POOLS[dsn] = pool
    return pool


def ensure_schema(pool: ConnectionPool) -> None:
    """Apply pg_schema.sql once per pool (idempotent CREATE ... IF NOT EXISTS)."""
    key = str(id(pool))
    if key in _SCHEMA_DONE:
        return
    ddl = _SCHEMA_PATH.read_text()
    with pool.connection() as conn:
        conn.execute(ddl)
    _SCHEMA_DONE.add(key)
