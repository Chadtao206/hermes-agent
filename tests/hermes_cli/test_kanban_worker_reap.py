"""The dispatcher must see every worker's real exit status.

``_default_spawn`` used to drop its ``Popen`` handle. ``Popen.__del__`` then parks the
still-running child in ``subprocess._active`` and the next ``Popen()`` anywhere in the
process (the dispatcher runs inside the gateway) reaps it via ``subprocess._cleanup()``,
so ``reap_worker_zombies`` never sees the status and the worker is booked "pid N not
alive" (a counted crash) instead of by its exit code. Rate-limited exits (75) were the
most visible casualty: 36 Boris runs through 2026-09-06."""

import gc
import os
import subprocess
import sys
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _spawn_exit(code: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import sys; sys.exit({code})"])


def _wait_for_zombie(pid: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not kbd._pid_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not exit in time")


def test_retained_worker_exit_status_survives_subprocess_cleanup(monkeypatch):
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    proc = _spawn_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    pid = proc.pid
    kbd._retain_worker_proc(proc)
    del proc
    gc.collect()
    _wait_for_zombie(pid)

    # What any later Popen() in the process does first; it would have stolen the
    # status of an unretained child.
    subprocess._cleanup()

    assert pid in kbd.reap_worker_zombies()
    assert kbd._classify_worker_exit(pid) == ("rate_limited", kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    assert pid not in kbd._live_worker_procs


def test_reaping_releases_the_handle_without_reregistering_it(monkeypatch):
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    proc = _spawn_exit(3)
    kbd._retain_worker_proc(proc)
    _wait_for_zombie(proc.pid)

    assert proc.pid in kbd.reap_worker_zombies()

    # A released handle must never wander back into subprocess._active on __del__.
    assert proc.returncode == 3
    assert kbd._classify_worker_exit(proc.pid) == ("nonzero_exit", 3)


def test_unretained_child_status_is_stolen_by_subprocess_cleanup(monkeypatch):
    """Documents the race the retention closes; if this ever fails, CPython changed
    ``subprocess._cleanup`` and the retention can be revisited."""
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    proc = _spawn_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    pid = proc.pid
    del proc
    gc.collect()
    _wait_for_zombie(pid)

    subprocess._cleanup()

    assert pid not in kbd.reap_worker_zombies()
    assert kbd._classify_worker_exit(pid) == ("unknown", None)


def test_prune_reaps_and_records_a_finished_child_instead_of_discarding_it(monkeypatch):
    """When the registry overflows, pruning must not itself lose an exit status."""
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    monkeypatch.setattr(kbd, "_LIVE_WORKER_PROCS_MAX", 0)
    finished = _spawn_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    _wait_for_zombie(finished.pid)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        kbd._retain_worker_proc(finished)  # overflow -> prune runs
        kbd._retain_worker_proc(live)

        assert kbd._classify_worker_exit(finished.pid) == (
            "rate_limited", kb.KANBAN_RATE_LIMIT_EXIT_CODE,
        )
        assert finished.pid not in kbd._live_worker_procs
        assert live.pid in kbd._live_worker_procs  # still running: kept
    finally:
        live.kill()
        live.wait()
