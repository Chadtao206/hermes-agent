import uuid

import psycopg
import pytest
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban import migrate_sqlite_to_pg as m


@pytest.fixture
def schema(_pg_dsn):
    """A throwaway schema with the kanban DDL applied; dropped on teardown."""
    name = "mtest_" + uuid.uuid4().hex[:8]
    conn = psycopg.connect(_pg_dsn, autocommit=True)
    conn.execute(f'CREATE SCHEMA "{name}"')
    conn.execute(f'SET search_path TO "{name}"')
    conn.execute(pg_pool.read_schema_ddl())
    try:
        yield (_pg_dsn, name, conn)
    finally:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        conn.close()


def test_load_stamps_board_and_counts(schema):
    dsn, name, conn = schema
    data = {t: ([], []) for t in m.MIGRATED_TABLES}
    data["tasks"] = (["id", "title", "status", "priority", "created_at",
                      "workspace_kind"],
                     [{"id": "t_1", "title": "a", "status": "ready",
                       "priority": 0, "created_at": 1, "workspace_kind": "scratch"}])
    data["task_events"] = (["id", "task_id", "kind", "payload", "created_at"],
                           [{"id": 1, "task_id": "t_1", "kind": "created",
                             "payload": '{"k": 1}', "created_at": 1}])
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE board='default'").fetchone()[0] == 1
    # payload landed as JSONB
    assert conn.execute("SELECT payload->>'k' FROM task_events").fetchone()[0] == "1"
    # reseq: next id after a max of 1 is 2
    assert conn.execute("SELECT nextval(pg_get_serial_sequence('task_events','id'))").fetchone()[0] == 2


def test_reseq_empty_table_starts_at_one(schema):
    dsn, name, conn = schema
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.reseq(c)
        c.commit()
    assert conn.execute("SELECT nextval(pg_get_serial_sequence('task_runs','id'))").fetchone()[0] == 1
