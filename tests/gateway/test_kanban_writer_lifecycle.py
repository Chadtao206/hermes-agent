import time
from pathlib import Path
import gateway.run as gr
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_db as kb


def test_spawn_writer_daemons_starts_registers_and_serves(tmp_path):
    db = tmp_path / "kanban.db"
    started = gr._spawn_writer_daemons([db], auto_recovery=False)
    try:
        assert len(started) == 1
        assert wd.lookup_daemon(db) is started[0]
        # the daemon serves writes (in-process execute)
        new_id = started[0].execute("create_task", title="lc", assignee="engineer")
        ro = kb.connect(db_path=db, readonly=True)
        assert ro.execute("SELECT title FROM tasks WHERE id=?", (new_id,)).fetchone()["title"] == "lc"
    finally:
        for d in started:
            wd.unregister_daemon(d.db_path); d.shutdown()
    assert wd.lookup_daemon(db) is None


def test_spawn_writer_daemons_dedups_by_resolved_path(tmp_path):
    db = tmp_path / "kanban.db"
    started = gr._spawn_writer_daemons([db, db], auto_recovery=False)  # same path twice
    try:
        assert len(started) == 1  # deduped
    finally:
        for d in started:
            wd.unregister_daemon(d.db_path); d.shutdown()
