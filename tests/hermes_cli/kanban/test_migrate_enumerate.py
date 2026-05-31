import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import migrate_sqlite_to_pg as m


def _bootstrap_default(home):
    """Create the default board DB at <home>/kanban.db."""
    kb.connect(db_path=home / "kanban.db", readonly=False, _bootstrap=True).close()


def test_enumerate_single_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    _bootstrap_default(tmp_path)
    assert m.enumerate_board() == "default"


def test_enumerate_refuses_multi_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    _bootstrap_default(tmp_path)
    b2 = tmp_path / "kanban" / "boards" / "second" / "kanban.db"
    b2.parent.mkdir(parents=True)
    kb.connect(db_path=b2, readonly=False, _bootstrap=True).close()
    with pytest.raises(m.MigrationError) as ei:
        m.enumerate_board()
    assert "more than one board" in str(ei.value).lower()
    assert "second" in str(ei.value)
