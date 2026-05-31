from hermes_cli.kanban import pg_pool


def test_read_schema_ddl_has_tables():
    ddl = pg_pool.read_schema_ddl()
    assert "CREATE TABLE" in ddl and "tasks" in ddl


def test_make_pool_sets_search_path(_pg_dsn):
    pool = pg_pool.make_pool(_pg_dsn, search_path="pg_temp,public")
    try:
        with pool.connection() as conn:
            got = conn.execute("SHOW search_path").fetchone()[0]
        assert "pg_temp" in got
    finally:
        pool.close()
