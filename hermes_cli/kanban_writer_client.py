"""Client side of the single-writer daemon."""
from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

from hermes_cli import kanban_writer_protocol as proto


class RemoteWriteError(Exception):
    def __init__(self, message: str, error_type: str = ""):
        super().__init__(message)
        self.error_type = error_type


class RemoteWriter:
    """Routes ``w.<op>(**kwargs)`` to the daemon over a persistent socket."""

    def __init__(self, socket_path: Path, *, connect_timeout: float = 5.0):
        self._socket_path = Path(socket_path)
        self._timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def __enter__(self) -> "RemoteWriter":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        self._sock.connect(str(self._socket_path))
        return self

    def __exit__(self, *exc) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _call(self, op: str, **kwargs: Any) -> Any:
        if self._sock is None:
            raise RemoteWriteError("writer socket not connected")
        with self._lock:
            self._seq += 1
            req = proto.Request(str(self._seq), op, kwargs)
            self._sock.sendall(proto.encode(req.to_wire()))
            resp = proto.read_frame(self._sock.recv)
        if not resp.get("ok"):
            raise RemoteWriteError(resp.get("error", "remote write failed"),
                                   resp.get("error_type", ""))
        return resp.get("result")

    def __getattr__(self, op: str):
        # Don't turn dunder/introspection lookups (copy, repr, serialization)
        # into RPC stubs — let them raise AttributeError cleanly. Only real op
        # names become RPC methods.
        if op.startswith("__"):
            raise AttributeError(op)

        def _method(**kwargs: Any) -> Any:
            return self._call(op, **kwargs)
        return _method
