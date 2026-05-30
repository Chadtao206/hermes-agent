import sqlite3
from pathlib import Path
import pytest
from hermes_cli import kanban_recovery as rec
from hermes_cli import kanban_db as kb


def _make_good_db(path: Path):
    conn = kb.connect(db_path=path, readonly=False, _bootstrap=True)
    conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t1','keep','todo',0)")
    conn.commit(); conn.close()


def _corrupt_in_place(path: Path):
    data = bytearray(path.read_bytes())
    for i in range(100, 1600):  # smash schema/B-tree area; 800-1600 is insufficient on this SQLite build
        data[i] = 0xFF
    path.write_bytes(bytes(data))


def test_is_corruption_signal():
    assert rec.is_corruption_signal(sqlite3.DatabaseError("database disk image is malformed"))
    assert rec.is_corruption_signal(sqlite3.DatabaseError("file is not a database"))
    assert not rec.is_corruption_signal(ValueError("nope"))


def test_recover_restores_from_backup_when_recover_fails(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _make_good_db(db)
    backup_dir = tmp_path
    rec.make_online_backup(db, backup_dir, keep=3)          # snapshot the good state
    _corrupt_in_place(db)                                   # break the live file
    monkeypatch.setattr(rec, "_try_sqlite_recover", lambda *a, **k: False)  # force restore path
    result = rec.recover_board(db, backup_dir=backup_dir, keep=3)
    assert result.healed is True
    assert result.method == "restore_from_backup"
    ro = kb.connect(db_path=db, readonly=True)
    assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ro.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()["title"] == "keep"


def test_recover_reports_exhausted_when_no_backup_and_recover_fails(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _make_good_db(db)
    _corrupt_in_place(db)
    monkeypatch.setattr(rec, "_try_sqlite_recover", lambda *a, **k: False)
    result = rec.recover_board(db, backup_dir=tmp_path / "empty", keep=3)
    assert result.healed is False
    assert result.method == "exhausted"
