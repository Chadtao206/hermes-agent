"""Length-prefixed JSON wire protocol for the kanban single-writer daemon.

Frame = 4-byte big-endian unsigned length || UTF-8 JSON payload.
Kept deliberately tiny: the daemon and client are the only users.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MiB ceiling; task bodies are small


class FrameError(Exception):
    """Raised on truncated, oversize, or malformed frames."""


def encode(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(f"frame too large: {len(body)} bytes")
    return len(body).to_bytes(4, "big") + body


def read_frame(recv_exact: Callable[[int], bytes]) -> dict[str, Any]:
    """Read one frame using a ``recv_exact(n)``-style reader.

    ``recv_exact`` must return exactly ``n`` bytes or fewer at EOF.
    """
    header = _read_n(recv_exact, 4)
    length = int.from_bytes(header, "big")
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"declared frame too large: {length} bytes")
    body = _read_n(recv_exact, length)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError(f"malformed JSON frame: {exc}") from exc


def _read_n(recv_exact: Callable[[int], bytes], n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = recv_exact(remaining)
        if not chunk:
            raise FrameError(f"unexpected EOF: wanted {n} bytes, short by {remaining}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass
class Request:
    req_id: str
    op: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"req_id": self.req_id, "op": self.op, "kwargs": self.kwargs}


@dataclass
class Response:
    req_id: str
    ok: bool
    result: Any = None
    error: str = ""
    error_type: str = ""
    # JSON-able payload for reconstructing a known typed exception client-side
    # (see kanban_db.serialize_kanban_error). None for non-reconstructable errors.
    error_payload: Any = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id, "ok": self.ok, "result": self.result,
            "error": self.error, "error_type": self.error_type,
            "error_payload": self.error_payload,
        }
