"""Postgres fixtures for the kanban dashboard plugin tests.

The store conformance suite's _pg_dsn lives under tests/hermes_cli/kanban/ and
is not visible here, so we provide an equivalent: HERMES_PG_TEST_DSN if set,
else a throwaway docker postgres:16-alpine container.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _pg_dsn():
    dsn = os.environ.get("HERMES_PG_TEST_DSN")
    if dsn:
        yield dsn
        return
    if not shutil.which("docker"):
        pytest.skip("docker not available and HERMES_PG_TEST_DSN unset")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
    except Exception:
        pytest.skip("docker not usable and HERMES_PG_TEST_DSN unset")
    name = f"hermes-kanban-dashpgtest-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name,
             "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=kanban",
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
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def _load_plugin_router():
    """Load plugins/kanban/dashboard/plugin_api.py by path (mirrors production)."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def pg_board(monkeypatch, _pg_dsn):
    """A clean Postgres 'default' board + a PostgresKanbanStore bound to it.

    Production is single-board 'default'; the dashboard's board=None path
    resolves to 'default', so tests seed and read 'default'. Rows for
    board='default' are deleted up-front for isolation across tests.
    """
    from hermes_cli.kanban import pg_pool
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    pool = pg_pool.make_pool(_pg_dsn)
    pg_pool.ensure_schema(pool)
    with pool.connection() as conn, conn.cursor() as cur:
        for tbl in ("task_events", "task_comments", "task_runs", "task_links",
                    "kanban_profile_wake_events", "kanban_profile_event_subs", "tasks"):
            cur.execute(f"DELETE FROM {tbl} WHERE board=%s", ("default",))
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    monkeypatch.setenv("HERMES_KANBAN_PG_DSN", _pg_dsn)
    monkeypatch.setattr(pg_pool, "get_pool", lambda *a, **k: pool)
    store = PostgresKanbanStore(board="default", pool=pool)
    try:
        yield store
    finally:
        store.close()
        pool.close()


@pytest.fixture
def pg_client(pg_board, tmp_path, monkeypatch):
    """A TestClient whose dashboard resolves backend=postgres (board 'default')."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    # HERMES_HOME isolation so any incidental sqlite path is a throwaway tmp dir.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    client = TestClient(app)
    client.pg_store = pg_board  # convenience handle for seeding
    return client
