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


def test_make_online_backup_opens_source_read_only(tmp_path, monkeypatch):
    """The online backup must open the live DB read-only, never a second
    writable connection.

    A raw writable ``sqlite3.connect(db_path)`` bypasses the single-writer guard
    and adds concurrent-connection pressure to the hot WAL board — a trigger for
    the transient ``disk I/O error`` the single-writer daemon is meant to avoid.
    The backup API only needs a read-only source.
    """
    db = tmp_path / "kanban.db"
    _make_good_db(db)

    real_connect = sqlite3.connect
    opened: list[str] = []

    def spy_connect(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    out = rec.make_online_backup(db, tmp_path / "backups", keep=3)
    assert out is not None

    # Every connection opened against the live source path must be read-only.
    src_opens = [t for t in opened if t.startswith(f"file:{db}") or t == str(db)]
    assert src_opens, f"expected a connect to source {db}; saw {opened}"
    assert all("mode=ro" in t for t in src_opens), (
        f"online backup must open the live DB read-only; got {src_opens}"
    )


def test_make_online_backup_produces_healthy_backup(tmp_path):
    """Regression: switching the source to read-only must still yield a valid,
    consistent backup of the live data."""
    db = tmp_path / "kanban.db"
    _make_good_db(db)
    out = rec.make_online_backup(db, tmp_path / "backups", keep=3)
    assert out is not None and out.exists()
    ro = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    try:
        assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert ro.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()[0] == "keep"
    finally:
        ro.close()


def test_recover_reports_exhausted_when_no_backup_and_recover_fails(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _make_good_db(db)
    _corrupt_in_place(db)
    monkeypatch.setattr(rec, "_try_sqlite_recover", lambda *a, **k: False)
    result = rec.recover_board(db, backup_dir=tmp_path / "empty", keep=3)
    assert result.healed is False
    assert result.method == "exhausted"
