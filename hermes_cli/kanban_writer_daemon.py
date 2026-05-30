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
    "link_tasks", "heartbeat_worker", "add_profile_event_sub",
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
    __slots__ = ("op", "kwargs", "result", "error", "error_type", "done")

    def __init__(self, op: str, kwargs: dict[str, Any]):
        self.op = op
        self.kwargs = kwargs
        self.result: Any = None
        self.error: Optional[str] = None
        self.error_type: Optional[str] = None
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
        self._conn = None
        self._server: Optional[socketserver.UnixStreamServer] = None
        self._pid_fh = None

    def _writer_loop(self) -> None:
        _WRITER_TLS.is_writer = True
        self._conn = kb.connect(db_path=self.db_path, readonly=False)
        try:
            while True:
                item = self._queue.get()
                if item is None:  # shutdown sentinel
                    break
                try:
                    fn = getattr(kb, item.op)
                    item.result = fn(self._conn, **item.kwargs)
                except Exception as exc:
                    item.error = str(exc)
                    item.error_type = type(exc).__name__
                finally:
                    item.done.set()
        finally:
            self._checkpoint_and_close()

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
                                  error_type=item.error_type or "Error").to_wire()
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
