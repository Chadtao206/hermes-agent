from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Any, Optional
try:
    from typing import Protocol, runtime_checkable
except ImportError:  # Python 3.7
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from hermes_cli.kanban_db import Task, DispatchResult

_VALID_BACKENDS = {"sqlite", "postgres"}


@dataclass
class DispatchPlan:
    """Output of one ``dispatch_plan()`` pass: tasks CLAIMED and
    workspace-resolved but NOT yet spawned (the glue spawns them), plus the
    :class:`DispatchResult` diagnostics for telemetry/logging.

    ``to_spawn`` is a list of ``(claimed_task, workspace_path)`` tuples where
    ``claimed_task`` is a full :class:`Task` (status already flipped to
    ``running`` by the claim) and ``workspace_path`` is the resolved string
    path the worker should ``cd`` into. After spawning each, the glue calls
    :meth:`KanbanStore.record_spawn_success` to stamp the worker pid, or
    :meth:`KanbanStore.record_spawn_failure` on spawn error.
    """

    to_spawn: list = field(default_factory=list)  # list[tuple[Task, str]]
    result: "DispatchResult" = field(default_factory=DispatchResult)


@runtime_checkable
class KanbanStore(Protocol):
    """Backend-agnostic interface for the kanban board store.

    Implementations must provide every method declared here; callers depend
    only on this surface so backends (SQLite today, Postgres later) can be
    swapped without touching call sites.
    """

    board: Optional[str]

    # ------------------------------------------------------------------
    # Task CRUD
    # ------------------------------------------------------------------

    def create_task(self, **kwargs: Any) -> str: ...

    def get_task(self, task_id: str) -> Optional[Task]: ...

    def list_tasks(self, **kwargs: Any) -> list[Task]: ...

    def complete_task(
        self,
        task_id: str,
        *,
        result=None,
        summary=None,
        metadata=None,
        created_cards=None,
        expected_run_id: Optional[int] = None,
        on_cleanup=None,
    ) -> bool: ...

    def block_task(
        self, task_id: str, *, reason=None, expected_run_id=None
    ) -> bool: ...

    def unblock_task(self, task_id: str) -> bool: ...

    def schedule_task(self, task_id: str, *, reason=None) -> bool: ...

    def archive_task(self, task_id: str) -> bool: ...

    def assign_task(self, task_id: str, profile: Optional[str]) -> bool: ...

    def reassign_task(
        self,
        task_id: str,
        profile: Optional[str],
        *,
        reclaim_first: bool = False,
        reason=None,
    ) -> bool: ...

    def claim_task(self, task_id: str, *, ttl_seconds: Optional[int] = None,
                   claimer: Optional[str] = None) -> Optional[Task]: ...

    def reclaim_task(self, task_id: str, *, reason=None) -> bool: ...

    def record_task_failure(
        self,
        task_id: str,
        error: str,
        *,
        outcome: str,
        failure_limit=None,
        failure_limit_is_cap: bool = False,
        release_claim: bool = True,
        end_run: bool = True,
        event_payload_extra=None,
    ) -> bool: ...

    # ------------------------------------------------------------------
    # Dispatch core (one dispatcher tick: reclaim + ready-scan + claim)
    # ------------------------------------------------------------------

    def dispatch_plan(
        self,
        *,
        max_spawn: Optional[int] = None,
        max_in_progress: Optional[int] = None,
        failure_limit: int = 2,
        stale_timeout_seconds: int = 0,
        default_assignee: Optional[str] = None,
        max_in_progress_per_profile: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
        resolve_workspace=None,
        profile_exists=None,
        terminate_fn=None,
        signal_fn=None,
        pid_alive_fn=None,
        classify_exit_fn=None,
    ) -> "DispatchPlan": ...

    def record_spawn_success(self, task_id: str, pid: int) -> None: ...

    def record_spawn_failure(
        self, task_id: str, error: str, *, failure_limit=None
    ) -> bool: ...

    def block_systemic_spawn_failure_signature(
        self, task_ids: list[str], *, failure_signature: str, error: str,
        signature_count: int,
    ) -> list[str]: ...

    def set_status_direct(self, task_id: str, new_status: str) -> bool: ...

    def set_task_priority(self, task_id: str, priority: int) -> bool: ...

    def edit_task_fields(self, task_id: str, *, title=None, body=None) -> bool: ...

    def edit_completed_task_result(self, task_id: str, **kwargs: Any) -> bool: ...

    def delete_task(self, task_id: str) -> bool: ...

    def promote_task(self, task_id: str, **kwargs: Any) -> tuple[bool, Optional[str]]: ...

    def set_workspace_path(self, task_id: str, path: str) -> None: ...

    # ------------------------------------------------------------------
    # Task links (parent/child DAG)
    # ------------------------------------------------------------------

    def link_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> None: ...

    def unlink_tasks(self, parent_id: str, child_id: str, **kwargs: Any) -> bool: ...

    def parent_ids(self, task_id: str) -> list[str]: ...

    def child_ids(self, task_id: str) -> list[str]: ...

    # ------------------------------------------------------------------
    # Comments & events
    # ------------------------------------------------------------------

    def add_comment(self, task_id: str, *, author: str, body: str) -> int: ...

    def list_comments(self, task_id: str) -> list[Any]: ...

    def list_events(self, task_id: str, **kwargs: Any) -> list[Any]: ...

    def gc_events(self, **kwargs: Any) -> int: ...

    # ------------------------------------------------------------------
    # Runs / summaries
    # ------------------------------------------------------------------

    def list_runs(self, task_id: str) -> list[Any]: ...

    def get_run(self, run_id: int) -> Optional[Any]: ...

    def latest_run(self, task_id: str) -> Optional[Any]: ...

    def latest_summary(self, task_id: str) -> Optional[str]: ...

    def latest_summaries(self, task_ids: Any) -> dict: ...

    # ------------------------------------------------------------------
    # Notify subscriptions
    # ------------------------------------------------------------------

    def add_notify_sub(self, **kwargs: Any) -> int: ...

    def remove_notify_sub(self, **kwargs: Any) -> bool: ...

    def list_notify_subs(self, task_id: Optional[str] = None) -> list[Any]: ...

    def claim_unseen_events_for_sub(self, **kwargs: Any) -> tuple: ...

    def advance_notify_cursor(self, **kwargs: Any) -> None: ...

    def rewind_notify_cursor(self, **kwargs: Any) -> bool: ...

    # ------------------------------------------------------------------
    # Profile event subscriptions
    # ------------------------------------------------------------------

    def add_profile_event_sub(self, **kwargs: Any) -> Any: ...

    def remove_profile_event_sub(self, **kwargs: Any) -> bool: ...

    def list_profile_event_subs(self, **kwargs: Any) -> list[Any]: ...

    def claim_unseen_events_for_profile_sub(self, **kwargs: Any) -> tuple: ...

    def advance_profile_event_cursor(self, **kwargs: Any) -> None: ...

    def rewind_profile_event_cursor(self, **kwargs: Any) -> bool: ...

    def record_profile_wake_success(self, **kwargs: Any) -> int: ...

    def record_profile_wake_failure(self, **kwargs: Any) -> int: ...

    def list_profile_wake_events(self, **kwargs: Any) -> list[Any]: ...

    # ------------------------------------------------------------------
    # Notifier heartbeats
    # ------------------------------------------------------------------

    def record_notifier_heartbeat(self, **kwargs: Any) -> Any: ...

    def list_notifier_heartbeats(self, **kwargs: Any) -> list[Any]: ...

    def heartbeat_worker(self, **kwargs: Any) -> bool: ...

    # ------------------------------------------------------------------
    # Readiness & stats
    # ------------------------------------------------------------------

    def recompute_ready(self) -> int: ...

    def has_spawnable_ready(self) -> bool: ...

    def board_stats(self) -> dict: ...

    def known_assignees(self) -> list[dict]: ...

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None: ...


def kanban_store(board: Optional[str] = None) -> "KanbanStore":
    """Return the configured KanbanStore for a board."""
    backend = resolve_backend()
    if backend == "sqlite":
        from .store_sqlite import SqliteKanbanStore
        return SqliteKanbanStore(board=board)
    if backend == "postgres":
        from .store_postgres import PostgresKanbanStore
        return PostgresKanbanStore(board=board)
    raise NotImplementedError(f"kanban backend '{backend}' not available yet")


def resolve_backend() -> str:
    """Return the configured kanban backend ('sqlite' default).

    Precedence: the ``HERMES_KANBAN_BACKEND`` env override (if it names a valid
    backend) wins, so the gateway can propagate the live backend to spawned
    workers whose profile-scoped config does not carry it. Otherwise read
    config defensively; any failure falls back to 'sqlite' so default
    deployments and upstream are unaffected."""
    env_backend = (os.environ.get("HERMES_KANBAN_BACKEND") or "").strip().lower()
    if env_backend in _VALID_BACKENDS:
        return env_backend
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
        backend = str(kanban_cfg.get("backend") or "sqlite").strip().lower()
    except Exception:
        return "sqlite"
    return backend if backend in _VALID_BACKENDS else "sqlite"
