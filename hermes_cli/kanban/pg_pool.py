# hermes_cli/kanban/pg_pool.py
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

from psycopg_pool import ConnectionPool, PoolTimeout

_SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")

# Process-wide pools keyed by DSN so callers in one process share connections
# (Supabase transaction pooler handles cross-process fan-out).
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()

# getconn retry: ride through bursty Supabase-pooler PoolTimeouts. Total wait
# budget stays comparable to the historical single 30s (ATTEMPTS * per-attempt
# TIMEOUT + backoff), but split so a pooler that recovers mid-window is caught
# instead of hard-failing. Covers every store call site transparently because
# ConnectionPool.connection() delegates to getconn().
POOL_GETCONN_TIMEOUT = 10.0       # seconds, per attempt
POOL_GETCONN_ATTEMPTS = 3
_POOL_GETCONN_BACKOFF = (0.5, 1.0)  # sleeps between attempts; total ceiling ~31.5s


class _RetryingConnectionPool(ConnectionPool):
    """ConnectionPool that retries getconn on PoolTimeout with bounded backoff."""

    def getconn(self, timeout=None):
        """Retry getconn on PoolTimeout, but ONLY for default-timeout callers.

        An explicit ``timeout`` is honored as a caller-chosen ABSOLUTE deadline
        (single attempt, no retry) — fail-fast connectivity probes / liveness
        ticks (gateway, reconciler, board doctor) pass one and must not have it
        silently multiplied. Default-timeout callers (every normal store op via
        ``pool.connection()``) get the bounded retry: the wall-time ceiling is
        ``POOL_GETCONN_ATTEMPTS * POOL_GETCONN_TIMEOUT + sum(_POOL_GETCONN_BACKOFF)``
        (~31.5s), split so a pooler that recovers mid-window is caught.
        """
        if timeout is not None:
            return super().getconn(timeout=timeout)
        for attempt in range(POOL_GETCONN_ATTEMPTS):
            try:
                return super().getconn(timeout=POOL_GETCONN_TIMEOUT)
            except PoolTimeout:
                if attempt == POOL_GETCONN_ATTEMPTS - 1:
                    raise
                time.sleep(_POOL_GETCONN_BACKOFF[
                    min(attempt, len(_POOL_GETCONN_BACKOFF) - 1)])


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
    transaction-mode pooling.

    Pools also set ``check`` (drop dead pooler connections on checkout),
    ``max_lifetime``/``max_idle`` (recycle to survive Supavisor reaping), and a
    short per-attempt ``timeout``; the returned pool retries ``getconn`` on
    ``PoolTimeout`` (see ``_RetryingConnectionPool``)."""
    kwargs: dict = {"autocommit": True, "prepare_threshold": None}
    if search_path is not None:
        kwargs["options"] = f"-c search_path={search_path}"
    return _RetryingConnectionPool(
        conninfo=dsn, min_size=min_size, max_size=max_size,
        kwargs=kwargs, open=True,
        check=ConnectionPool.check_connection,
        max_lifetime=1800,   # recycle before Supavisor reaps long server sessions
        max_idle=300,        # release idle conns above min_size -> lower pooler footprint
        timeout=POOL_GETCONN_TIMEOUT,
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
