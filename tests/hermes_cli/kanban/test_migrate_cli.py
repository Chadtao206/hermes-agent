import os
import sqlite3
import subprocess
import sys
from pathlib import Path
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
    return tmp_path


def test_main_requires_mode(seeded_home, _pg_dsn, capsys):
    rc = m.main(["--dsn", _pg_dsn])
    assert rc == 2


def test_main_dry_run_ok(seeded_home, _pg_dsn):
    rc = m.main(["--dry-run", "--dsn", _pg_dsn])
    assert rc == 0


def test_main_json_output(seeded_home, _pg_dsn, capsys):
    rc = m.main(["--dry-run", "--dsn", _pg_dsn, "--json"])
    out = capsys.readouterr().out
    import json
    assert rc == 0 and json.loads(out)["ok"] is True


def test_module_runs_as_main_subprocess(seeded_home, _pg_dsn):
    # Runs the CLI exactly as an operator would (python -m ...), which executes
    # the `if __name__ == "__main__"` guard. This catches definition-order bugs
    # that import-based tests miss.
    env = {**os.environ, "HERMES_KANBAN_HOME": str(seeded_home)}
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.kanban.migrate_sqlite_to_pg",
         "--dry-run", "--dsn", _pg_dsn],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
