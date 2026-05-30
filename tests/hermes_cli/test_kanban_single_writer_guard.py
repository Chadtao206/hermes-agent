import pytest
from hermes_cli import kanban_db as kb


def test_writable_connect_blocked_in_client_when_flag_on(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    with pytest.raises(kb.DirectWriteForbidden):
        kb.connect(db_path=db, readonly=False)


def test_writable_connect_allowed_in_writer_thread(tmp_path, monkeypatch):
    from hermes_cli import kanban_writer_daemon as wd
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    wd._WRITER_TLS.is_writer = True
    try:
        conn = kb.connect(db_path=db, readonly=False)  # writer thread -> allowed
        conn.close()
    finally:
        wd._WRITER_TLS.is_writer = False


def test_writable_connect_allowed_with_bootstrap(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)  # exempt
    conn.close()


def test_readonly_connect_always_allowed(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    kb.connect(db_path=db, readonly=False, _bootstrap=True).close()  # create file
    kb.connect(db_path=db, readonly=True).close()  # ro never blocked


def test_flag_off_allows_writable(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: False)
    kb.connect(db_path=db, readonly=False).close()  # flag off -> allowed
