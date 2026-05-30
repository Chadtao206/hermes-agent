# WS1 — Single-Writer Daemon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Funnel all kanban DB *writes* through a single owner process so concurrent writers
and SIGKILL-mid-write torn pages become structurally impossible, while keeping SQLite, the
schema, and all read paths unchanged.

**Architecture:** One writable connection per board lives in an *owner* process (the gateway,
or a standalone `hermes kanban db-daemon`). The owner runs in-process write paths (dispatcher
claim, notifier heartbeat, reclaim) directly. *Client* processes (worker subprocesses, one-shot
CLI commands) route writes over a per-board Unix-domain socket using a length-prefixed JSON
protocol; the daemon executes the named `kb.<fn>(conn, **kwargs)` against its owned connection
and returns the JSON-able result. Reads stay direct `mode=ro` everywhere and are unaffected.
All of this is behind `kanban.single_writer_daemon` (default `false`).

**Tech Stack:** Python 3, `socket`/`socketserver` (AF_UNIX), `sqlite3`, existing `hermes_cli`
modules.

**Key existing anchors (verified):**
- `hermes_cli/kanban_db.py:1653` `def connect(...)` — writable branch `:1708-1760`, read-only
  branch `:1690-1706` (`mode=ro`, `query_only`).
- Write functions all take `conn` first: `create_task:2436`, `link_tasks:2816`,
  `_append_event:3072`, `recompute_ready:3699`, `claim_task:3747`, `heartbeat_claim:3937`,
  `release_stale_claims:3968`, `reclaim_task:4081`, `complete_task:4313`, `block_task:4849`,
  `unblock_task:5119`, `heartbeat_worker:6054`, `detect_crashed_workers:6401`,
  `add_profile_event_sub:8947`, `record_profile_wake_success:9330`,
  `record_profile_wake_failure:9368`, `record_notifier_heartbeat:9507`.
- Worker spawn env contract: `_default_spawn:7913`, injects `HERMES_KANBAN_DB:8000`,
  `HERMES_KANBAN_WORKSPACES_ROOT:8001`, `HERMES_KANBAN_BOARD:8006`, `HERMES_KANBAN_TASK:7974`.
- Tool write handlers open via `_connect(board)` → `kb.connect(board=board)` in
  `tools/kanban_tools.py:164-176`; handlers at `:743` (create), `:618` (block), plus
  complete/comment/link/heartbeat/unblock nearby.
- `kanban_db_path(board=...)` resolves the board file; board dir is its parent.

---

## File structure

- **Create** `hermes_cli/kanban_writer_protocol.py` — socket framing + request/response codec
  (shared by daemon and client). One responsibility: wire format.
- **Create** `hermes_cli/kanban_writer_daemon.py` — the owner-side server: owns one writable
  conn per board, serves ops, lifecycle (pidfile/socket/shutdown-with-checkpoint).
- **Create** `hermes_cli/kanban_writer_client.py` — `RemoteWriter` (socket client) + the
  `write_session()` façade selection logic.
- **Modify** `hermes_cli/kanban_db.py` — add `write_session()`, `single_writer_enabled()`,
  `writer_socket_path()`, and the direct-writable-open guard in `connect()`.
- **Modify** `tools/kanban_tools.py` — route write handlers through `write_session`.
- **Modify** `hermes_cli/kanban.py` — add `hermes kanban db-daemon` subcommand.
- **Modify** `gateway/run.py` — add `_start_kanban_writer_daemon()` + one start call.
- **Modify** `config.yaml` (+ example) — document `kanban.single_writer_daemon`.
- **Tests** `tests/hermes_cli/test_kanban_writer_protocol.py`,
  `tests/hermes_cli/test_kanban_writer_daemon.py`,
  `tests/hermes_cli/test_kanban_writer_client.py`,
  `tests/hermes_cli/test_kanban_single_writer_guard.py`,
  `tests/hermes_cli/test_kanban_writer_integration.py`.

---

### Task 1: Wire protocol (framing + codec)

**Files:**
- Create: `hermes_cli/kanban_writer_protocol.py`
- Test: `tests/hermes_cli/test_kanban_writer_protocol.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_writer_protocol.py
import io
import pytest
from hermes_cli import kanban_writer_protocol as proto


def test_roundtrip_request():
    req = proto.Request(req_id="r1", op="create_task", kwargs={"title": "x", "assignee": "engineer"})
    blob = proto.encode(req.to_wire())
    buf = io.BytesIO(blob)
    got = proto.read_frame(buf.read)
    assert got == req.to_wire()


def test_partial_frame_raises_eof():
    # 4-byte length header says 10 bytes, but only 3 follow.
    truncated = (10).to_bytes(4, "big") + b"abc"
    buf = io.BytesIO(truncated)
    with pytest.raises(proto.FrameError):
        proto.read_frame(buf.read)


def test_oversize_frame_rejected():
    too_big = (proto.MAX_FRAME_BYTES + 1).to_bytes(4, "big")
    buf = io.BytesIO(too_big)
    with pytest.raises(proto.FrameError):
        proto.read_frame(buf.read)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.kanban_writer_protocol`.

- [ ] **Step 3: Write minimal implementation**

```python
# hermes_cli/kanban_writer_protocol.py
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

    def to_wire(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id, "ok": self.ok, "result": self.result,
            "error": self.error, "error_type": self.error_type,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_protocol.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_writer_protocol.py tests/hermes_cli/test_kanban_writer_protocol.py
git commit -m "feat(kanban): add single-writer daemon wire protocol"
```

---

### Task 2: Daemon server core (owns the writable conn, serves ops)

**Files:**
- Create: `hermes_cli/kanban_writer_daemon.py`
- Test: `tests/hermes_cli/test_kanban_writer_daemon.py`

**Design notes:**
- `OP_ALLOWLIST` names exactly the write functions reachable over the socket. Anything else →
  `Response(ok=False, error_type="UnknownOp")`. This is the security/scope boundary.
- The daemon holds one writable conn (`kb.connect(board=..., readonly=False)`), serves requests
  **single-threaded** (one `socketserver` with a global write lock), so writes are serialized in
  one process — SQLite's writer lock is no longer contended across processes.
- Results are passed through `_to_jsonable` (dataclasses → `.to_dict()` if present, else
  `vars()`; everything else must already be JSON-able).

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_writer_daemon.py
import socket
import threading
import time
from pathlib import Path

from hermes_cli import kanban_writer_protocol as proto
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_db as kb


def _client_call(sock_path, op, **kwargs):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(sock_path))
    s.sendall(proto.encode(proto.Request("t", op, kwargs).to_wire()))
    resp = proto.read_frame(lambda n: s.recv(n))
    s.close()
    return resp


def test_daemon_serves_create_task(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.02)
        resp = _client_call(sock, "create_task", title="hello", assignee="engineer")
        assert resp["ok"] is True
        new_id = resp["result"]
        # The write landed in the real DB, readable via a normal ro connection.
        conn = kb.connect(db_path=db, readonly=True)
        row = conn.execute("SELECT title, assignee FROM tasks WHERE id = ?", (new_id,)).fetchone()
        assert row["title"] == "hello"
        assert row["assignee"] == "engineer"
    finally:
        server.shutdown()


def test_daemon_rejects_unknown_op(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.02)
        resp = _client_call(sock, "DROP_TABLE", x=1)
        assert resp["ok"] is False
        assert resp["error_type"] == "UnknownOp"
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_daemon.py -v`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.kanban_writer_daemon`.

- [ ] **Step 3: Write minimal implementation**

```python
# hermes_cli/kanban_writer_daemon.py
"""Single-writer daemon: the sole owner of a board's writable connection.

Clients (workers, one-shot CLI) send named write ops over an AF_UNIX socket;
the daemon executes ``kb.<op>(conn, **kwargs)`` against its owned connection,
serialized behind one lock, and returns the JSON-able result.
"""
from __future__ import annotations

import dataclasses
import os
import socketserver
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_writer_protocol as proto

# Exactly the writes reachable over the socket. Extend deliberately.
OP_ALLOWLIST = frozenset({
    "create_task", "complete_task", "block_task", "unblock_task",
    "link_tasks", "heartbeat_worker", "add_profile_event_sub",
})


def _to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return value


class WriterDaemon:
    def __init__(self, *, db_path: Path, socket_path: Path):
        self.db_path = Path(db_path)
        self.socket_path = Path(socket_path)
        self._write_lock = threading.Lock()
        self._conn = None
        self._server: Optional[socketserver.UnixStreamServer] = None

    def _conn_or_open(self):
        if self._conn is None:
            # Owner process: this is the ONE writable connection for the board.
            os.environ["HERMES_KANBAN_WRITER_OWNER"] = "1"
            self._conn = kb.connect(db_path=self.db_path, readonly=False)
        return self._conn

    def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = proto.Request(payload.get("req_id", ""), payload.get("op", ""),
                            payload.get("kwargs") or {})
        if req.op not in OP_ALLOWLIST:
            return proto.Response(req.req_id, ok=False,
                                  error=f"op not allowed: {req.op}",
                                  error_type="UnknownOp").to_wire()
        fn = getattr(kb, req.op, None)
        if fn is None:
            return proto.Response(req.req_id, ok=False,
                                  error=f"no such op: {req.op}",
                                  error_type="UnknownOp").to_wire()
        try:
            with self._write_lock:
                conn = self._conn_or_open()
                result = fn(conn, **req.kwargs)
            return proto.Response(req.req_id, ok=True,
                                  result=_to_jsonable(result)).to_wire()
        except Exception as exc:  # surfaced to the client as a structured error
            return proto.Response(req.req_id, ok=False, error=str(exc),
                                  error_type=type(exc).__name__).to_wire()

    def serve_forever(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        daemon = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                recv = self.request.recv
                while True:
                    try:
                        payload = proto.read_frame(recv)
                    except proto.FrameError:
                        return  # client closed or framing broke; drop connection
                    resp = daemon._dispatch(payload)
                    self.request.sendall(proto.encode(resp))

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            # ThreadingMixIn accepts concurrent clients; the write lock still
            # serializes every actual mutation through the single connection.
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server(str(self.socket_path), Handler)
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self._checkpoint_and_close()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def _checkpoint_and_close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._conn.close()
                self._conn = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_daemon.py -v`
Expected: PASS (2 tests). If `create_task`'s real signature requires extra kwargs, read
`kanban_db.py:2436` and pass them in the test — keep the test faithful to the real signature.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_writer_daemon.py tests/hermes_cli/test_kanban_writer_daemon.py
git commit -m "feat(kanban): writer daemon core serving allowlisted ops over AF_UNIX"
```

---

### Task 3: Client (`RemoteWriter`) + `write_session()` façade

**Files:**
- Create: `hermes_cli/kanban_writer_client.py`
- Modify: `hermes_cli/kanban_db.py` (add `write_session`, `single_writer_enabled`,
  `writer_socket_path`)
- Test: `tests/hermes_cli/test_kanban_writer_client.py`

**Design:** `write_session(board=...)` is the *only* way write call-sites mutate the board.
- In the **owner** process (`HERMES_KANBAN_WRITER_OWNER=1`) or when the flag is **off**, it
  yields a `LocalWriter` that holds a real writable conn and calls `kb.<fn>(conn, **kwargs)`
  via `__getattr__`.
- In a **client** process with the flag on, it yields a `RemoteWriter` that RPCs each call.
  Both expose the same `w.create_task(**kwargs)` surface, so call-sites are transport-agnostic.

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_writer_client.py
import threading
import time
from pathlib import Path

from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_writer_client as wc
from hermes_cli import kanban_db as kb


def _start_daemon(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.02)
    return db, sock, server


def test_remote_writer_create_roundtrips(tmp_path):
    db, sock, server = _start_daemon(tmp_path)
    try:
        with wc.RemoteWriter(sock) as w:
            new_id = w.create_task(title="via-client", assignee="engineer")
        conn = kb.connect(db_path=db, readonly=True)
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (new_id,)).fetchone()
        assert row["title"] == "via-client"
    finally:
        server.shutdown()


def test_remote_writer_raises_on_daemon_error(tmp_path):
    db, sock, server = _start_daemon(tmp_path)
    try:
        with wc.RemoteWriter(sock) as w:
            try:
                w.create_task()  # missing required kwargs -> daemon raises
                assert False, "expected RemoteWriteError"
            except wc.RemoteWriteError as exc:
                assert exc.error_type  # carries the daemon-side exception type
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_client.py -v`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.kanban_writer_client`.

- [ ] **Step 3: Write minimal implementation**

```python
# hermes_cli/kanban_writer_client.py
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
        # Any attribute access that isn't a real field becomes an RPC method.
        def _method(**kwargs: Any) -> Any:
            return self._call(op, **kwargs)
        return _method
```

Now add the façade + helpers to `kanban_db.py`. **Read `kanban_db.py:1653` `connect()` and the
module's config-loading helper first**, then add near the other connection helpers (after
`connect_closing`, ~`:1812`):

```python
# hermes_cli/kanban_db.py  (new helpers)
import contextlib as _contextlib  # if not already imported at module top

def single_writer_enabled() -> bool:
    """True when kanban.single_writer_daemon is on in config (cached per process)."""
    cfg = _load_kanban_config()  # use the module's existing config accessor
    return bool(cfg.get("single_writer_daemon", False))

def writer_socket_path(*, board: Optional[str] = None) -> Path:
    """Per-board daemon socket: alongside the board DB file."""
    return kanban_db_path(board=board).parent / ".kanban-writer.sock"

def _is_writer_owner() -> bool:
    return os.environ.get("HERMES_KANBAN_WRITER_OWNER") == "1"

class _LocalWriter:
    """Owner-side / flag-off writer: calls module write fns on a real conn."""
    def __init__(self, conn):
        self._conn = conn
    def __getattr__(self, op: str):
        fn = globals().get(op)
        if fn is None:
            raise AttributeError(op)
        def _method(**kwargs):
            return fn(self._conn, **kwargs)
        return _method

@_contextlib.contextmanager
def write_session(*, board: Optional[str] = None):
    """Yield a transport-agnostic writer (LocalWriter or RemoteWriter).

    Every cross-process write MUST go through this. In the owner process or
    when the flag is off, it is a direct connection; in a client process with
    the flag on, it proxies to the daemon.
    """
    if single_writer_enabled() and not _is_writer_owner():
        from hermes_cli.kanban_writer_client import RemoteWriter
        with RemoteWriter(writer_socket_path(board=board)) as w:
            yield w
        return
    conn = connect(board=board, readonly=False)
    try:
        yield _LocalWriter(conn)
    finally:
        conn.close()
```

> Use the module's real config accessor in `single_writer_enabled()` — find how other
> `kanban.*` settings are read (e.g. a `_kanban_cfg()`/`load_config()` helper) and match it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_client.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_writer_client.py hermes_cli/kanban_db.py \
        tests/hermes_cli/test_kanban_writer_client.py
git commit -m "feat(kanban): add write_session facade + RemoteWriter client"
```

---

### Task 4: Guard direct writable opens in client processes

**Files:**
- Modify: `hermes_cli/kanban_db.py:1653` (`connect`)
- Test: `tests/hermes_cli/test_kanban_single_writer_guard.py`

**Why:** the guard is the safety net that makes "we migrated every writer" *enforceable* — if
any code still opens a writable conn directly in a client process under the flag, it fails loud
instead of silently re-introducing a second writer.

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_single_writer_guard.py
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


def test_readonly_connect_always_allowed(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.delenv("HERMES_KANBAN_WRITER_OWNER", raising=False)
    kb.connect(db_path=db, readonly=False, _bootstrap=True).close()  # create file
    kb.connect(db_path=db, readonly=True).close()  # ro never blocked
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/hermes_cli/test_kanban_single_writer_guard.py -v`
Expected: FAIL — `AttributeError: DirectWriteForbidden` / guard not present.

- [ ] **Step 3: Write minimal implementation**

In `kanban_db.py`, add the exception near the other kanban exceptions, and add the guard as the
first lines of the **writable branch** of `connect()` (right after the `readonly` check, before
`:1708`). Add a private `_bootstrap` kwarg so the daemon/tests can create the file:

```python
class DirectWriteForbidden(RuntimeError):
    """Raised when a client process opens a writable kanban conn under the
    single-writer daemon flag. All writes must go through write_session()."""


# inside connect(...), at the top of the writable (non-readonly) path:
if (single_writer_enabled() and not _is_writer_owner()
        and not _bootstrap):
    raise DirectWriteForbidden(
        "writable kanban.connect() called in a client process while "
        "kanban.single_writer_daemon is enabled; use kb.write_session() instead"
    )
```

Update the `connect` signature to accept `_bootstrap: bool = False`. The owner daemon
(`_conn_or_open`) and the `write_session` LocalWriter set `HERMES_KANBAN_WRITER_OWNER` or run
flag-off, so neither trips the guard.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/hermes_cli/test_kanban_single_writer_guard.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_single_writer_guard.py
git commit -m "feat(kanban): forbid direct writable opens in client processes under flag"
```

---

### Task 5: Daemon lifecycle — pidfile, singleton, `hermes kanban db-daemon`

**Files:**
- Modify: `hermes_cli/kanban_writer_daemon.py` (pidfile/singleton + `run()` entrypoint)
- Modify: `hermes_cli/kanban.py` (add `db-daemon` subcommand)
- Test: extend `tests/hermes_cli/test_kanban_writer_daemon.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/hermes_cli/test_kanban_writer_daemon.py
def test_second_daemon_refuses_when_one_owns_board(tmp_path):
    db = tmp_path / "kanban.db"
    sock = tmp_path / ".kanban-writer.sock"
    pid = tmp_path / ".kanban-writer.pid"
    s1 = wd.WriterDaemon(db_path=db, socket_path=sock, pid_path=pid)
    assert s1.acquire_singleton() is True
    s2 = wd.WriterDaemon(db_path=db, socket_path=sock, pid_path=pid)
    assert s2.acquire_singleton() is False  # first owner holds the board
    s1.release_singleton()
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_daemon.py::test_second_daemon_refuses_when_one_owns_board -v`
Expected: FAIL — `TypeError: unexpected keyword 'pid_path'` / no `acquire_singleton`.

- [ ] **Step 3: Implement**

Add to `WriterDaemon.__init__` a `pid_path: Optional[Path] = None` (default
`socket_path.with_name(".kanban-writer.pid")`). Add singleton methods using an exclusive
`fcntl.flock` on the pidfile (stale-PID safe because flock releases on process death):

```python
import fcntl, os

def acquire_singleton(self) -> bool:
    self._pid_fh = open(self.pid_path, "a+")
    try:
        fcntl.flock(self._pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        self._pid_fh.close()
        self._pid_fh = None
        return False
    self._pid_fh.seek(0); self._pid_fh.truncate()
    self._pid_fh.write(str(os.getpid())); self._pid_fh.flush()
    return True

def release_singleton(self) -> None:
    if getattr(self, "_pid_fh", None) is not None:
        try:
            fcntl.flock(self._pid_fh, fcntl.LOCK_UN)
        finally:
            self._pid_fh.close()
            self._pid_fh = None

def run(self) -> int:
    if not self.acquire_singleton():
        return 0  # another owner already serves this board
    try:
        self.serve_forever()
    finally:
        self.release_singleton()
    return 0
```

Then add the CLI subcommand in `hermes_cli/kanban.py` (find the argparse subparser setup and
the `_cmd_*` dispatch; mirror an existing daemon-style command like `kanban daemon`):

```python
def _cmd_db_daemon(args) -> int:
    from hermes_cli.kanban_writer_daemon import WriterDaemon
    import hermes_cli.kanban_db as kb
    daemon = WriterDaemon(
        db_path=kb.kanban_db_path(board=getattr(args, "board", None)),
        socket_path=kb.writer_socket_path(board=getattr(args, "board", None)),
    )
    return daemon.run()
```

Register: `sub.add_parser("db-daemon", help="Run the single-writer daemon for a board")`
with an optional `--board`, wired to `_cmd_db_daemon`.

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_daemon.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_writer_daemon.py hermes_cli/kanban.py \
        tests/hermes_cli/test_kanban_writer_daemon.py
git commit -m "feat(kanban): daemon singleton lock + 'hermes kanban db-daemon' command"
```

---

### Task 6: Gateway starts the daemon as a managed subsystem

**Files:**
- Modify: `gateway/run.py` (add `_start_kanban_writer_daemon()` + one start call near
  `_start_cron_ticker()` ~`:20016`/`:20499`)

> **Merge-tax note:** add a *new* method and a *single* call line. Do not edit the dispatcher
> or notifier internals here.

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_kanban_writer_startup.py
from pathlib import Path
import gateway.run as gr

def test_gateway_skips_daemon_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr(gr, "_single_writer_flag", lambda: False, raising=False)
    started = gr._kanban_writer_daemon_should_start()  # helper under test
    assert started is False
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/gateway/test_kanban_writer_startup.py -v`
Expected: FAIL — helper not defined.

- [ ] **Step 3: Implement**

In `gateway/run.py`, add a small pure helper and a starter. The starter sets
`HERMES_KANBAN_WRITER_OWNER=1` in the gateway process *before* the dispatcher/notifier open
their connections, and launches `WriterDaemon.run()` on a daemon thread for each configured
board:

```python
def _kanban_writer_daemon_should_start() -> bool:
    import hermes_cli.kanban_db as kb
    return bool(kb.single_writer_enabled())

def _start_kanban_writer_daemon(self) -> None:
    if not _kanban_writer_daemon_should_start():
        return
    import os, threading
    import hermes_cli.kanban_db as kb
    from hermes_cli.kanban_writer_daemon import WriterDaemon
    os.environ["HERMES_KANBAN_WRITER_OWNER"] = "1"  # gateway is the owner
    for slug in self._configured_board_slugs():  # reuse existing board enumeration
        daemon = WriterDaemon(db_path=kb.kanban_db_path(board=slug),
                              socket_path=kb.writer_socket_path(board=slug))
        if daemon.acquire_singleton():
            threading.Thread(target=daemon.serve_forever, daemon=True,
                             name=f"kanban-writer-{slug}").start()
            self._kanban_writer_daemons.append(daemon)
```

Call `self._start_kanban_writer_daemon()` immediately before the dispatcher/notifier watchers
are started in `start_gateway()` (so the owner env + socket exist first). Initialize
`self._kanban_writer_daemons = []` in `GatewayRunner.__init__`, and call
`daemon.shutdown()` for each in the gateway's shutdown path. Reuse the existing board-slug
enumeration the dispatcher/notifier already use (search for where `slug` boards are iterated,
~`:5540`/`:6707`).

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/gateway/test_kanban_writer_startup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_kanban_writer_startup.py
git commit -m "feat(gateway): own kanban writer daemon as a managed subsystem"
```

---

### Task 7: Migrate worker/CLI write handlers to `write_session`

**Files:**
- Modify: `tools/kanban_tools.py` (handlers: `_handle_create:743`, `_handle_block:618`,
  complete/comment/link/heartbeat/add-sub handlers nearby)

**Pattern:** today each write handler does `kb, conn = _connect(board)` then
`kb.create_task(conn, ...)`. Change *write* handlers to:

```python
with kb.write_session(board=board) as w:
    new_id = w.create_task(title=title, assignee=assignee, body=body, ...)
```

Leave **read** handlers (`kanban_show`, `kanban_list`) on the existing read path unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_tools_write_session.py
import threading, time
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_db as kb
import tools.kanban_tools as kt


def test_create_handler_uses_daemon_under_flag(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(50):
        if sock.exists(): break
        time.sleep(0.02)
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db)
    monkeypatch.setattr(kb, "writer_socket_path", lambda board=None: sock)
    monkeypatch.delenv("HERMES_KANBAN_WRITER_OWNER", raising=False)
    try:
        res = kt._handle_create({"title": "t-flag", "assignee": "engineer"})
        assert "error" not in (res or {}).get("type", "")  # adapt to real return shape
        conn = kb.connect(db_path=db, readonly=True)
        assert conn.execute("SELECT COUNT(*) c FROM tasks WHERE title='t-flag'").fetchone()["c"] == 1
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/hermes_cli/test_kanban_tools_write_session.py -v`
Expected: FAIL — handler still opens a direct writable conn → `DirectWriteForbidden`.

- [ ] **Step 3: Implement**

Edit each *write* handler in `tools/kanban_tools.py` to use `with kb.write_session(board=...)`.
Read each handler first; preserve its validation, error wrapping, and return shape — only the
DB-mutation lines change. Map: `kb.create_task(conn,...)`→`w.create_task(...)`,
`kb.block_task(conn,...)`→`w.block_task(...)`, `kb.complete_task`→`w.complete_task`,
`kb.unblock_task`→`w.unblock_task`, `kb.link_tasks`→`w.link_tasks`,
`kb.heartbeat_worker`→`w.heartbeat_worker`, `kb.add_profile_event_sub`→`w.add_profile_event_sub`.
(Comment handling routes through whichever write fn it uses today — migrate it the same way and
add its op to `OP_ALLOWLIST` if missing.)

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/hermes_cli/test_kanban_tools_write_session.py tests/hermes_cli/ -k kanban -v`
Expected: PASS, and no existing kanban-tool test regresses.

- [ ] **Step 5: Commit**

```bash
git add tools/kanban_tools.py tests/hermes_cli/test_kanban_tools_write_session.py
git commit -m "refactor(kanban): route tool write handlers through write_session"
```

---

### Task 8: Worker spawn — point clients at the socket, drop writable DB handle

**Files:**
- Modify: `hermes_cli/kanban_db.py:_default_spawn` (~`:7960-8019`)

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_spawn_env.py
from hermes_cli import kanban_db as kb

def test_spawn_env_includes_writer_socket_under_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "single_writer_enabled", lambda: True)
    monkeypatch.setattr(kb, "writer_socket_path",
                        lambda board=None: tmp_path / ".kanban-writer.sock")
    env = kb._build_worker_env_for_test(board="default")  # thin testable extract
    assert env["HERMES_KANBAN_WRITER_SOCK"].endswith(".kanban-writer.sock")
    assert env.get("HERMES_KANBAN_WRITER_OWNER") is None  # workers are NOT owners
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/hermes_cli/test_kanban_spawn_env.py -v`
Expected: FAIL — env key absent / no test extract.

- [ ] **Step 3: Implement**

In `_default_spawn`, where the env dict is built (`:7974-8006`), add under the flag:

```python
if single_writer_enabled():
    env["HERMES_KANBAN_WRITER_SOCK"] = str(writer_socket_path(board=board))
    env.pop("HERMES_KANBAN_WRITER_OWNER", None)  # workers must never be owners
```

`HERMES_KANBAN_DB` stays set (reads still open ro directly). The worker's tool handlers now hit
`write_session` → `RemoteWriter` for writes; the guard from Task 4 blocks any stray direct
writable open. Extract the env-building lines into a small `_build_worker_env(...)` helper so the
test can call it without spawning a real subprocess.

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/hermes_cli/test_kanban_spawn_env.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_spawn_env.py
git commit -m "feat(kanban): spawn workers as writer clients (socket, never owner)"
```

---

### Task 9: Integration — concurrent + killed clients cannot corrupt; config flag

**Files:**
- Modify: `config.yaml` and `cli-config.yaml.example` (document the flag)
- Test: `tests/hermes_cli/test_kanban_writer_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_writer_integration.py
import os, signal, threading, time, multiprocessing as mp
from pathlib import Path
from hermes_cli import kanban_writer_daemon as wd
from hermes_cli import kanban_writer_client as wc
from hermes_cli import kanban_db as kb


def _spam(sock_path, n, label):
    with wc.RemoteWriter(Path(sock_path)) as w:
        for i in range(n):
            w.create_task(title=f"{label}-{i}", assignee="engineer")


def test_many_concurrent_clients_then_kill_one_keeps_db_healthy(tmp_path):
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(100):
        if sock.exists(): break
        time.sleep(0.02)
    try:
        threads = [threading.Thread(target=_spam, args=(sock, 40, f"c{n}"))
                   for n in range(6)]
        for t in threads: t.start()
        # Kill a client mid-flight: it can hold NO writable DB handle, so the
        # board cannot be torn — only the daemon writes.
        victim = mp.Process(target=_spam, args=(str(sock), 1000, "victim"))
        victim.start(); time.sleep(0.2); os.kill(victim.pid, signal.SIGKILL)
        for t in threads: t.join()
        # DB still passes integrity check -> the structural cure holds.
        ro = kb.connect(db_path=db, readonly=True)
        assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert ro.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] >= 240
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_integration.py -v`
Expected: PASS once Tasks 1–3 landed (this test exercises only daemon+client). If it does not
pass, fix the daemon/client before proceeding — this is the WS1 acceptance gate.

- [ ] **Step 3: Document the flag**

Add to `config.yaml` under `kanban:` (and mirror in `cli-config.yaml.example`):

```yaml
kanban:
  # Route all board WRITES through a single owner process (the gateway, or
  # `hermes kanban db-daemon`). Eliminates concurrent-writer corruption.
  # Reads stay direct/read-only. Default false for backward compatibility.
  single_writer_daemon: false
```

- [ ] **Step 4: Full kanban suite green**

Run: `python -m pytest tests/hermes_cli/ tests/gateway/ -k "kanban or writer" -v`
Expected: PASS. Then manually: start gateway with the flag on, create a task via chat, confirm
a worker spawns, writes land, and `hermes kanban show <id>` reflects them.

- [ ] **Step 5: Commit**

```bash
git add config.yaml cli-config.yaml.example tests/hermes_cli/test_kanban_writer_integration.py
git commit -m "test(kanban): integration proof killed client cannot corrupt board; doc flag"
```

---

## WS1 acceptance criteria

- With `single_writer_daemon: true`, **no process other than the owner holds a writable conn** —
  the Task 4 guard makes a stray direct write fail loud.
- A `SIGKILL`'d worker/client cannot leave a torn page (Task 9 integration proof).
- All existing kanban tests pass with the flag both **off** (default, unchanged behavior) and
  **on**.
- Graceful daemon shutdown checkpoints the WAL (`wal_checkpoint(TRUNCATE)`).

## Self-review notes

- Method names are consistent across tasks: `write_session`, `single_writer_enabled`,
  `writer_socket_path`, `WriterDaemon`, `RemoteWriter`, `OP_ALLOWLIST`, `DirectWriteForbidden`,
  `_is_writer_owner`, `acquire_singleton`/`release_singleton`.
- `_load_kanban_config()`/`_kanban_cfg()` is a stand-in: the executor must bind it to the real
  config accessor in `kanban_db.py`. Same for `_configured_board_slugs()` in `gateway/run.py`.
- `complete_task`/`unblock_task` return types: verify they are JSON-able; if either returns a
  dataclass, `_to_jsonable` already handles it via `dataclasses.asdict`.
