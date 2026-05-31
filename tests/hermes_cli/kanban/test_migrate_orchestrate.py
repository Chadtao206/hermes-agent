import sqlite3
import psycopg
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


@pytest.fixture
def seeded_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_1", "a", "ready", 0, 1, "scratch"))
        c.commit()
    return tmp_path, p


def _schema_exists(dsn, name):
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute("SELECT 1 FROM information_schema.schemata "
                         "WHERE schema_name=%s", (name,)).fetchone() is not None


def test_dry_run_green_and_drops_schema(seeded_home, _pg_dsn):
    home, p = seeded_home
    report, schema = m.dry_run(_pg_dsn, "default", sqlite_path=p)
    assert report.ok, report.render()
    assert not _schema_exists(_pg_dsn, schema)  # cleaned up


def test_execute_then_guard_then_force(seeded_home, _pg_dsn):
    home, p = seeded_home
    name = "xtest_" + __import__("uuid").uuid4().hex[:8]
    try:
        r1 = m.execute(_pg_dsn, "default", sqlite_path=p, target_schema=name)
        assert r1.ok
        # second execute refuses (target now non-empty for the board)
        with pytest.raises(m.MigrationError) as ei:
            m.execute(_pg_dsn, "default", sqlite_path=p, target_schema=name)
        assert "force" in str(ei.value).lower()
        # --force succeeds and counts still match
        r3 = m.execute(_pg_dsn, "default", sqlite_path=p, target_schema=name,
                       force=True)
        assert r3.ok and r3.counts["tasks"] == (1, 1)
    finally:
        with psycopg.connect(_pg_dsn, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
