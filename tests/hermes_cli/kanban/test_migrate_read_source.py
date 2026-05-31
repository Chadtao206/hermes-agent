import sqlite3
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


def _src(tmp_path):
    p = tmp_path / "kanban.db"
    kb.connect(db_path=p, readonly=False, _bootstrap=True).close()
    return p


def test_read_source_returns_cols_and_rows(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_aaa", "hello", "ready", 0, 1700000000, "scratch"))
        c.commit()
    data = m.read_source(p)
    cols, rows = data["tasks"]
    assert "id" in cols and "title" in cols
    assert len(rows) == 1 and rows[0]["id"] == "t_aaa" and rows[0]["title"] == "hello"
    # untouched tables come back empty
    assert data["task_events"][1] == []


def test_read_source_rejects_non_utf8(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        # title holds invalid UTF-8 bytes
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_bad", b"\xff\xfe", "ready", 0, 1700000000, "scratch"))
        c.commit()
    with pytest.raises(m.MigrationError) as ei:
        m.read_source(p)
    assert "non-utf-8" in str(ei.value).lower() and "t_bad" in str(ei.value)


def test_read_source_rejects_bad_json(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_e", "x", "done", 0, 1700000000, "scratch"))
        c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                  "VALUES (?,?,?,?)", ("t_e", "created", "{not json", 1700000000))
        c.commit()
    with pytest.raises(m.MigrationError) as ei:
        m.read_source(p)
    assert "json" in str(ei.value).lower()


def test_read_source_missing_table_raises_migration_error(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        c.execute("DROP TABLE kanban_profile_wake_events")
        c.commit()
    with pytest.raises(m.MigrationError) as ei:
        m.read_source(p)
    assert "missing table" in str(ei.value).lower()
    assert "kanban_profile_wake_events" in str(ei.value)


def test_read_source_reports_all_offenders(tmp_path):
    p = _src(tmp_path)
    with sqlite3.connect(p) as c:
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_u", b"\xff", "ready", 0, 1, "scratch"))
        c.execute("INSERT INTO tasks (id,title,status,priority,created_at,"
                  "workspace_kind) VALUES (?,?,?,?,?,?)",
                  ("t_j", "ok", "done", 0, 1, "scratch"))
        c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) "
                  "VALUES (?,?,?,?)", ("t_j", "created", "{bad", 1))
        c.commit()
    with pytest.raises(m.MigrationError) as ei:
        m.read_source(p)
    msg = str(ei.value)
    assert "non-utf-8" in msg.lower() and "t_u" in msg      # the bad-UTF-8 offender
    assert "json" in msg.lower()                             # the bad-JSON offender
