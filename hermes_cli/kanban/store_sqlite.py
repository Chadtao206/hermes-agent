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

    def complete_task(self, task_id: str, *, on_cleanup=None,
                       **kwargs: Any) -> bool:
        # on_cleanup is part of the cross-backend complete_task contract but
        # is a no-op here: kanban_db.complete_task does its own
        # _cleanup_workspace internally and does NOT accept this kwarg, so we
        # drop it before delegating. Behavior on sqlite is unchanged.
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

    def claim_task(self, task_id: str, **kwargs: Any):
        return self._write("claim_task", task_id=task_id, **kwargs)

    def reclaim_task(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("reclaim_task", task_id=task_id, **kwargs)

    def record_task_failure(self, task_id, error, *, outcome, failure_limit=None,
                            failure_limit_is_cap=False, release_claim=True,
                            end_run=True, event_payload_extra=None) -> bool:
        return self._write("record_task_failure", task_id=task_id, error=error,
                           outcome=outcome, failure_limit=failure_limit,
                           failure_limit_is_cap=failure_limit_is_cap,
                           release_claim=release_claim, end_run=end_run,
                           event_payload_extra=event_payload_extra)

    def auto_block_unclosed_worker_turn(
        self,
        task_id: str,
        *,
        final_response: Optional[str] = None,
        expected_run_id: Optional[int] = None,
        expected_claim_lock: Optional[str] = None,
    ) -> bool:
        return self._write(
            "auto_block_unclosed_worker_turn",
            task_id=task_id,
            final_response=final_response,
            expected_run_id=expected_run_id,
            expected_claim_lock=expected_claim_lock,
        )

    def record_spawn_success(self, task_id: str, pid: int) -> None:
        return self._write("record_spawn_success", task_id=task_id, pid=int(pid))

    def record_spawn_failure(self, task_id, error, *, failure_limit=None) -> bool:
        # Thin wrapper over the A4 record_task_failure spawn_failed path. The
        # systemic-failure-signature grouping from dispatch_once lives in the
        # Part-B glue, NOT here.
        return self.record_task_failure(task_id, error, outcome="spawn_failed",
                                        failure_limit=failure_limit,
                                        release_claim=True, end_run=True)

    def block_systemic_spawn_failure_signature(self, task_ids, *,
                                               failure_signature, error,
                                               signature_count):
        conn = kb.connect(board=self.board, readonly=False)
        try:
            return kb._block_systemic_spawn_failure_signature(
                conn, list(task_ids), failure_signature=failure_signature,
                error=error, signature_count=signature_count)
        finally:
            conn.close()

    def dispatch_plan(self, *, resolve_workspace=None, profile_exists=None,
                      terminate_fn=None, signal_fn=None, pid_alive_fn=None,
                      classify_exit_fn=None, **cfg):
        """One dispatcher tick: reclaim + ready-scan + claim + workspace-resolve,
        WITHOUT spawning. Reuses the UNTOUCHED ``kanban_db.dispatch_once`` via a
        capturing ``spawn_fn`` that records each claimed task instead of spawning.

        NOTE (single-writer caveat): ``spawn_fn`` is a closure, so it CANNOT
        cross the writer-daemon socket (RemoteWriter serializes its args over
        the wire). We therefore run ``dispatch_once`` on a LOCAL writable
        connection (``kb.connect(..., _bootstrap=True)`` bypasses the
        single-writer guard) rather than routing it through ``write_session``.
        This is acceptable for A5/conformance; Part B (B3) addresses
        single-writer routing at the gateway call site, where the dispatcher
        runs in-process and can hold the writer directly.

        The injected ``resolve_workspace`` / ``profile_exists`` callbacks are
        part of the cross-backend contract; for sqlite, ``dispatch_once`` uses
        the module-level ``kb.resolve_workspace`` / ``hermes_cli.profiles
        .profile_exists`` it already imports. Tests monkeypatch those for the
        sqlite path; the injected callbacks are honored by the PG backend.
        """
        # terminate_fn / signal_fn / pid_alive_fn / classify_exit_fn: accepted
        # for KanbanStore Protocol conformance but NOT forwarded — kanban_db
        # .dispatch_once owns its own host-local kill ladder + reap internally.
        from hermes_cli.kanban.store import DispatchPlan

        captured: list = []

        def _capture_spawn(task, workspace, board=None):
            captured.append((task, str(workspace)))
            return None  # no pid yet; the glue spawns later -> record_spawn_success

        conn = kb.connect(board=self.board, readonly=False, _bootstrap=True)
        try:
            result = kb.dispatch_once(
                conn, spawn_fn=_capture_spawn, board=self.board, **cfg)
        finally:
            conn.close()
        return DispatchPlan(to_spawn=captured, result=result)

    def set_status_direct(self, task_id: str, new_status: str) -> bool:
        return self._write("set_status_direct", task_id=task_id, new_status=new_status)

    def set_task_priority(self, task_id: str, priority: int) -> bool:
        return self._write("set_task_priority", task_id=task_id, priority=priority)

    def edit_task_fields(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("edit_task_fields", task_id=task_id, **kwargs)

    def delete_task(self, task_id: str) -> bool:
        return self._write("delete_task", task_id=task_id)

    def promote_task(self, task_id: str, **kwargs: Any):
        return self._write("promote_task", task_id=task_id, **kwargs)

    def set_workspace_path(self, task_id: str, path: str) -> None:
        return self._write("set_workspace_path", task_id=task_id, path=path)

    # --- links -----------------------------------------------------------
    def link_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> None:
        return self._write("link_tasks", parent_id=parent_id, child_id=child_id, **kwargs)

    def unlink_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> bool:
        return self._write("unlink_tasks", parent_id=parent_id, child_id=child_id, **kwargs)

    def parent_ids(self, task_id: str, *, relation_type: Optional[str] = "dependency") -> list[str]:
        return self._read(lambda c: kb.parent_ids(c, task_id, relation_type=relation_type))

    def child_ids(self, task_id: str, *, relation_type: Optional[str] = "dependency") -> list[str]:
        return self._read(lambda c: kb.child_ids(c, task_id, relation_type=relation_type))

    # --- comments --------------------------------------------------------
    def add_comment(self, task_id: str, *, author: str, body: str) -> int:
        return self._write("add_comment", task_id=task_id, author=author, body=body)

    def list_comments(self, task_id: str):
        return self._read(lambda c: kb.list_comments(c, task_id))

    # --- events ----------------------------------------------------------
    def list_events(self, task_id: str, **kwargs: Any):
        return self._read(lambda c: kb.list_events(c, task_id, **kwargs))

    def gc_events(self, **kwargs: Any) -> int:
        return self._write("gc_events", **kwargs)

    # --- runs ------------------------------------------------------------
    def list_runs(self, task_id: str):
        return self._read(lambda c: kb.list_runs(c, task_id))

    def get_run(self, run_id: int):
        return self._read(lambda c: kb.get_run(c, run_id))

    def latest_run(self, task_id: str):
        return self._read(lambda c: kb.latest_run(c, task_id))

    def latest_summary(self, task_id: str):
        return self._read(lambda c: kb.latest_summary(c, task_id))

    def build_worker_context(self, task_id: str) -> str:
        # Byte-identical to the upstream worker-context builder. The sqlite
        # store reads through the same snapshot/connect lifecycle as every
        # other read here.
        return self._read(lambda c: kb.build_worker_context(c, task_id))

    def latest_summaries(self, task_ids):
        return self._read(lambda c: kb.latest_summaries(c, task_ids))

    # --- dashboard recovery ----------------------------------------------
    def edit_completed_task_result(self, task_id: str, **kwargs: Any) -> bool:
        return self._write("edit_completed_task_result", task_id=task_id, **kwargs)

    # --- notify subs + event claiming ------------------------------------
    def add_notify_sub(self, **kwargs: Any) -> int:
        return self._write("add_notify_sub", **kwargs)

    def remove_notify_sub(self, **kwargs: Any) -> bool:
        return self._write("remove_notify_sub", **kwargs)

    def list_notify_subs(self, task_id: Optional[str] = None):
        return self._read(lambda c: kb.list_notify_subs(c, task_id))

    def claim_unseen_events_for_sub(self, **kwargs: Any) -> tuple:
        # read-probe + conditional cursor-advance; needs a writable conn
        return self._write("claim_unseen_events_for_sub", **kwargs)

    def advance_notify_cursor(self, **kwargs: Any) -> None:
        return self._write("advance_notify_cursor", **kwargs)

    def rewind_notify_cursor(self, **kwargs: Any) -> bool:
        return self._write("rewind_notify_cursor", **kwargs)

    # --- profile-event subs + wake events --------------------------------
    def add_profile_event_sub(self, **kwargs: Any):
        return self._write("add_profile_event_sub", **kwargs)

    def remove_profile_event_sub(self, **kwargs: Any) -> bool:
        return self._write("remove_profile_event_sub", **kwargs)

    def list_profile_event_subs(self, **kwargs: Any):
        return self._read(lambda c: kb.list_profile_event_subs(c, **kwargs))

    def claim_unseen_events_for_profile_sub(self, **kwargs: Any) -> tuple:
        return self._write("claim_unseen_events_for_profile_sub", **kwargs)

    def advance_profile_event_cursor(self, **kwargs: Any) -> None:
        return self._write("advance_profile_event_cursor", **kwargs)

    def rewind_profile_event_cursor(self, **kwargs: Any) -> bool:
        return self._write("rewind_profile_event_cursor", **kwargs)

    def record_profile_wake_success(self, **kwargs: Any) -> int:
        return self._write("record_profile_wake_success", **kwargs)

    def record_profile_wake_failure(self, **kwargs: Any) -> int:
        return self._write("record_profile_wake_failure", **kwargs)

    def list_profile_wake_events(self, **kwargs: Any):
        return self._read(lambda c: kb.list_profile_wake_events(c, **kwargs))

    # --- notifier heartbeats ---------------------------------------------
    def record_notifier_heartbeat(self, **kwargs: Any):
        return self._write("record_notifier_heartbeat", **kwargs)

    def list_notifier_heartbeats(self, **kwargs: Any):
        return self._read(lambda c: kb.list_notifier_heartbeats(c, **kwargs))

    # --- worker heartbeat + dispatch reads -------------------------------
    def heartbeat_worker(self, **kwargs: Any) -> bool:
        return self._write("heartbeat_worker", **kwargs)

    def recompute_ready(self) -> int:
        return self._write("recompute_ready")

    def has_spawnable_ready(self) -> bool:
        return self._read(lambda c: kb.has_spawnable_ready(c))

    def board_stats(self):
        return self._read(lambda c: kb.board_stats(c))

    def known_assignees(self):
        return self._read(lambda c: kb.known_assignees(c))


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
