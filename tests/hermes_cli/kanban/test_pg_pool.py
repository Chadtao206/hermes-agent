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

_DEAD_DSN = "postgresql://u:p@127.0.0.1:1/db"  # port 1 -> connection refused fast


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
