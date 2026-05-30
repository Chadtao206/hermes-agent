"""Single-writer daemon: the sole owner of a board's writable connection.

Clients (workers, one-shot CLI) send named write ops over an AF_UNIX socket;
the daemon executes ``kb.<op>(conn, **kwargs)`` against its owned connection,
serialized behind one lock, and returns the JSON-able result.
"""
from __future__ import annotations

import dataclasses
import fcntl
import os
import queue
import socketserver
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_protocol as proto

OP_ALLOWLIST = frozenset({
    "create_task", "complete_task", "block_task", "unblock_task",
    "link_tasks", "heartbeat_worker", "heartbeat_claim", "add_comment",
    "add_profile_event_sub",
    # kanban_list (orchestrator, a remote client) promotes ready tasks before
    # listing; the write must cross the wire since its conn is read-only.
    "recompute_ready",
    # Dashboard write endpoints (a remote client under single-writer). These are
    # legitimate kanban mutations invoked from the local, auth-gated dashboard;
    # routing them through the daemon keeps the single-writer invariant intact.
    "unlink_tasks", "reclaim_task", "reassign_task", "assign_task",
    "schedule_task", "archive_task", "add_notify_sub", "remove_notify_sub",
    "remove_profile_event_sub",
    # Dashboard PATCH /tasks field edits (status drag-drop, priority, title/body).
    "set_status_direct", "set_task_priority", "edit_task_fields",
})


_WRITER_TLS = threading.local()


def in_writer_thread() -> bool:
    """True only on a WriterDaemon's dedicated writer thread."""
    return getattr(_WRITER_TLS, "is_writer", False)


_REGISTRY: dict[str, "WriterDaemon"] = {}
_REGISTRY_LOCK = threading.Lock()


def register_daemon(db_path, daemon: "WriterDaemon") -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[str(Path(db_path).resolve())] = daemon


def unregister_daemon(db_path) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.pop(str(Path(db_path).resolve()), None)


def lookup_daemon(db_path) -> "Optional[WriterDaemon]":
    with _REGISTRY_LOCK:
        return _REGISTRY.get(str(Path(db_path).resolve()))


def _to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return value


class _WorkItem:
    __slots__ = ("op", "kwargs", "result", "error", "error_type", "exc",
                 "error_payload", "done")

    def __init__(self, op: str, kwargs: dict[str, Any]):
        self.op = op
        self.kwargs = kwargs
        self.result: Any = None
        self.error: Optional[str] = None
        self.error_type: Optional[str] = None
        # The original exception object (for in-process re-raise) and its
        # JSON-able payload (for the wire) — both set only for known typed
        # write-gate errors; None otherwise.
        self.exc: Optional[BaseException] = None
        self.error_payload: Optional[dict] = None
        self.done = threading.Event()


class WriterDaemon:
    """Owns a board's single writable connection in one dedicated thread.

    All mutations run serially on that thread, so a SIGKILL'd client can never
    leave a torn write and there is never more than one writer to the file.
    """

    def __init__(self, *, db_path: Path, socket_path: Path, pid_path: Optional[Path] = None):
        self.db_path = Path(db_path)
        self.socket_path = Path(socket_path)
        self.pid_path = Path(pid_path) if pid_path is not None else self.socket_path.with_name(".kanban-writer.pid")
        self._queue: "queue.Queue[Optional[_WorkItem]]" = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        # Set once the writer thread has opened its connection and is draining
        # the queue. Lets callers (and the gateway watchdog) wait for a real
        # ready state instead of racing thread startup.
        self._writer_ready = threading.Event()
        self._conn = None
        self._server: Optional[socketserver.UnixStreamServer] = None
        self._pid_fh = None
        # Recovery/backup state (defaults: recovery OFF, behavior unchanged).
        self._auto_recovery = False
        self._backup_dir: Optional[Path] = None
        self._backup_keep = 6
        self._backup_interval = 300
        self._disabled: Optional[dict] = None
        self._last_recovery: Optional[dict] = None
        self._backup_thread: Optional[threading.Thread] = None
        self._stop_backup = threading.Event()
        self._fail_recovery_for_test = False  # test hook: force recover_board to "fail"

    def enable_auto_recovery(self, *, backup_dir, keep: int = 6, interval: int = 300) -> None:
        self._auto_recovery = True
        self._backup_dir = Path(backup_dir)
        self._backup_keep = keep
        self._backup_interval = interval

    def force_backup_now(self):
        from hermes_cli import kanban_recovery as rec
        if self._backup_dir is None:
            return None
        return rec.make_online_backup(self.db_path, self._backup_dir, keep=self._backup_keep)

    def health(self) -> dict:
        return {
            "disabled": self._disabled is not None,
            "detail": self._disabled,
            "last_recovery": self._last_recovery,
        }

    def is_alive(self) -> bool:
        """True when the dedicated writer-loop thread is running (i.e. queued
        ops will be drained). The gateway watchdog uses this to detect a writer
        thread that died unexpectedly."""
        t = self._writer_thread
        return t is not None and t.is_alive()

    def wait_until_serving(self, timeout: float = 5.0) -> bool:
        """Block until the writer thread has opened its connection and is
        draining the queue. Returns False on timeout. Callers register a daemon
        only after this returns True so no one races thread startup."""
        return self._writer_ready.wait(timeout)

    def restart_writer_thread(self) -> bool:
        """Revive the writer-loop thread in place if it died.

        The socket server and singleton lock are left untouched, so in-process
        ``execute()`` callers (the gateway dispatcher/notifier) can write again
        immediately. The loop reopens its own writable connection on start.
        Returns True only when a fresh thread was actually started (no-op + False
        when the thread is already alive)."""
        if self.is_alive():
            return False
        self._writer_ready.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="kanban-writer-loop", daemon=True)
        self._writer_thread.start()
        return True

    def _writer_loop(self) -> None:
        _WRITER_TLS.is_writer = True
        self._conn = kb.connect(db_path=self.db_path, readonly=False)
        self._writer_ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:  # shutdown sentinel
                    break
                try:
                    item.result = self._run_op(item.op, item.kwargs)
                except Exception as exc:
                    item.error = str(exc)
                    item.error_type = type(exc).__name__
                    item.exc = exc
                    item.error_payload = kb.serialize_kanban_error(exc)
                finally:
                    item.done.set()
        finally:
            self._checkpoint_and_close()

    def _run_op(self, op: str, kwargs: dict):
        from hermes_cli import kanban_recovery as rec
        fn = getattr(kb, op)
        try:
            return fn(self._conn, **kwargs)
        except Exception as exc:
            if not (self._auto_recovery and rec.is_corruption_signal(exc)):
                raise
            self._heal()                       # close, recover_board, reopen
            if self._disabled is not None:
                raise                          # recovery exhausted; surface the error
            return fn(self._conn, **kwargs)    # retry once on the healed conn

    def _heal(self) -> None:
        from hermes_cli import kanban_recovery as rec
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        backup_dir = self._backup_dir or self.db_path.parent
        if self._fail_recovery_for_test:
            result = rec.RecoveryResult(False, "exhausted")
        else:
            result = rec.recover_board(self.db_path, backup_dir=backup_dir,
                                       keep=self._backup_keep)
        self._last_recovery = {"method": result.method, "healed": result.healed,
                               "quarantine": result.quarantine}
        if not result.healed:
            self._disabled = {"reason": "recovery_exhausted", **self._last_recovery}
        # Reopen on the writer thread (guard-exempt) so subsequent ops have a conn.
        self._conn = kb.connect(db_path=self.db_path, readonly=False)

    def _backup_loop(self) -> None:
        from hermes_cli import kanban_recovery as rec
        while not self._stop_backup.wait(self._backup_interval):
            try:
                rec.make_online_backup(self.db_path, self._backup_dir, keep=self._backup_keep)
            except Exception:
                pass  # backup is best-effort; never crash the daemon over it

    def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        req_id = payload.get("req_id", "")
        op = payload.get("op", "")
        kwargs = payload.get("kwargs") or {}
        if op not in OP_ALLOWLIST or getattr(kb, op, None) is None:
            return proto.Response(req_id, ok=False,
                                  error=f"op not allowed: {op}",
                                  error_type="UnknownOp").to_wire()
        item = _WorkItem(op, kwargs)
        self._queue.put(item)
        item.done.wait()
        if item.error is not None:
            return proto.Response(req_id, ok=False, error=item.error,
                                  error_type=item.error_type or "Error",
                                  error_payload=item.error_payload).to_wire()
        return proto.Response(req_id, ok=True, result=_to_jsonable(item.result)).to_wire()

    def execute(self, op: str, **kwargs):
        """In-process synchronous write via the single writer thread. For trusted
        in-process callers (gateway dispatcher/notifier). Returns the raw result;
        raises RuntimeError carrying the original error type/message on failure.

        Callers MUST NOT pass an op whose function itself calls write_session()
        (it would re-enqueue onto this same thread and deadlock); the kanban write
        fns operate directly on the provided conn, so this holds today."""
        if not callable(getattr(kb, op, None)):
            raise ValueError(f"unknown kanban write op: {op}")
        # Don't enqueue onto a writer thread that isn't draining the queue — a
        # stale registry entry would otherwise hang the caller on done.wait().
        if self._writer_thread is None or not self._writer_thread.is_alive():
            raise RuntimeError(f"writer daemon not running; cannot execute {op}")
        item = _WorkItem(op, kwargs)
        self._queue.put(item)
        item.done.wait()
        if item.error is not None:
            # Known write-gate exceptions are re-raised as their original typed
            # object (same process → attributes intact) so callers' `except
            # kb.<Error>` clauses match. Everything else keeps the generic
            # RuntimeError contract the dispatcher's backoff relies on.
            if item.exc is not None and item.error_payload is not None:
                raise item.exc
            raise RuntimeError(f"{item.error_type}: {item.error}")
        return item.result

    def acquire_singleton(self) -> bool:
        """Take an exclusive advisory lock on the pidfile. Returns False if another
        live process already owns this board (flock auto-releases on death, so a
        crashed prior owner does not wedge the board)."""
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid_fh = open(self.pid_path, "a+")
        try:
            fcntl.flock(self._pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._pid_fh.close()
            self._pid_fh = None
            return False
        self._pid_fh.seek(0)
        self._pid_fh.truncate()
        self._pid_fh.write(str(os.getpid()))
        self._pid_fh.flush()
        return True

    def release_singleton(self) -> None:
        if self._pid_fh is not None:
            try:
                fcntl.flock(self._pid_fh, fcntl.LOCK_UN)
            finally:
                self._pid_fh.close()
                self._pid_fh = None

    def run(self) -> int:
        """Acquire singleton then serve. No-op (return 0) if another owner exists."""
        if not self.acquire_singleton():
            return 0
        try:
            self.serve_forever()
        finally:
            self.release_singleton()
        return 0

    def serve_forever(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="kanban-writer-loop", daemon=True)
        self._writer_thread.start()
        if self._auto_recovery and self._backup_dir is not None:
            self._stop_backup.clear()
            self._backup_thread = threading.Thread(target=self._backup_loop,
                                                   name="kanban-writer-backup", daemon=True)
            self._backup_thread.start()
        daemon = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                recv = self.request.recv
                while True:
                    try:
                        payload = proto.read_frame(recv)
                    except proto.FrameError:
                        return
                    self.request.sendall(proto.encode(daemon._dispatch(payload)))

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(str(self.socket_path), Handler)
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self._stop_backup.set()  # stop the periodic backup thread
            if self._backup_thread is not None:
                self._backup_thread.join(timeout=10)
            self._queue.put(None)  # stop the writer thread -> it checkpoints+closes
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=10)
            try:
                if self.socket_path.exists():
                    self.socket_path.unlink()
            except OSError:
                pass

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def _checkpoint_and_close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            finally:
                self._conn.close()
                self._conn = None
