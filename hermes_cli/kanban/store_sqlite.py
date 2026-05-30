from __future__ import annotations
from typing import Any, Optional
from hermes_cli import kanban_db as kb


class SqliteKanbanStore:
    """KanbanStore backed by the upstream kanban_db (sqlite3). Owns connection
    lifecycle; callers never pass a conn. Behavior is identical to calling the
    kanban_db functions directly — this is a delegating adapter."""

    def __init__(self, board: Optional[str] = None):
        self.board = board

    def close(self) -> None:  # no persistent conn held; nothing to close
        return None

    # --- helpers ---------------------------------------------------------
    def _read(self, fn):
        """Run a read closure on a fresh read connection (snapshot under
        single-writer, else writable). fn receives the conn."""
        conn = _read_conn(self.board)
        try:
            return fn(conn)
        finally:
            conn.close()

    def _write(self, op: str, **kwargs):
        """Run a single write op via write_session (daemon under single-writer,
        else a local writable conn)."""
        with kb.write_session(board=self.board) as w:
            return getattr(w, op)(**kwargs)

    # --- task lifecycle --------------------------------------------------
    def create_task(self, **kwargs: Any) -> str:
        return self._write("create_task", **kwargs)

    def get_task(self, task_id: str):
        return self._read(lambda c: kb.get_task(c, task_id))

    def list_tasks(self, **kwargs: Any):
        return self._read(lambda c: kb.list_tasks(c, **kwargs))

    def complete_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("complete_task", task_id=task_id, **kwargs)

    def block_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("block_task", task_id=task_id, **kwargs)

    def unblock_task(self, task_id: str) -> bool:
        return self._write("unblock_task", task_id=task_id)

    def schedule_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("schedule_task", task_id=task_id, **kwargs)

    def archive_task(self, task_id: str) -> bool:
        return self._write("archive_task", task_id=task_id)

    def assign_task(self, task_id: str, profile: Optional[str]) -> bool:
        return self._write("assign_task", task_id=task_id, profile=profile)

    def reassign_task(self, task_id: str, profile: Optional[str], **kwargs: Any) -> bool:
        return self._write("reassign_task", task_id=task_id, profile=profile, **kwargs)

    def reclaim_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("reclaim_task", task_id=task_id, **kwargs)

    def set_status_direct(self, task_id: str, new_status: str) -> bool:
        return self._write("set_status_direct", task_id=task_id, new_status=new_status)

    def set_task_priority(self, task_id: str, priority: int) -> bool:
        return self._write("set_task_priority", task_id=task_id, priority=priority)

    def edit_task_fields(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("edit_task_fields", task_id=task_id, **kwargs)

    def delete_task(self, task_id: str) -> bool:
        return self._write("delete_task", task_id=task_id)


class _SnapshotReadConn:
    """Closeable wrapper around :func:`kb.snapshot_connect` so callers that do
    ``kb, conn = _connect(...); try: ...; finally: conn.close()`` get a
    consistent snapshot reader (read-after-write safe under WAL/SHM churn)."""

    def __init__(self, cm):
        self._cm = cm
        self._conn = cm.__enter__()
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if not self._closed:
            self._closed = True
            self._cm.__exit__(None, None, None)


def _read_conn(board):
    """Mirror tools.kanban_tools._connect read policy: snapshot under
    single-writer, else writable connect."""
    if kb.single_writer_enabled():
        return _SnapshotReadConn(kb.snapshot_connect(board=board))
    return kb.connect(board=board)
