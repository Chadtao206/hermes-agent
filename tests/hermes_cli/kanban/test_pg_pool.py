"""DB-free unit tests for the kanban PG pool wrapper.

These never open a real connection: the retry tests monkeypatch the base
``ConnectionPool.getconn`` (so ``super().getconn`` in the subclass resolves to
the fake), and the kwargs test relies on ``ConnectionPool(open=True)`` returning
immediately without blocking on a reachable server (background connect attempts
to the unroutable DSN fail silently and are irrelevant to the assertions)."""
import pytest
from psycopg_pool import ConnectionPool, PoolTimeout

from hermes_cli.kanban import pg_pool
from hermes_cli.kanban.pg_pool import (
    POOL_GETCONN_ATTEMPTS,
    _RetryingConnectionPool,
)

_DEAD_DSN = "postgresql://u:***@127.0.0.1:1/db"  # port 1 -> connection refused fast


def test_getconn_retries_then_succeeds(monkeypatch):
    sentinel = object()
    calls = {"n": 0}

    def fake_getconn(self, timeout=None):
        calls["n"] += 1
        if calls["n"] < POOL_GETCONN_ATTEMPTS:
            raise PoolTimeout("transient")
        return sentinel

    monkeypatch.setattr(ConnectionPool, "getconn", fake_getconn)
    monkeypatch.setattr(pg_pool.time, "sleep", lambda *_: None)

    pool = _RetryingConnectionPool(conninfo=_DEAD_DSN, open=False)
    try:
        assert pool.getconn() is sentinel
        assert calls["n"] == POOL_GETCONN_ATTEMPTS
    finally:
        pool.close()


def test_getconn_raises_after_exhausting_attempts(monkeypatch):
    calls = {"n": 0}

    def always_timeout(self, timeout=None):
        calls["n"] += 1
        raise PoolTimeout("still saturated")

    monkeypatch.setattr(ConnectionPool, "getconn", always_timeout)
    monkeypatch.setattr(pg_pool.time, "sleep", lambda *_: None)

    pool = _RetryingConnectionPool(conninfo=_DEAD_DSN, open=False)
    try:
        with pytest.raises(PoolTimeout):
            pool.getconn()
        assert calls["n"] == POOL_GETCONN_ATTEMPTS
    finally:
        pool.close()


def test_make_pool_sets_resilience_kwargs():
    pool = pg_pool.make_pool(_DEAD_DSN)
    try:
        assert isinstance(pool, _RetryingConnectionPool)
        assert pool.max_lifetime == 1800
        assert pool.max_idle == 300
        assert pool.timeout == pg_pool.POOL_GETCONN_TIMEOUT
        assert pool._check is ConnectionPool.check_connection
    finally:
        pool.close()


def test_getconn_with_explicit_timeout_does_not_retry(monkeypatch):
    # An explicit timeout is an absolute deadline (fail-fast probes); it must be
    # a single attempt, NOT multiplied by the retry loop.
    calls = {"n": 0}

    def always_timeout(self, timeout=None):
        calls["n"] += 1
        raise PoolTimeout("saturated")

    monkeypatch.setattr(ConnectionPool, "getconn", always_timeout)
    monkeypatch.setattr(pg_pool.time, "sleep", lambda *_: None)

    pool = _RetryingConnectionPool(conninfo=_DEAD_DSN, open=False)
    try:
        with pytest.raises(PoolTimeout):
            pool.getconn(timeout=1)
        assert calls["n"] == 1  # exactly one attempt, no retry
    finally:
        pool.close()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.calls.append("tx_enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.calls.append("tx_exit")
        return False


class _ConnectionContext:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSchemaConn:
    def __init__(self, *, current: bool | None = None,
                 tables_current: bool | None = None,
                 columns_current: bool | None = None):
        if current is not None:
            tables_current = current
            columns_current = current
        self.tables_current = bool(tables_current)
        self.columns_current = bool(columns_current)
        self.calls = []

    def transaction(self):
        return _Tx(self)

    def execute(self, query, params=None):
        text = str(query).lower()
        if "information_schema.tables" in text:
            self.calls.append("check_tables")
            rows = [(name,) for name in pg_pool._REQUIRED_TABLES] if self.tables_current else []
            return _Rows(rows)
        if "information_schema.columns" in text:
            self.calls.append("check_columns")
            rows = [(name,) for name in pg_pool._REQUIRED_TASK_COLUMNS] if self.columns_current else []
            return _Rows(rows)
        if "pg_advisory_xact_lock" in text:
            self.calls.append("advisory_lock")
            return _Rows([])
        self.calls.append("ddl")
        self.tables_current = True
        self.columns_current = True
        return _Rows([])


class _FakeSchemaPool:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        return _ConnectionContext(self.conn)


def test_ensure_schema_current_schema_skips_ddl_and_caches_pool():
    conn = _FakeSchemaConn(current=True)
    pool = _FakeSchemaPool(conn)
    pg_pool._SCHEMA_DONE.clear()
    try:
        pg_pool.ensure_schema(pool)  # type: ignore[arg-type]
        pg_pool.ensure_schema(pool)  # type: ignore[arg-type]
    finally:
        pg_pool._SCHEMA_DONE.clear()

    assert conn.calls == ["check_tables", "check_columns"]


def test_ensure_schema_serializes_ddl_when_schema_not_current():
    conn = _FakeSchemaConn(current=False)
    pool = _FakeSchemaPool(conn)
    pg_pool._SCHEMA_DONE.clear()
    try:
        pg_pool.ensure_schema(pool)  # type: ignore[arg-type]
    finally:
        pg_pool._SCHEMA_DONE.clear()

    assert conn.calls == [
        "check_tables",
        "tx_enter",
        "advisory_lock",
        "check_tables",
        "ddl",
        "tx_exit",
    ]
    assert conn.tables_current is True
    assert conn.columns_current is True


def test_ensure_schema_handles_column_only_drift_under_lock():
    conn = _FakeSchemaConn(tables_current=True, columns_current=False)
    pool = _FakeSchemaPool(conn)
    pg_pool._SCHEMA_DONE.clear()
    try:
        pg_pool.ensure_schema(pool)  # type: ignore[arg-type]
    finally:
        pg_pool._SCHEMA_DONE.clear()

    assert conn.calls == [
        "check_tables",
        "check_columns",
        "tx_enter",
        "advisory_lock",
        "check_tables",
        "check_columns",
        "ddl",
        "tx_exit",
    ]
    assert conn.tables_current is True
    assert conn.columns_current is True
