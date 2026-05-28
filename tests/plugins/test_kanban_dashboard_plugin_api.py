import asyncio
import sqlite3
from types import SimpleNamespace

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


def test_get_board_uses_readonly_conn(monkeypatch):
    calls: list[bool] = []

    def _recording_conn(*args, **kwargs):
        calls.append(bool(kwargs.get("readonly", False)))
        raise RuntimeError("stop")

    monkeypatch.setattr(plugin_api, "_conn", _recording_conn)

    with pytest.raises(RuntimeError, match="stop"):
        plugin_api.get_board(board=None)

    assert calls == [True]


def test_get_task_uses_readonly_conn(monkeypatch):
    calls: list[bool] = []

    def _recording_conn(*args, **kwargs):
        calls.append(bool(kwargs.get("readonly", False)))
        raise RuntimeError("stop")

    monkeypatch.setattr(plugin_api, "_conn", _recording_conn)

    with pytest.raises(RuntimeError, match="stop"):
        plugin_api.get_task("t_demo", board=None)

    assert calls == [True]


def test_create_task_uses_writable_conn(monkeypatch):
    calls: list[bool] = []

    def _recording_conn(*args, **kwargs):
        calls.append(bool(kwargs.get("readonly", False)))
        raise RuntimeError("stop")

    monkeypatch.setattr(plugin_api, "_conn", _recording_conn)

    payload = plugin_api.CreateTaskBody(title="demo task")
    with pytest.raises(RuntimeError, match="stop"):
        plugin_api.create_task(payload, board=None)

    assert calls == [False]


def test_board_counts_uses_readonly_connect(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    db_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(plugin_api.kanban_db, "kanban_db_path", lambda board=None: db_path)

    calls: list[bool] = []

    class _FakeConn:
        def execute(self, query):
            return SimpleNamespace(fetchall=lambda: [{"status": "ready", "n": 2}])

        def close(self):
            return None

    def _connect(*args, **kwargs):
        calls.append(bool(kwargs.get("readonly", False)))
        return _FakeConn()

    monkeypatch.setattr(plugin_api.kanban_db, "connect", _connect)

    counts = plugin_api._board_counts("default")

    assert counts == {"ready": 2}
    assert calls == [True]


def test_stream_events_uses_readonly_connect(monkeypatch):
    monkeypatch.setattr(plugin_api, "_check_ws_token", lambda _token: True)

    calls: list[dict] = []

    def _recording_connect(*args, **kwargs):
        calls.append(dict(kwargs))
        raise RuntimeError("stop after first poll")

    monkeypatch.setattr(plugin_api.kanban_db, "connect", _recording_connect)

    class _FakeWS:
        def __init__(self):
            self.query_params = {"token": "token", "since": "0"}
            self.accepted = False
            self.closed = False

        async def accept(self):
            self.accepted = True

        async def send_json(self, data):
            return None

        async def close(self, code=None):
            self.closed = True

    ws = _FakeWS()
    asyncio.run(plugin_api.stream_events(ws))  # type: ignore[arg-type]

    assert ws.accepted is True
    assert ws.closed is True
    assert calls, "stream_events did not call kanban_db.connect"
    assert calls[0].get("readonly") is True
    assert calls[0].get("board") is None


def test_stream_events_corrupt_db_fail_stops_without_generic_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_api, "_check_ws_token", lambda _token: True)
    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")

    def _corrupt_connect(*args, **kwargs):
        raise plugin_api.kanban_db.KanbanDbCorruptError(
            db_path, tmp_path / "kanban.db.corrupt.once.bak", "sqlite refused to open file"
        )

    monkeypatch.setattr(plugin_api.kanban_db, "connect", _corrupt_connect)

    class _FakeWS:
        def __init__(self):
            self.query_params = {"token": "token", "since": "0"}
            self.accepted = False
            self.closed = False
            self.close_code = None

        async def accept(self):
            self.accepted = True

        async def send_json(self, data):
            return None

        async def close(self, code=None):
            self.closed = True
            self.close_code = code

    ws = _FakeWS()
    asyncio.run(plugin_api.stream_events(ws))  # type: ignore[arg-type]

    assert ws.accepted is True
    assert ws.closed is True
    assert ws.close_code == plugin_api.http_status.WS_1011_INTERNAL_ERROR
