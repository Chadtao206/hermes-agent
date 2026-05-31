import psycopg
import pytest
from hermes_cli.kanban import pg_pool
from hermes_cli.kanban import migrate_sqlite_to_pg as m
from tests.hermes_cli.kanban.test_migrate_load import schema  # reuse fixture


def _data_one_task_one_event():
    data = {t: ([], []) for t in m.MIGRATED_TABLES}
    data["tasks"] = (["id", "title", "status", "priority", "created_at",
                      "workspace_kind"],
                     [{"id": "t_1", "title": "a", "status": "ready",
                       "priority": 0, "created_at": 1, "workspace_kind": "scratch"}])
    data["task_events"] = (["id", "task_id", "kind", "payload", "created_at"],
                           [{"id": 1, "task_id": "t_1", "kind": "created",
                             "payload": None, "created_at": 1}])
    return data


def test_verify_ok_after_clean_load(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert report.ok, report.render()
    assert report.counts["task_events"] == (1, 1)


def test_verify_detects_count_mismatch(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    # claim the source says 2 events though only 1 was loaded
    data["task_events"] = (data["task_events"][0],
                           data["task_events"][1] + [{"id": 2, "task_id": "t_1",
                            "kind": "x", "payload": None, "created_at": 2}])
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert not report.ok
    assert any("task_events" in mm for mm in report.count_mismatches)


def test_verify_detects_orphan_event(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    # event references a task that is NOT loaded -> orphan
    data["task_events"][1][0]["task_id"] = "t_ghost"
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert not report.ok
    assert any("orphan" in f.lower() and "task_events" in f
               for f in report.integrity_failures)


def test_verify_detects_bad_sequence(schema):
    dsn, name, conn = schema
    data = _data_one_task_one_event()
    with psycopg.connect(dsn, autocommit=False) as c:
        c.execute(f'SET search_path TO "{name}"')
        m.load(c, "default", data)
        m.reseq(c)
        c.commit()
    # Corrupt the task_events sequence so it no longer matches max(id)=1.
    conn.execute(
        "SELECT setval(pg_get_serial_sequence('task_events','id'), 99, true)")
    report = m.verify(data, dsn, name, "default", check_parity=False)
    assert not report.ok
    assert any("task_events" in f for f in report.idseq_failures)
