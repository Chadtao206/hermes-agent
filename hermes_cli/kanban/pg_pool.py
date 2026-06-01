# hermes_cli/kanban/pg_pool.py
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import yaml

from psycopg_pool import ConnectionPool

_SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")

# Process-wide pools keyed by DSN so callers in one process share connections
# (Supabase transaction pooler handles cross-process fan-out).
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()


def resolve_dsn() -> str:
    """Resolve the Postgres DSN for the kanban board.

    Order: HERMES_KANBAN_PG_DSN env -> active config kanban.postgres.dsn ->
    default-root config kanban.postgres.dsn -> raise.
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
        try:
            home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
            root = home.parent.parent if home.parent.name == "profiles" else home
            cfg_path = root / "config.yaml"
            root_cfg = yaml.safe_load(cfg_path.read_text()) or {}
            pg = ((root_cfg.get("kanban") or {}).get("postgres") or {})
            dsn = pg.get("dsn")
        except Exception:
            dsn = None
    if not dsn:
        raise RuntimeError(
            "kanban backend=postgres but no DSN configured "
            "(set HERMES_KANBAN_PG_DSN or kanban.postgres.dsn)"
        )
    return dsn


def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 8,
              search_path: Optional[str] = None) -> ConnectionPool:
    """Create a bounded psycopg ConnectionPool. autocommit=True: each store op
    manages its own transaction explicitly via `with conn.transaction():`.
    `search_path`, when given, pins every connection's schema search path
    (used by the migrator's dry-run parity read). Must not contain spaces
    (libpq splits options on whitespace); use the "schema,public" form.

    `prepare_threshold=None` disables psycopg's server-side prepared statements:
    Supabase's transaction pooler (Supavisor) does not pin a backend across
    transactions, so auto-prepared statements collide (`prepared statement
    "_pg3_N" already exists`). Disabling them is the supported pattern for
    transaction-mode pooling."""
    kwargs: dict = {"autocommit": True, "prepare_threshold": None}
    if search_path is not None:
        kwargs["options"] = f"-c search_path={search_path}"
    return ConnectionPool(
        conninfo=dsn, min_size=min_size, max_size=max_size,
        kwargs=kwargs, open=True,
    )


def read_schema_ddl() -> str:
    """Return the kanban Postgres DDL text (pg_schema.sql)."""
    return _SCHEMA_PATH.read_text()


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
