# tests/hermes_cli/kanban/conftest.py
import os
import shutil
import subprocess
import time
import uuid

import pytest

_BACKENDS = ["sqlite"]
if os.environ.get("HERMES_PG_TEST_DSN") or shutil.which("docker"):
    _BACKENDS.append("postgres")


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], check=True,
                       capture_output=True, timeout=15)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def _pg_dsn():
    """Session-wide Postgres DSN. Uses HERMES_PG_TEST_DSN if set; else starts a
    throwaway docker postgres:16-alpine container and tears it down at session end."""
    dsn = os.environ.get("HERMES_PG_TEST_DSN")
    if dsn:
        yield dsn
        return
    if not _docker_available():
        pytest.skip("docker not available and HERMES_PG_TEST_DSN unset")
    name = f"hermes-kanban-pgtest-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name,
             "-e", "POSTGRES_PASSWORD=postgres",
             "-e", "POSTGRES_DB=kanban",
             "-P", "postgres:16-alpine"],
            check=True, capture_output=True, timeout=120,
        )
        out = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        port = int(out.rsplit(":", 1)[-1])
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/kanban"
        import psycopg
        waited = 0
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=3):
                    break
            except Exception:
                if waited >= 60:
                    raise
                time.sleep(1.0)
                waited += 1
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, timeout=30)


@pytest.fixture(params=_BACKENDS)
def store(request, tmp_path, monkeypatch):
    backend = request.param
    if backend == "sqlite":
        db = tmp_path / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
        from hermes_cli import kanban_db as kb
        kb.connect(db_path=db, readonly=False, _bootstrap=True).close()
        from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
        s = SqliteKanbanStore(board=None)
        try:
            yield s
        finally:
            s.close()
    elif backend == "postgres":
        dsn = request.getfixturevalue("_pg_dsn")
        from hermes_cli.kanban import pg_pool
        from hermes_cli.kanban.store_postgres import PostgresKanbanStore
        board = f"test_{uuid.uuid4().hex[:8]}"
        pool = pg_pool.make_pool(dsn)
        pg_pool.ensure_schema(pool)
        s = PostgresKanbanStore(board=board, pool=pool)
        try:
            yield s
        finally:
            s.close()
            pool.close()
    else:
        pytest.skip(f"backend {backend} not available")
