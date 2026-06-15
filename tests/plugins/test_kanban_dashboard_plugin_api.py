import asyncio
import contextlib
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


def test_readonly_conn_uses_snapshot_not_live_mode_ro(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    db_path.write_text("placeholder", encoding="utf-8")
    closed = []

    class _FakeConn:
        def close(self):
            closed.append(True)

    @contextlib.contextmanager
    def _snapshot_connect(*, board=None):
        assert board == "default"
        conn = _FakeConn()
        try:
            yield conn
        finally:
            conn.close()

    def _connect(*args, **kwargs):
        raise AssertionError("dashboard readonly paths must not use live mode=ro connect")

    monkeypatch.setattr(plugin_api.kanban_db, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(plugin_api.kanban_db, "snapshot_connect", _snapshot_connect)
    monkeypatch.setattr(plugin_api.kanban_db, "connect", _connect)

    conn = plugin_api._conn(board="default", readonly=True)
    assert isinstance(conn, plugin_api._SnapshotConn)
    conn.close()
    assert closed == [True]


def test_create_task_routes_write_through_write_session(monkeypatch):
    """Under single-writer the create must go through write_session (the daemon),
    not a direct writable connect; the read-back uses a read-only conn."""
    ops: list[tuple] = []

    class _W:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def create_task(self, **kwargs):
            ops.append(("create_task", kwargs))
            return "t_new"

    monkeypatch.setattr(plugin_api.kanban_db, "write_session", lambda *a, **k: _W())

    def _recording_conn(*args, **kwargs):
        # Stop at the read-back so the test needs no real DB.
        raise RuntimeError("stop:readonly=%s" % kwargs.get("readonly"))

    monkeypatch.setattr(plugin_api, "_conn", _recording_conn)

    payload = plugin_api.CreateTaskBody(title="demo task")
    with pytest.raises(RuntimeError, match="stop:readonly=True"):
        plugin_api.create_task(payload, board=None)

    assert ops and ops[0][0] == "create_task"
    assert ops[0][1]["title"] == "demo task"


def test_board_counts_uses_snapshot_reader(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    db_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(plugin_api.kanban_db, "kanban_db_path", lambda board=None: db_path)

    calls: list[str | None] = []

    class _FakeConn:
        def execute(self, query):
            return SimpleNamespace(fetchall=lambda: [{"status": "ready", "n": 2}])

        def close(self):
            return None

    def _snapshot_conn(*, board=None):
        calls.append(board)
        return _FakeConn()

    monkeypatch.setattr(plugin_api, "_readonly_snapshot_conn", _snapshot_conn)

    counts = plugin_api._board_counts("default")

    assert counts == {"ready": 2}
    assert calls == ["default"]


def test_stream_events_uses_snapshot_reader(monkeypatch):
    monkeypatch.setattr(plugin_api, "_ws_upgrade_authorized", lambda _ws: True)

    calls: list[str | None] = []

    def _recording_snapshot_conn(*, board=None):
        calls.append(board)
        raise RuntimeError("stop after first poll")

    monkeypatch.setattr(plugin_api, "_readonly_snapshot_conn", _recording_snapshot_conn)

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
    assert calls == [None]


def test_stream_events_corrupt_db_fail_stops_without_generic_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_api, "_ws_upgrade_authorized", lambda _ws: True)
    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")

    def _corrupt_snapshot_conn(*, board=None):
        raise plugin_api.kanban_db.KanbanDbCorruptError(
            db_path, tmp_path / "kanban.db.corrupt.once.bak", "sqlite refused to open file"
        )

    monkeypatch.setattr(plugin_api, "_readonly_snapshot_conn", _corrupt_snapshot_conn)

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


def test_stream_task_log_rejects_unauthorized_upgrade(monkeypatch):
    """stream_task_log must gate the WS upgrade through the canonical auth
    helper, then reject cleanly.

    Regression for a NameError: the handler called a since-removed
    ``_check_ws_token`` (left dangling when kanban WS auth was routed through
    ``_ws_upgrade_authorized``), so every log-stream connection 500'd at the
    auth check instead of accepting or cleanly rejecting.
    """
    monkeypatch.setattr(plugin_api, "_ws_upgrade_authorized", lambda _ws: False)

    class _FakeWS:
        def __init__(self):
            self.query_params = {"token": "nope"}
            self.accepted = False
            self.closed = False
            self.close_code = None

        async def accept(self):
            self.accepted = True

        async def close(self, code=None):
            self.closed = True
            self.close_code = code

    ws = _FakeWS()
    # An unauthorized upgrade must short-circuit before any DB access, so no
    # store/conn fixture is needed — only the auth gate runs.
    asyncio.run(plugin_api.stream_task_log(ws, "task-123"))  # type: ignore[arg-type]

    assert ws.accepted is False
    assert ws.closed is True
    assert ws.close_code == plugin_api.http_status.WS_1008_POLICY_VIOLATION


def test_get_board_tolerates_non_utf8_metadata(tmp_path, monkeypatch):
    """A task_run with non-UTF-8 bytes in `metadata` (a torn-write artifact) must
    not 500 the whole board: the read connection decodes TEXT leniently.

    Regression for the dashboard 'Failed to load Kanban board: 500' that
    persisted after the DB was structurally healthy (quick_check ok) because one
    task_runs.metadata value held invalid UTF-8.
    """
    from hermes_cli import kanban_db as kb

    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    conn = kb.connect(db_path=db, readonly=False, _bootstrap=True)
    tid = kb.create_task(conn, title="x", assignee="worker")
    # Invalid UTF-8 tail (\x9e) in an otherwise-JSON metadata blob.
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, metadata) "
        "VALUES (?, 'done', 0, CAST(? AS TEXT))",
        (tid, b'{"verdict":"ok","searchO"}\x9e'),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for sfx in ("-wal", "-shm"):
        db.with_name(db.name + sfx).unlink(missing_ok=True)

    # Sanity: a strict-decoding read of that column raises (the original 500).
    strict = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            strict.execute("SELECT * FROM task_runs").fetchall()
    finally:
        strict.close()

    # The dashboard read path (snapshot conn + the diagnostics query that
    # originally 500'd at get_board:735) must now tolerate the bad row.
    conn = plugin_api._conn(readonly=True)
    try:
        diagnostics = plugin_api._compute_task_diagnostics(conn, task_ids=None)
    finally:
        conn.close()
    assert isinstance(diagnostics, dict)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: plugin_api.get_task_log("t_demo", board=None),
        lambda: plugin_api.list_diagnostics(board=None),
    ],
)
def test_read_endpoints_use_readonly_conn(monkeypatch, invoke):
    """Per-ticket / board read endpoints must open a read-only (snapshot) conn.

    Regression: under the single-writer daemon a writable connect raises
    DirectWriteForbidden, so read endpoints that used the writable `_conn`
    returned HTTP 500 (the dashboard's 'worker log' and 'Wake Hermes profiles'
    sections). Reads must pass readonly=True.
    """
    calls: list[bool] = []

    def _recording_conn(*args, **kwargs):
        calls.append(bool(kwargs.get("readonly", False)))
        raise RuntimeError("stop")

    monkeypatch.setattr(plugin_api, "_conn", _recording_conn)
    with pytest.raises(RuntimeError, match="stop"):
        invoke()
    assert calls == [True], "read endpoint must use readonly=True"


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: plugin_api.list_profile_subs("t_demo", board=None),
        lambda: plugin_api.get_stats(board=None),
        lambda: plugin_api.get_assignees(board=None),
    ],
)
def test_store_read_endpoints_use_store(monkeypatch, invoke):
    """Endpoints converted to KanbanStore routing must call _store(), not _conn().

    The store always opens snapshot (read-only) connections internally, so
    routing through it satisfies the same DirectWriteForbidden guard that the
    original readonly=True tests enforce. Verify _store is invoked.
    """

    def _recording_store(*args, **kwargs):
        raise RuntimeError("stop:store")

    def _fail_conn(*args, **kwargs):
        raise AssertionError(
            "store-routed read endpoint must not call _conn(); use _store() instead"
        )

    monkeypatch.setattr(plugin_api, "_store", _recording_store)
    monkeypatch.setattr(plugin_api, "_conn", _fail_conn)
    with pytest.raises(RuntimeError, match="stop:store"):
        invoke()

