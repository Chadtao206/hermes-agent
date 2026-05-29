import asyncio
import sqlite3
from pathlib import Path

import pytest

from gateway.config import Platform
import gateway.run as gateway_run
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_profile_event_table_canary_rejects_impossible_claim_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile wake canary", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="default",
            event_kinds=["completed"],
        )
        conn.execute(
            """
            INSERT INTO kanban_profile_event_claims (
                event_id, profile, name, root_task_id, claimed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("t_not_an_event_id", 1779917918, "", tid, 1779917918),
        )
        with pytest.raises(sqlite3.DatabaseError, match="profile-event table invariant"):
            kb.validate_profile_event_tables(conn)
    finally:
        conn.close()


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_corrupt_db_disable_is_keyed_by_resolved_path(tmp_path, monkeypatch, caplog):
    """Once one DB path is repeatedly confirmed corrupt, aliases stop retrying."""
    shared_db = tmp_path / "shared-corrupt.db"
    orders = [
        [
            {"slug": "alias-a", "db_path": str(shared_db)},
            {"slug": "alias-b", "db_path": str(shared_db)},
        ],
        [
            {"slug": "alias-b", "db_path": str(shared_db)},
            {"slug": "alias-a", "db_path": str(shared_db)},
        ],
        [
            {"slug": "alias-a", "db_path": str(shared_db)},
            {"slug": "alias-b", "db_path": str(shared_db)},
        ],
        [
            {"slug": "alias-b", "db_path": str(shared_db)},
            {"slug": "alias-a", "db_path": str(shared_db)},
        ],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise kb.KanbanDbCorruptError(
            shared_db,
            None,
            "integrity_check returned 'wrong # of entries in index idx_profile_event_claims_root'",
        )

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    for _ in range(4):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    # Hard-disable only after confirmation streak reaches threshold.
    assert connect_calls == ["alias-a", "alias-b", "alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert str(shared_db.resolve()) in disabled
    assert disabled[str(shared_db.resolve())]["reason"] == "corrupt_db"
    assert (
        disabled[str(shared_db.resolve())]["streak"]
        == gateway_run._KANBAN_DB_CORRUPTION_CONFIRM_STREAK
    )
    assert "confirmation 1/3 signaled corruption" in caplog.text
    assert "confirmation 2/3 signaled corruption" in caplog.text



def test_kanban_notifier_transient_malformed_is_not_db_path_disabled(tmp_path, monkeypatch, caplog):
    """A connection-local malformed read should not process-disable a healthy DB."""
    shared_db = tmp_path / "shared-healthy.db"
    orders = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (False, "confirmation probe quick_check/integrity_check ok"),
    )

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect_calls == ["alias-a", "alias-a"]
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert "treating as transient" in caplog.text


def test_confirm_board_db_corruption_retries_transient_database_error(monkeypatch, tmp_path):
    """A single malformed probe must not confirm corruption if retry is healthy."""

    class _Cursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return self._value

    class _Conn:
        def __init__(self, attempt):
            self._attempt = attempt

        def execute(self, sql):
            if "quick_check" in sql and self._attempt == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            if "quick_check" in sql:
                return _Cursor(("ok",))
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    attempts = {"count": 0}
    sleep_calls = []

    def fake_connect(*args, **kwargs):
        attempts["count"] += 1
        return _Conn(attempts["count"])

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda sec: sleep_calls.append(sec))

    confirmed, reason = gateway_run._confirm_board_db_corruption(tmp_path / "board.db")

    assert confirmed is False
    assert "ok on attempt 2" in reason
    assert attempts["count"] == 2
    assert sleep_calls == [gateway_run._KANBAN_DB_CORRUPTION_PROBE_BACKOFF_SEC]


def test_confirm_board_db_corruption_confirms_after_repeated_database_errors(monkeypatch, tmp_path):
    """Repeated malformed live + sqlite-backup snapshot failures confirm corruption."""
    db_path = tmp_path / "board.db"
    db_path.write_bytes(gateway_run._SQLITE_HEADER_MAGIC + (b"\x00" * 64))

    class _LiveConn:
        def execute(self, sql):
            if "quick_check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    class _BackupSourceConn:
        def backup(self, dst):
            raise sqlite3.DatabaseError("database disk image is malformed")

        def close(self):
            return None

    class _BackupDestConn:
        def close(self):
            return None

    sleep_calls = []
    connect_calls = []

    def fake_connect(database, *args, **kwargs):
        connect_calls.append(str(database))
        live_ro_calls = [
            uri for uri in connect_calls
            if "mode=ro" in uri and "immutable=1" not in uri
        ]
        if "mode=ro" in str(database) and len(live_ro_calls) <= gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS:
            return _LiveConn()
        if "mode=ro" in str(database):
            return _BackupSourceConn()
        return _BackupDestConn()

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda sec: sleep_calls.append(sec))

    confirmed, reason = gateway_run._confirm_board_db_corruption(db_path)

    assert confirmed is True
    assert f"failed {gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS}x" in reason
    assert "independent snapshot probe backup raised" in reason
    expected_live_sleeps = gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS - 1
    expected_snapshot_sleeps = gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS - 1
    assert len(sleep_calls) == expected_live_sleeps + expected_snapshot_sleeps


def test_confirm_board_db_corruption_snapshot_ok_overrides_live_readonly_malformed(
    monkeypatch, tmp_path
):
    """Live readonly malformed bursts are transient if an immutable snapshot is ok."""
    db_path = tmp_path / "board.db"
    db_path.write_bytes(gateway_run._SQLITE_HEADER_MAGIC + (b"\x00" * 64))

    class _Cursor:
        def fetchone(self):
            return ("ok",)

    class _LiveConn:
        def execute(self, sql):
            if "quick_check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    class _BackupSourceConn:
        def backup(self, dst):
            return None

        def close(self):
            return None

    class _BackupDestConn:
        def close(self):
            return None

    class _SnapshotConn:
        def execute(self, sql):
            if "quick_check" in sql:
                return _Cursor()
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    seen_uris = []
    sleep_calls = []

    def fake_connect(database, *args, **kwargs):
        seen_uris.append(str(database))
        if "immutable=1" in str(database):
            return _SnapshotConn()
        live_ro_calls = [
            uri for uri in seen_uris
            if "mode=ro" in uri and "immutable=1" not in uri
        ]
        if "mode=ro" in str(database) and len(live_ro_calls) <= gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS:
            return _LiveConn()
        if "mode=ro" in str(database):
            return _BackupSourceConn()
        return _BackupDestConn()

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda sec: sleep_calls.append(sec))

    confirmed, reason = gateway_run._confirm_board_db_corruption(db_path)

    assert confirmed is False
    assert f"failed {gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS}x" in reason
    assert "sqlite backup snapshot quick_check ok" in reason
    assert sum("mode=ro" in uri and "immutable=1" not in uri for uri in seen_uris) == gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS + 1
    assert sum("immutable=1" in uri for uri in seen_uris) == 1
    assert len(sleep_calls) == gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS - 1


def test_confirm_board_db_corruption_snapshot_backup_transient_malformed_then_ok(
    monkeypatch, tmp_path
):
    """A one-shot malformed backup probe must not confirm corruption."""
    db_path = tmp_path / "board.db"
    db_path.write_bytes(gateway_run._SQLITE_HEADER_MAGIC + (b"\x00" * 64))

    class _Cursor:
        def fetchone(self):
            return ("ok",)

    class _LiveProbeConn:
        def execute(self, sql):
            if "quick_check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    class _BackupSourceConn:
        def __init__(self, attempt):
            self._attempt = attempt

        def backup(self, dst):
            if self._attempt == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return None

        def close(self):
            return None

    class _BackupDestConn:
        def close(self):
            return None

    class _SnapshotProbeConn:
        def execute(self, sql):
            if "quick_check" in sql:
                return _Cursor()
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    live_calls = {"count": 0}
    snapshot_backup_attempt = {"count": 0}
    sleep_calls = []

    def fake_connect(database, *args, **kwargs):
        uri = str(database)
        if "immutable=1" in uri:
            return _SnapshotProbeConn()
        if "mode=ro" in uri:
            if live_calls["count"] < gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS:
                live_calls["count"] += 1
                return _LiveProbeConn()
            snapshot_backup_attempt["count"] += 1
            return _BackupSourceConn(snapshot_backup_attempt["count"])
        return _BackupDestConn()

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda sec: sleep_calls.append(sec))

    confirmed, reason = gateway_run._confirm_board_db_corruption(db_path)

    assert confirmed is False
    assert f"failed {gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS}x" in reason
    assert "sqlite backup snapshot quick_check ok on attempt 2" in reason
    assert snapshot_backup_attempt["count"] == 2
    expected_sleeps = gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS
    assert len(sleep_calls) == expected_sleeps


def test_confirm_board_db_corruption_operational_error_is_transient(monkeypatch, tmp_path):
    """Operational open/lock failures remain transient and should not retry."""
    sleep_calls = []

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fail_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda sec: sleep_calls.append(sec))

    confirmed, reason = gateway_run._confirm_board_db_corruption(tmp_path / "board.db")

    assert confirmed is False
    assert "transient/open failure on attempt 1" in reason
    assert sleep_calls == []


def test_confirm_board_db_corruption_non_corruption_database_error_is_transient(
    monkeypatch, tmp_path
):
    """Non-corruption DatabaseError should short-circuit as transient."""

    def fail_connect(*args, **kwargs):
        err = sqlite3.DatabaseError("no such table: tasks")
        err.sqlite_errorcode = sqlite3.SQLITE_ERROR
        err.sqlite_errorname = "SQLITE_ERROR"
        raise err

    sleep_calls = []
    monkeypatch.setattr(gateway_run.sqlite3, "connect", fail_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda sec: sleep_calls.append(sec))

    confirmed, reason = gateway_run._confirm_board_db_corruption(tmp_path / "board.db")

    assert confirmed is False
    assert "non-corruption DatabaseError" in reason
    assert sleep_calls == []


def test_confirm_board_db_corruption_header_mismatch_confirms_without_probe(monkeypatch, tmp_path):
    """Non-SQLite file header is durable corruption and should fail-close immediately."""
    db_path = tmp_path / "board.db"
    db_path.write_bytes(b"not-a-real-sqlite-db")

    connect_called = {"value": False}

    def fail_connect(*args, **kwargs):
        connect_called["value"] = True
        raise AssertionError("connect should not be called when header mismatches")

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fail_connect)

    confirmed, reason = gateway_run._confirm_board_db_corruption(db_path)

    assert confirmed is True
    assert "header mismatch" in reason
    assert connect_called["value"] is False


def test_confirm_board_db_corruption_uses_read_only_uri_and_only_quick_check(
    monkeypatch, tmp_path
):
    """Probe should use readonly URI and avoid integrity_check in hot path."""
    db_path = tmp_path / "board.db"
    db_path.write_bytes(gateway_run._SQLITE_HEADER_MAGIC + (b"\x00" * 64))

    seen_sql = []
    captured = {}

    class _Cursor:
        def fetchone(self):
            return ("ok",)

    class _Conn:
        def execute(self, sql):
            seen_sql.append(sql)
            return _Cursor()

        def close(self):
            return None

    def fake_connect(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Conn()

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_connect)

    confirmed, reason = gateway_run._confirm_board_db_corruption(db_path)

    target = captured["args"][0]
    assert confirmed is False
    assert "quick_check ok on attempt 1" in reason
    assert "mode=ro" in target
    assert captured["kwargs"]["uri"] is True
    assert any("quick_check" in sql for sql in seen_sql)
    assert not any("integrity_check" in sql for sql in seen_sql)


def test_confirm_board_db_corruption_outlasts_transient_malformed_window(
    monkeypatch, tmp_path
):
    """A short malformed burst that later recovers should not confirm corruption."""
    db_path = tmp_path / "board.db"
    db_path.write_bytes(gateway_run._SQLITE_HEADER_MAGIC + (b"\x00" * 64))

    fake_clock = {"seconds": 0.0}
    attempts = {"count": 0}
    sleep_calls = []

    class _Cursor:
        def fetchone(self):
            return ("ok",)

    class _Conn:
        def execute(self, sql):
            if "quick_check" not in sql:
                raise AssertionError(f"unexpected SQL: {sql}")
            if fake_clock["seconds"] < 1.0:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return _Cursor()

        def close(self):
            return None

    def fake_connect(*args, **kwargs):
        attempts["count"] += 1
        return _Conn()

    def fake_sleep(sec):
        sleep_calls.append(sec)
        fake_clock["seconds"] += sec

    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", fake_sleep)

    confirmed, reason = gateway_run._confirm_board_db_corruption(db_path)

    assert confirmed is False
    assert "quick_check ok on attempt 4" in reason
    assert attempts["count"] == 4
    assert sleep_calls == [
        gateway_run._KANBAN_DB_CORRUPTION_PROBE_BACKOFF_SEC,
        gateway_run._KANBAN_DB_CORRUPTION_PROBE_BACKOFF_SEC,
        gateway_run._KANBAN_DB_CORRUPTION_PROBE_BACKOFF_SEC,
    ]


def test_kanban_notifier_transient_malformed_retrying_probe_preserves_db(tmp_path, monkeypatch, caplog):
    """Notifier should treat malformed reads as transient when probe retry turns healthy."""
    shared_db = tmp_path / "shared-healthy-retry.db"
    orders = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.DatabaseError("database disk image is malformed")

    class _Cursor:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return self._value

    class _ProbeConn:
        def __init__(self, attempt):
            self._attempt = attempt

        def execute(self, sql):
            if "quick_check" in sql and self._attempt % 2 == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            if "quick_check" in sql:
                return _Cursor(("ok",))
            if "integrity_check" in sql:
                return _Cursor(("ok",))
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    probe_attempts = {"count": 0}

    def fake_probe_connect(*args, **kwargs):
        probe_attempts["count"] += 1
        return _ProbeConn(probe_attempts["count"])

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_probe_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda _sec: None)

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect_calls == ["alias-a", "alias-a"]
    assert probe_attempts["count"] == 4
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert getattr(runner, "_kanban_notifier_corruption_streaks", {}) == {}
    assert "treating as transient" in caplog.text


def test_kanban_notifier_live_readonly_malformed_snapshot_ok_never_hard_disables(
    tmp_path, monkeypatch, caplog
):
    """Repeated live readonly malformed probes need snapshot confirmation to disable."""
    shared_db = tmp_path / "shared-sidecar-churn.db"
    kb.init_db(db_path=shared_db)
    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}] for _ in range(3)]
    connect_calls = []

    class _Cursor:
        def fetchone(self):
            return ("ok",)

    class _LiveProbeConn:
        def execute(self, sql):
            if "quick_check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    class _BackupSourceConn:
        def backup(self, dst):
            return None

        def close(self):
            return None

    class _BackupDestConn:
        def close(self):
            return None

    class _SnapshotProbeConn:
        def execute(self, sql):
            if "quick_check" in sql:
                return _Cursor()
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    probe_connects = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.DatabaseError("database disk image is malformed")

    def fake_probe_connect(database, *args, **kwargs):
        probe_connects.append(str(database))
        if "immutable=1" in str(database):
            return _SnapshotProbeConn()
        live_ro_calls = [
            uri for uri in probe_connects
            if "mode=ro" in uri and "immutable=1" not in uri
        ]
        if "mode=ro" in str(database) and len(live_ro_calls) % (gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS + 1) != 0:
            return _LiveProbeConn()
        if "mode=ro" in str(database):
            return _BackupSourceConn()
        return _BackupDestConn()

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_probe_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda _sec: None)

    runner = _make_runner(RecordingAdapter())
    for _ in range(3):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    assert connect_calls == ["alias-a", "alias-a", "alias-a"]
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert getattr(runner, "_kanban_notifier_corruption_streaks", {}) == {}
    assert "sqlite backup snapshot quick_check ok" in caplog.text
    assert "is corrupt/unhealthy" not in caplog.text


def test_kanban_notifier_live_readonly_malformed_snapshot_backup_transient_then_ok(
    tmp_path, monkeypatch, caplog
):
    """Transient snapshot backup malformed should not disable notifier DB access."""
    shared_db = tmp_path / "shared-sidecar-churn-backup-transient.db"
    kb.init_db(db_path=shared_db)
    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}] for _ in range(3)]
    connect_calls = []

    class _Cursor:
        def fetchone(self):
            return ("ok",)

    class _LiveProbeConn:
        def execute(self, sql):
            if "quick_check" in sql:
                raise sqlite3.DatabaseError("database disk image is malformed")
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    class _BackupSourceConn:
        def __init__(self, attempt):
            self._attempt = attempt

        def backup(self, dst):
            if self._attempt == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return None

        def close(self):
            return None

    class _BackupDestConn:
        def close(self):
            return None

    class _SnapshotProbeConn:
        def execute(self, sql):
            if "quick_check" in sql:
                return _Cursor()
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self):
            return None

    probe_state = {"ro_in_cycle": 0}

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.DatabaseError("database disk image is malformed")

    def fake_probe_connect(database, *args, **kwargs):
        uri = str(database)
        if "immutable=1" in uri:
            probe_state["ro_in_cycle"] = 0
            return _SnapshotProbeConn()
        if "mode=ro" in uri:
            probe_state["ro_in_cycle"] += 1
            if probe_state["ro_in_cycle"] <= gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS:
                return _LiveProbeConn()
            snapshot_attempt = (
                probe_state["ro_in_cycle"]
                - gateway_run._KANBAN_DB_CORRUPTION_PROBE_ATTEMPTS
            )
            return _BackupSourceConn(snapshot_attempt)
        return _BackupDestConn()

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(gateway_run.sqlite3, "connect", fake_probe_connect)
    monkeypatch.setattr(gateway_run.time, "sleep", lambda _sec: None)

    runner = _make_runner(RecordingAdapter())
    for _ in range(3):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    assert connect_calls == ["alias-a", "alias-a", "alias-a"]
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert getattr(runner, "_kanban_notifier_corruption_streaks", {}) == {}
    assert "sqlite backup snapshot quick_check ok on attempt 2" in caplog.text
    assert "is corrupt/unhealthy" not in caplog.text


def test_kanban_notifier_confirmation_streak_resets_when_probe_turns_healthy(
    tmp_path, monkeypatch, caplog
):
    """Confirmed failures must be consecutive; a healthy probe resets the streak."""
    shared_db = tmp_path / "shared-flap.db"
    orders = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]
    connect_calls = []
    confirmations = iter(
        [
            (True, "test confirmed corruption #1"),
            (True, "test confirmed corruption #2"),
            (False, "confirmation probe quick_check/integrity_check ok"),
            (True, "test confirmed corruption #3"),
        ]
    )

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: next(confirmations),
    )

    runner = _make_runner(RecordingAdapter())
    for _ in range(4):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    resolved = str(shared_db.resolve())
    assert connect_calls == ["alias-a", "alias-a", "alias-a", "alias-a"]
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert runner._kanban_notifier_corruption_streaks[resolved] == 1
    assert "confirmation 1/3 signaled corruption" in caplog.text
    assert "confirmation 2/3 signaled corruption" in caplog.text
    assert "treating as transient" in caplog.text


def test_kanban_notifier_post_connect_read_corruption_progresses_to_hard_disable(
    tmp_path, monkeypatch, caplog
):
    """Corruption after successful connect must still advance 1/3 -> 2/3 -> 3/3."""
    shared_db = tmp_path / "post-connect-read-corrupt.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}] for _ in range(4)]
    connect_calls = []
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_notify_subs(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", fake_list_notify_subs)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    for _ in range(4):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    resolved = str(shared_db.resolve())
    assert connect_calls == ["alias-a", "alias-a", "alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert resolved in disabled
    assert disabled[resolved]["reason"] == "corrupt_db"
    assert disabled[resolved]["streak"] == gateway_run._KANBAN_DB_CORRUPTION_CONFIRM_STREAK
    assert resolved not in runner._kanban_notifier_corruption_streaks
    assert "confirmation 1/3 signaled corruption" in caplog.text
    assert "confirmation 2/3 signaled corruption" in caplog.text
    assert "is corrupt/unhealthy during read" in caplog.text


def test_kanban_notifier_post_connect_profile_event_corruption_progresses_streak(
    tmp_path, monkeypatch, caplog
):
    """Profile-event claim corruption after connect should preserve streak across ticks."""
    shared_db = tmp_path / "post-connect-profile-event-corrupt.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}] for _ in range(3)]
    connect_calls = []
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_profile_event_subs(conn, enabled_only=True):
        return [{"task_id": "t_profile", "profile": "default", "name": ""}]

    def fake_claim_profile_sub(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", lambda *a, **kw: [])
    monkeypatch.setattr(kb, "list_profile_event_subs", fake_list_profile_event_subs)
    monkeypatch.setattr(kb, "claim_unseen_events_for_profile_sub", fake_claim_profile_sub)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    for _ in range(3):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    assert connect_calls == ["alias-a", "alias-a", "alias-a"]
    assert "confirmation 1/3 signaled corruption" in caplog.text
    assert "confirmation 2/3 signaled corruption" in caplog.text
    assert "profile-event claim detected corrupt/unhealthy DB" in caplog.text


def test_kanban_notifier_multi_profile_sub_corruption_increments_once_per_tick(
    tmp_path, monkeypatch, caplog
):
    """Multiple failing profile subs on one DB should only consume one confirmation per tick."""
    shared_db = tmp_path / "profile-event-multi-sub-corrupt.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}]]
    connect_calls = []
    claim_calls = {"count": 0}
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_profile_event_subs(conn, enabled_only=True):
        return [
            {"task_id": "t_profile_a", "profile": "default", "name": "sub-a"},
            {"task_id": "t_profile_b", "profile": "default", "name": "sub-b"},
            {"task_id": "t_profile_c", "profile": "default", "name": "sub-c"},
        ]

    def fake_claim_profile_sub(*args, **kwargs):
        claim_calls["count"] += 1
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", lambda *a, **kw: [])
    monkeypatch.setattr(kb, "list_profile_event_subs", fake_list_profile_event_subs)
    monkeypatch.setattr(kb, "claim_unseen_events_for_profile_sub", fake_claim_profile_sub)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    resolved = str(shared_db.resolve())
    assert connect_calls == ["alias-a"]
    assert claim_calls["count"] == 3
    assert runner._kanban_notifier_corruption_streaks[resolved] == 1
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert caplog.text.count("confirmation 1/3 signaled corruption") == 1
    assert "confirmation 2/3 signaled corruption" not in caplog.text


def test_kanban_notifier_multi_profile_sub_corruption_still_progresses_across_ticks(
    tmp_path, monkeypatch, caplog
):
    """Persistent profile-event corruption should still hard-disable after consecutive ticks."""
    shared_db = tmp_path / "profile-event-multi-sub-progress.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}] for _ in range(3)]
    connect_calls = []
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_profile_event_subs(conn, enabled_only=True):
        return [
            {"task_id": "t_profile_a", "profile": "default", "name": "sub-a"},
            {"task_id": "t_profile_b", "profile": "default", "name": "sub-b"},
            {"task_id": "t_profile_c", "profile": "default", "name": "sub-c"},
        ]

    def fake_claim_profile_sub(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", lambda *a, **kw: [])
    monkeypatch.setattr(kb, "list_profile_event_subs", fake_list_profile_event_subs)
    monkeypatch.setattr(kb, "claim_unseen_events_for_profile_sub", fake_claim_profile_sub)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    for _ in range(3):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    resolved = str(shared_db.resolve())
    assert connect_calls == ["alias-a", "alias-a", "alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert resolved in disabled
    assert disabled[resolved]["reason"] == "profile_event_corruption"
    assert disabled[resolved]["streak"] == gateway_run._KANBAN_DB_CORRUPTION_CONFIRM_STREAK
    assert caplog.text.count("confirmation 1/3 signaled corruption") == 1
    assert caplog.text.count("confirmation 2/3 signaled corruption") == 1
    assert caplog.text.count("profile-event claim detected corrupt/unhealthy DB") == 1


def test_kanban_notifier_post_connect_streak_clears_after_healthy_read_cycle(
    tmp_path, monkeypatch, caplog
):
    """A healthy read cycle after confirmed corruption should reset streak."""
    shared_db = tmp_path / "post-connect-read-flap.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    orders = [[{"slug": "alias-a", "db_path": str(shared_db)}] for _ in range(4)]
    connect_calls = []
    list_calls = {"count": 0}
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_notify_subs(*args, **kwargs):
        list_calls["count"] += 1
        if list_calls["count"] in (1, 2, 4):
            raise sqlite3.DatabaseError("database disk image is malformed")
        return []

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", fake_list_notify_subs)
    monkeypatch.setattr(
        gateway_run,
        "_confirm_board_db_corruption",
        lambda db_path: (True, "test confirmed corruption"),
    )

    runner = _make_runner(RecordingAdapter())
    for _ in range(4):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True

    resolved = str(shared_db.resolve())
    assert connect_calls == ["alias-a", "alias-a", "alias-a", "alias-a"]
    assert getattr(runner, "_kanban_notifier_disabled_db_paths", {}) == {}
    assert runner._kanban_notifier_corruption_streaks[resolved] == 1
    assert "confirmation 1/3 signaled corruption" in caplog.text
    assert "confirmation 2/3 signaled corruption" in caplog.text
    assert caplog.text.count("confirmation 1/3 signaled corruption") == 2


def test_kanban_notifier_disk_io_disable_is_keyed_by_resolved_path(tmp_path, monkeypatch, caplog):
    """Disk I/O fail-stop should disable the DB path once per process.

    If one alias hits `sqlite3.OperationalError("disk I/O error")`, later
    aliases resolving to the same DB must be skipped for the process lifetime.
    """
    shared_db = tmp_path / "shared-disk-io.db"
    orders = [
        [
            {"slug": "alias-a", "db_path": str(shared_db)},
            {"slug": "alias-b", "db_path": str(shared_db)},
        ],
        [
            {"slug": "alias-b", "db_path": str(shared_db)},
            {"slug": "alias-a", "db_path": str(shared_db)},
        ],
    ]
    connect_calls = []

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect_calls == ["alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert str(shared_db.resolve()) in disabled
    assert disabled[str(shared_db.resolve())]["reason"] == "disk_io_error"
    assert "filesystem-level fault" in caplog.text


def test_kanban_notifier_inner_disk_io_disables_board_path(tmp_path, monkeypatch):
    """Disk I/O during in-conn board reads should quarantine that DB path."""
    shared_db = tmp_path / "inner-disk-io.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(shared_db))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    orders = [
        [{"slug": "alias-a", "db_path": str(shared_db)}],
        [{"slug": "alias-a", "db_path": str(shared_db)}],
    ]
    connect_calls = []
    list_calls = []
    real_connect = kb.connect

    def fake_list_boards(include_archived=False):
        return orders.pop(0) if orders else []

    def fake_connect(*, board=None, **kwargs):
        connect_calls.append(board)
        return real_connect(board=board, **kwargs)

    def fake_list_notify_subs(*args, **kwargs):
        list_calls.append(1)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb, "list_boards", fake_list_boards)
    monkeypatch.setattr(kb, "record_notifier_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(kb, "list_notify_subs", fake_list_notify_subs)

    runner = _make_runner(RecordingAdapter())
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(list_calls) == 1
    assert connect_calls == ["alias-a"]
    disabled = runner._kanban_notifier_disabled_db_paths
    assert str(shared_db.resolve()) in disabled
    assert disabled[str(shared_db.resolve())]["reason"] == "disk_io_error"


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


# ---------------------------------------------------------------------------
# Event-driven kanban subscriptions: per-sub event_kinds + include_children.
# These pin the first-pass orchestration contract — legacy subs still see only
# terminal events, custom kinds let a sub tail lifecycle moments (created,
# claimed, etc.), and include_children fans descendant events up to a parent.
# ---------------------------------------------------------------------------


def test_legacy_subscription_only_sees_terminal_events(tmp_path, monkeypatch):
    """A sub registered without event_kinds keeps the old terminal-only filter.

    `created` and `claimed` fire on every task during its lifecycle but the
    legacy gateway watcher only surfaces terminal kinds — that must stay
    true even after the event_kinds column lands.
    """
    db_path = tmp_path / "legacy-sub.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Only `created` exists on the task; legacy filter must drop it.
    assert adapter.sent == [], (
        f"Legacy subscription should ignore non-terminal events, got "
        f"{[s['text'] for s in adapter.sent]}"
    )

    # Now complete the task — that *is* a terminal event and must deliver.
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="ok")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()


def test_custom_event_kinds_delivers_lifecycle_events(tmp_path, monkeypatch):
    """A sub with explicit ``event_kinds`` receives non-terminal lifecycle pings.

    Pins that the gateway watcher honors per-sub kind filters instead of the
    hardcoded terminal set, and that the renderer doesn't drop `created` /
    `claimed`.
    """
    db_path = tmp_path / "custom-kinds.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="lifecycle task", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            event_kinds=["created", "claimed"],
        )
        # The `create_task` call above already emitted a `created` event
        # before the sub existed (cursor starts at 0, so it'll still be
        # picked up). Add a `claimed` to exercise both kinds in one tick.
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    kinds_delivered = [s["text"] for s in adapter.sent]
    assert any("created" in t for t in kinds_delivered), kinds_delivered
    assert any("claimed" in t for t in kinds_delivered), kinds_delivered
    # No terminal event ever fired, so the only deliveries are the two
    # lifecycle pings we asked for.
    assert len(adapter.sent) == 2, kinds_delivered


def test_include_children_subscription_receives_child_completion(tmp_path, monkeypatch):
    """A parent-level sub with include_children sees descendant completions.

    Verifies the subtree fetch path: events from a child task linked under
    the parent via a dependency edge are claimed under the parent's single
    cursor and rendered against the child task's title.
    """
    db_path = tmp_path / "subtree.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="epic-parent", assignee="lead")
        child_id = kb.create_task(conn, title="leaf-child", assignee="worker")
        kb.link_tasks(conn, parent_id, child_id)
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            include_children=True,
        )
        # Stamp a `completed` event onto the CHILD directly. Driving it
        # through `complete_task` would require running the full
        # ready/claim/run lifecycle, which is out of scope for this test
        # — we're pinning the subtree-fetch contract: a parent sub with
        # include_children sees the child's event.
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "child done"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, (
        f"Parent sub with include_children should receive the child's "
        f"completion event; got {[s['text'] for s in adapter.sent]}"
    )
    text = adapter.sent[0]["text"]
    assert "done" in text.lower()
    assert child_id in text, (
        f"Rendered message should identify the descendant task; got {text!r}"
    )
    assert "leaf-child" in text, (
        f"Rendered message should use the child's title, not the parent's; "
        f"got {text!r}"
    )



def test_include_children_subscription_survives_parent_completion(tmp_path, monkeypatch):
    """Subtree subscriptions stay alive after root completion.

    In dependency graphs the parent often completes before children become
    ready. A root-level orchestration subscription must therefore survive the
    root's completed event so descendant lane completions still notify Jensen
    or another orchestrator.
    """
    db_path = tmp_path / "subtree-survives-root.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="root lane", assignee="lead")
        child_id = kb.create_task(
            conn, title="child lane", assignee="worker", parents=[parent_id],
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            include_children=True,
        )
        assert kb.complete_task(conn, parent_id, summary="root done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert parent_id in adapter.sent[0]["text"]

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, parent_id)
        assert len(subs) == 1, (
            "include_children subscription must survive root completion so "
            "downstream child lane events can still notify"
        )
        assert kb.complete_task(conn, child_id, summary="child done")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2
    assert child_id in adapter.sent[1]["text"]
    assert "child lane" in adapter.sent[1]["text"]



class FailOnSecondSendAdapter:
    """Adapter that fails only on its second send call."""

    def __init__(self):
        self.calls = 0
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated second-send failure")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def test_multi_event_subscription_rewinds_to_last_success(tmp_path, monkeypatch):
    """Retry after a partial batch failure must not duplicate successes.

    Custom event subscriptions make multi-event batches normal. If the first
    event sends successfully and the second send fails, the notifier should
    rewind only to the first event id, not all the way back to the pre-claim
    cursor. Otherwise the first event is delivered twice on retry.
    """
    db_path = tmp_path / "partial-batch-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="partial failure", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            event_kinds=["created", "claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    adapter = FailOnSecondSendAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "created" in adapter.sent[0]["text"]

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    texts = [item["text"] for item in adapter.sent]
    assert len(texts) == 2, texts
    assert sum("created" in text for text in texts) == 1, texts
    assert sum("claimed" in text for text in texts) == 1, texts



def test_include_children_subscription_unsubs_when_root_archived(tmp_path, monkeypatch):
    """Archive is the explicit cleanup path for subtree subscriptions."""
    db_path = tmp_path / "subtree-archive-cleanup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="root cleanup", assignee="lead")
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            include_children=True,
        )
        assert kb.archive_task(conn, parent_id)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "archived" in adapter.sent[0]["text"].lower()

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, parent_id) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 2: profile-level event subscriptions drive a fire-and-forget
# ``hermes -p <profile> chat -q <prompt>`` wake instead of a chat message.
# These tests pin: (a) the wake fires without any active adapter, (b) the
# spawned command/prompt/env match the contract, (c) spawn failure rewinds
# the cursor so the next tick retries, (d) include_children fans descendant
# events into the wake batch.
# ---------------------------------------------------------------------------


def _make_runner_no_adapters():
    """Runner with the kanban notifier wired but zero connected adapters.

    Phase 2 requires profile wakes to run on adapter-less gateways — this
    fixture pins that invariant.
    """
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._kanban_sub_fail_counts = {}
    return runner


def test_notifier_heartbeat_records_on_adapterless_tick(tmp_path, monkeypatch):
    """The gateway records presence even when only profile wakes are possible."""
    db_path = tmp_path / "adapterless-heartbeat.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    runner = _make_runner_no_adapters()
    runner._kanban_notifier_profile = "default"
    runner._kanban_notifier_started_at = 1_000
    runner._kanban_notifier_host = "test-host"
    runner._kanban_notifier_id = "test-host:4242:1000"
    monkeypatch.setattr("gateway.run.os.getpid", lambda: 4242)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        rows = kb.list_notifier_heartbeats(
            conn,
            board_slug="default",
            db_path=str(db_path.resolve()),
            now=int(kb.time.time()),
        )
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["notifier_id"] == "test-host:4242:1000"
    assert rows[0]["notifier_profile"] == "default"
    assert rows[0]["active"] is True


def test_notifier_heartbeat_failure_does_not_block_profile_wake(tmp_path, monkeypatch):
    """Heartbeat write failures are diagnostics-only; wake claims still run."""
    db_path = tmp_path / "heartbeat-failure-still-wakes.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="wake despite heartbeat fail", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    captured_events = []

    def fake_heartbeat(*args, **kwargs):
        raise RuntimeError("heartbeat table temporarily unavailable")

    def fake_wake(self, sub, events, task, event_tasks, board):
        captured_events.extend(events)
        return True, None

    monkeypatch.setattr(kb, "record_notifier_heartbeat", fake_heartbeat)
    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert captured_events
    assert any(ev.kind == "claimed" for ev in captured_events)


def test_profile_event_wake_spawns_without_adapters(tmp_path, monkeypatch):
    """A profile event sub spawns the wake command with no chat adapter connected."""
    db_path = tmp_path / "profile-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="profile target", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed", "completed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    captured: dict = {}

    def fake_wake(self, sub, events, task, event_tasks, board):
        captured["sub"] = sub
        captured["events"] = list(events)
        captured["board"] = board
        captured["task_id"] = sub["task_id"]
        captured["profile"] = sub["profile"]
        # Reuse the real prompt builder so we exercise it.
        captured["prompt"] = self._build_kanban_event_wake_prompt(
            sub, events, task, event_tasks, board,
        )
        return True

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake,
    )

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert captured.get("profile") == "jensen", (
        "Wake should fire even with zero connected adapters"
    )
    assert captured["task_id"] == tid
    assert any(ev.kind == "claimed" for ev in captured["events"])
    prompt = captured["prompt"]
    assert "Kanban event wake" in prompt
    assert "DO NOT create cron" in prompt
    assert tid in prompt
    assert "jensen" in prompt
    assert "claimed" in prompt

    # Cursor advanced past the wake batch.
    conn = kb.connect()
    try:
        subs = kb.list_profile_event_subs(conn, task_id=tid)
        assert len(subs) == 1
        assert int(subs[0]["last_event_id"]) > 0
        assert int(subs[0]["last_wake_at"] or 0) > 0
    finally:
        conn.close()


def test_profile_event_wake_includes_child_event(tmp_path, monkeypatch):
    """include_children profile sub picks up a descendant lifecycle event."""
    db_path = tmp_path / "profile-subtree.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="parent", assignee="lead")
        child_id = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent_id, child_id)
        kb.add_profile_event_sub(
            conn,
            task_id=parent_id,
            profile="jensen",
            include_children=True,
        )
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "child done"},
        )
    finally:
        conn.close()

    captured: dict = {}

    def fake_wake(self, sub, events, task, event_tasks, board):
        captured["events"] = list(events)
        captured["event_tasks"] = dict(event_tasks)
        return True

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake,
    )

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    events = captured.get("events") or []
    assert any(e.task_id == child_id and e.kind == "completed" for e in events), (
        f"include_children parent sub should see child completion; got "
        f"{[(e.task_id, e.kind) for e in events]}"
    )
    ev_tasks = captured.get("event_tasks") or {}
    assert child_id in ev_tasks
    assert ev_tasks[child_id] is not None
    assert ev_tasks[child_id].title == "child"


def test_profile_event_wake_dedupes_overlapping_child_roots(tmp_path, monkeypatch):
    """The same descendant event should wake one profile/name only once."""
    db_path = tmp_path / "profile-subtree-dedupe.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root_a = kb.create_task(conn, title="root a", assignee="lead")
        root_b = kb.create_task(conn, title="root b", assignee="lead")
        child_id = kb.create_task(conn, title="shared child", assignee="worker")
        kb.link_tasks(conn, root_a, child_id)
        kb.link_tasks(conn, root_b, child_id)
        for root_id in (root_a, root_b):
            kb.add_profile_event_sub(
                conn,
                task_id=root_id,
                profile="default",
                name="jensen-orchestrator",
                event_kinds=["completed"],
                include_children=True,
            )
        kb._append_event(
            conn, child_id, kind="completed", payload={"summary": "shared done"},
        )
    finally:
        conn.close()

    wake_calls: list[tuple[str, list[str]]] = []

    def fake_wake(self, sub, events, task, event_tasks, board):
        wake_calls.append((sub["task_id"], [e.task_id for e in events]))
        return True

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(wake_calls) == 1
    assert wake_calls[0][1] == [child_id]

    conn = kb.connect()
    try:
        claims = conn.execute(
            "SELECT event_id, profile, name, root_task_id "
            "FROM kanban_profile_event_claims"
        ).fetchall()
        assert len(claims) == 1
        assert claims[0]["profile"] == "default"
        assert claims[0]["name"] == "jensen-orchestrator"
        subs = kb.list_profile_event_subs(conn, profile="default")
        assert len(subs) == 2
        assert all(int(sub["last_event_id"] or 0) > 0 for sub in subs)
    finally:
        conn.close()


def test_profile_event_wake_failure_rewinds_cursor(tmp_path, monkeypatch):
    """Spawn failure must rewind the cursor so the next tick retries."""
    db_path = tmp_path / "profile-rewind.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="retry target", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    def fake_wake_fail(self, sub, events, task, event_tasks, board):
        return False

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake_fail,
    )

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        subs = kb.list_profile_event_subs(conn, task_id=tid)
        assert len(subs) == 1
        # Cursor rewound to 0; last_wake_at untouched, and the transient
        # event claim was released so a later notifier tick can retry.
        assert int(subs[0]["last_event_id"]) == 0
        assert subs[0]["last_wake_at"] in (None, 0)
        claims = conn.execute(
            "SELECT COUNT(*) FROM kanban_profile_event_claims"
        ).fetchone()[0]
        assert claims == 0
    finally:
        conn.close()

    wake_calls: list[int] = []

    def fake_wake_success(self, sub, events, task, event_tasks, board):
        wake_calls.append(len(events))
        return True

    monkeypatch.setattr(
        GatewayRunner, "_kanban_profile_wake", fake_wake_success,
    )
    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert wake_calls == [1]


def test_profile_event_wake_command_and_env_via_popen(tmp_path, monkeypatch):
    """The real wake path constructs the expected argv + env via subprocess.Popen."""
    db_path = tmp_path / "profile-popen.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="popen target", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="default",  # 'default' avoids the profile-dir existence check
            event_kinds=["claimed"],
            wake_prompt="please orchestrate",
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    popen_calls: list[dict] = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append({"cmd": cmd, "kwargs": kwargs})

    monkeypatch.setattr("subprocess.Popen", FakePopen)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(popen_calls) == 1, (
        f"Expected exactly one Popen for the wake spawn; got {len(popen_calls)}"
    )
    call = popen_calls[0]
    cmd = call["cmd"]
    # Profile, accept-hooks, and the chat -q invocation must all appear.
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "default"
    assert "--accept-hooks" in cmd
    assert "chat" in cmd and "-q" in cmd
    prompt_arg = cmd[cmd.index("-q") + 1]
    assert "Kanban event wake" in prompt_arg
    assert tid in prompt_arg
    assert "please orchestrate" in prompt_arg

    env = call["kwargs"]["env"]
    assert env.get("HERMES_KANBAN_EVENT_WAKE") == "1"
    assert env.get("HERMES_PROFILE") == "default"
    assert env.get("HERMES_KANBAN_BOARD")
    assert env.get("HERMES_KANBAN_DB")

    # Cursor advanced + last_wake_at stamped on Popen success.
    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["last_event_id"]) > 0
        assert int(sub["last_wake_at"] or 0) > 0
    finally:
        conn.close()


def test_profile_event_wake_disabled_via_wake_agent_zero(tmp_path, monkeypatch):
    """wake_agent=0 advances the cursor but never spawns."""
    db_path = tmp_path / "profile-no-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="no-wake", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
            wake_agent=False,
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    wake_calls: list = []

    def fake_wake(self, sub, events, task, event_tasks, board):
        wake_calls.append(sub)
        return True

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert wake_calls == [], (
        "wake_agent=0 subscriptions must not spawn a wake"
    )
    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["last_event_id"]) > 0  # cursor still advances
        assert sub["last_wake_at"] in (None, 0)
    finally:
        conn.close()


def test_chat_subscription_unaffected_when_profile_sub_present(tmp_path, monkeypatch):
    """Adding a profile sub does not change chat-subscription delivery."""
    db_path = tmp_path / "mixed-subs.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="mixed", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.add_profile_event_sub(
            conn, task_id=tid, profile="jensen", event_kinds=["completed"],
        )
        kb.complete_task(conn, tid, summary="ok")
    finally:
        conn.close()

    wake_calls: list = []

    def fake_wake(self, sub, events, task, event_tasks, board):
        wake_calls.append(sub)
        return True

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Chat sub still delivers the terminal event.
    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()
    # Profile sub also fires for the same event.
    assert len(wake_calls) == 1
    assert wake_calls[0]["profile"] == "jensen"



# ---------------------------------------------------------------------------
# Phase 3: profile-wake health tracking + wake-event audit log
# ---------------------------------------------------------------------------
#
# These tests pin the gateway → kanban_db wiring for ``record_profile_wake_*``:
# success clears the error state and appends a ``status='success'`` wake-event
# row, failure increments ``wake_failure_count``, stamps the sanitized error,
# preserves the existing cursor-rewind behavior, and appends/coalesces
# ``status='failed'`` wake-event rows.


def test_profile_wake_success_records_health_and_wake_event(tmp_path, monkeypatch):
    db_path = tmp_path / "profile-health-success.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="health success", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        # Seed a "previously failed" state so the success path must clear it.
        kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=0,
            old_cursor=0,
            error=RuntimeError("stale"),
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    def fake_wake(self, sub, events, task, event_tasks, board):
        return True, None

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["last_event_id"]) > 0
        assert int(sub["last_wake_at"] or 0) > 0
        # Success path must clear error fields + reset failure counter.
        assert sub["last_wake_error"] in (None, "")
        assert sub["last_wake_error_at"] in (None, 0)
        assert int(sub["wake_failure_count"] or 0) == 0
        # One pre-seeded failed row + one success row from this tick.
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        statuses = [r["status"] for r in rows]
        assert "success" in statuses, f"expected a success row; got {statuses}"
        assert "failed" in statuses, f"expected pre-seeded failed row; got {statuses}"
    finally:
        conn.close()


def test_profile_wake_failure_records_health_and_preserves_rewind(tmp_path, monkeypatch):
    db_path = tmp_path / "profile-health-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="health failure", assignee="worker")
        kb.add_profile_event_sub(
            conn,
            task_id=tid,
            profile="jensen",
            event_kinds=["claimed"],
        )
        kb._append_event(conn, tid, kind="claimed")
    finally:
        conn.close()

    def fake_wake_fail(self, sub, events, task, event_tasks, board):
        return False, "BoomError: spawn refused"

    monkeypatch.setattr(GatewayRunner, "_kanban_profile_wake", fake_wake_fail)

    runner = _make_runner_no_adapters()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        # Cursor rewound to 0 → next tick retries (preserve Phase 2 behavior).
        assert int(sub["last_event_id"]) == 0
        assert sub["last_wake_at"] in (None, 0)
        assert int(sub["wake_failure_count"] or 0) == 1
        assert sub["last_wake_error"] and "BoomError" in sub["last_wake_error"]
        assert int(sub["last_wake_error_at"] or 0) > 0
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] and "BoomError" in rows[0]["error"]
        assert int(rows[0]["claimed_event_cursor"]) > 0
    finally:
        conn.close()


def test_profile_wake_failure_rows_are_throttled_but_health_updates(tmp_path, monkeypatch):
    db_path = tmp_path / "profile-health-failure-throttle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="health failure throttle", assignee="worker")
        kb.add_profile_event_sub(conn, task_id=tid, profile="jensen")
        first_id = kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=10,
            old_cursor=0,
            error=RuntimeError("first"),
            at=1_000,
        )
        second_id = kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=11,
            old_cursor=0,
            error=RuntimeError("second"),
            at=1_010,
        )
        assert second_id == first_id
        sub = kb.list_profile_event_subs(conn, task_id=tid)[0]
        assert int(sub["wake_failure_count"] or 0) == 2
        assert "second" in sub["last_wake_error"]
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        assert len(rows) == 1

        third_id = kb.record_profile_wake_failure(
            conn,
            task_id=tid,
            profile="jensen",
            claimed_cursor=12,
            old_cursor=0,
            error=RuntimeError("third"),
            at=1_061,
        )
        assert third_id != first_id
        rows = kb.list_profile_wake_events(conn, task_id=tid)
        assert len(rows) == 2
    finally:
        conn.close()


def test_profile_wake_missing_profile_returns_false_and_does_not_popen(tmp_path, monkeypatch):
    """Missing profiles must be treated as spawn failure before cursor ack."""
    db_path = tmp_path / "missing-profile-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="missing profile wake", assignee="worker")
        task = kb.get_task(conn, tid)
        ev = kb.list_events(conn, tid)[0]
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    sub = {"task_id": tid, "profile": "does-not-exist", "name": ""}
    with pytest.MonkeyPatch.context() as mp:
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise AssertionError("Popen should not be reached for missing profile")
        mp.setattr("subprocess.Popen", fake_popen)
        result = runner._kanban_profile_wake(sub, [ev], task, {}, "default")

    # Phase 3: ``_kanban_profile_wake`` returns ``(ok, error_text)`` so the
    # caller can stamp the sanitized reason onto the wake-events row.
    ok, err = result if isinstance(result, tuple) else (result, None)
    assert ok is False
    assert err and "does-not-exist" in err
    assert popen_calls == []
