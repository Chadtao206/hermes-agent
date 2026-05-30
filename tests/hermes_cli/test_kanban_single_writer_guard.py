import pytest
from hermes_cli import kanban_db as kb


def test_writable_connect_blocked_in_client_when_flag_on(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.delenv("HERMES_KANBAN_WRITER_OWNER", raising=False)
    with pytest.raises(kb.DirectWriteForbidden):
        kb.connect(db_path=db, readonly=False)


def test_writable_connect_allowed_for_owner(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.setenv("HERMES_KANBAN_WRITER_OWNER", "1")
    conn = kb.connect(db_path=db, readonly=False)  # must not raise
    conn.close()


def test_writable_connect_allowed_with_bootstrap(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.delenv("HERMES_KANBAN_WRITER_OWNER", raising=False)
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)  # exempt
    conn.close()


def test_readonly_connect_always_allowed(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.delenv("HERMES_KANBAN_WRITER_OWNER", raising=False)
    kb.connect(db_path=db, readonly=False, _bootstrap=True).close()  # create file
    kb.connect(db_path=db, readonly=True).close()  # ro never blocked


def test_flag_off_allows_writable(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
    monkeypatch.delenv("HERMES_KANBAN_WRITER_OWNER", raising=False)
    kb.connect(db_path=db, readonly=False).close()  # flag off -> allowed
