import sqlite3

import pytest
from fastapi import HTTPException

from plugins.kanban.dashboard import plugin_api


def test_conn_converts_corrupt_db_connect_failure_to_503(monkeypatch, tmp_path):
    """Dashboard board API must fail closed instead of surfacing a raw 500."""
    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")

    monkeypatch.setattr(plugin_api.kanban_db, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(plugin_api.kanban_db, "init_db", lambda board=None: None)

    def _connect(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(plugin_api.kanban_db, "connect", _connect)

    with pytest.raises(HTTPException) as excinfo:
        plugin_api._conn()

    assert excinfo.value.status_code == 503
    assert "quiesced repair" in str(excinfo.value.detail)


def test_readonly_conn_converts_invalid_db_preflight_to_503(monkeypatch, tmp_path):
    """Read-only dashboard paths should also return controlled unavailable errors."""
    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")

    monkeypatch.setattr(plugin_api.kanban_db, "kanban_db_path", lambda board=None: db_path)

    def _init_db(*args, **kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(plugin_api.kanban_db, "init_db", _init_db)

    with pytest.raises(HTTPException) as excinfo:
        plugin_api._conn(readonly=True)

    assert excinfo.value.status_code == 503
    assert "unavailable or corrupt" in str(excinfo.value.detail)
