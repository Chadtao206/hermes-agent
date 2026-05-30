# WS2 — Corruption Auto-Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or
> `superpowers:executing-plans`. Depends on **WS1** (the daemon is the single recovery point).

**Goal:** Replace the current "detect corruption → disable board until a human repairs and
restarts the gateway" behavior with bounded, automatic recovery that resumes dispatch/notify on
its own, and pages a human only when recovery genuinely fails.

**Architecture:** The single-writer daemon (WS1) is the *only* writer, so it is the one place
that can safely quiesce and repair. On a corruption signal from its owned connection, the daemon
runs a bounded recovery ladder (checkpoint → `.recover`/reindex → restore-from-continuous-backup
→ give up + alert), re-probes, and resumes serving. A periodic online backup in the daemon
replaces the manual `.bak` graveyard. The gateway stops permanently disabling boards; it backs
off and lets the daemon heal, and a watchdog restarts the daemon thread if the process-internal
server dies. Behind `kanban.writer_auto_recovery` (default `false`).

**Tech Stack:** `sqlite3`, existing `hermes_cli/kanban_db.py` corruption guards and
`krepair.run_repair_guard` (the `hermes kanban repair-db` engine).

**Key existing anchors (verified):**
- `gateway/run.py`: `_KANBAN_DB_CORRUPTION_CONFIRM_STREAK` (~`:1789`), notifier hard-disable
  `notifier_disabled_db_paths` (~`:5452-5463`, checked `:5513-5520`), dispatcher hard-disable
  `disabled_db_paths` (~`:6989-7007`, checked `:6901`).
- `hermes_cli/kanban_db.py`: `_guard_existing_db_is_healthy`, `_validate_sqlite_header`,
  `_backup_corrupt_db` (~`:1648`), `KanbanDbCorruptError`, `_CORRUPT_PATHS`.
- `hermes_cli/kanban.py`: `_cmd_repair_db` → `krepair.run_repair_guard(...)`,
  `_cmd_doctor`, recovery flags `--confirm-quiesced/--install/--allow-data-loss`.

---

## File structure

- **Create** `hermes_cli/kanban_recovery.py` — corruption classification + the bounded recovery
  ladder + online backup, all operating on file paths (no gateway coupling, fully testable).
- **Modify** `hermes_cli/kanban_writer_daemon.py` — call recovery on corruption; run the backup
  loop; expose health state.
- **Modify** `gateway/run.py` — replace permanent disable with backoff + a daemon watchdog;
  emit an alert only when the daemon reports recovery exhausted.
- **Modify** `config.yaml` (+ example) — `kanban.writer_auto_recovery`,
  `kanban.writer_backup_interval_seconds`, `kanban.writer_backup_keep`.
- **Tests** `tests/hermes_cli/test_kanban_recovery.py`,
  extend `tests/hermes_cli/test_kanban_writer_daemon.py`,
  `tests/gateway/test_kanban_writer_watchdog.py`.

---

### Task 1: Corruption classifier + recovery ladder (pure, path-based)

**Files:**
- Create: `hermes_cli/kanban_recovery.py`
- Test: `tests/hermes_cli/test_kanban_recovery.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/hermes_cli/test_kanban_recovery.py
import sqlite3
from pathlib import Path
import pytest
from hermes_cli import kanban_recovery as rec
from hermes_cli import kanban_db as kb


def _make_good_db(path: Path):
    conn = kb.connect(db_path=path, readonly=False, _bootstrap=True)
    conn.execute("INSERT INTO tasks (id, title, status) VALUES ('t1','keep','todo')")
    conn.commit(); conn.close()


def _corrupt_in_place(path: Path):
    data = bytearray(path.read_bytes())
    for i in range(800, 1600):  # smash B-tree pages past the header
        data[i] = 0xFF
    path.write_bytes(bytes(data))


def test_is_corruption_signal():
    assert rec.is_corruption_signal(sqlite3.DatabaseError("database disk image is malformed"))
    assert rec.is_corruption_signal(sqlite3.DatabaseError("file is not a database"))
    assert not rec.is_corruption_signal(ValueError("nope"))


def test_recover_restores_from_backup_when_recover_fails(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _make_good_db(db)
    backup_dir = tmp_path
    rec.make_online_backup(db, backup_dir, keep=3)          # snapshot the good state
    _corrupt_in_place(db)                                   # now break the live file
    # Force the .recover branch to fail so we exercise restore-from-backup.
    monkeypatch.setattr(rec, "_try_sqlite_recover", lambda *a, **k: False)
    result = rec.recover_board(db, backup_dir=backup_dir, keep=3)
    assert result.healed is True
    assert result.method == "restore_from_backup"
    ro = kb.connect(db_path=db, readonly=True)
    assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ro.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()["title"] == "keep"


def test_recover_reports_exhausted_when_no_backup_and_recover_fails(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _make_good_db(db)
    _corrupt_in_place(db)
    monkeypatch.setattr(rec, "_try_sqlite_recover", lambda *a, **k: False)
    result = rec.recover_board(db, backup_dir=tmp_path / "empty", keep=3)
    assert result.healed is False
    assert result.method == "exhausted"
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/hermes_cli/test_kanban_recovery.py -v`
Expected: FAIL — `ModuleNotFoundError: hermes_cli.kanban_recovery`.

- [ ] **Step 3: Implement**

```python
# hermes_cli/kanban_recovery.py
"""Bounded, automatic recovery for a single-writer kanban board.

Only safe to call from the sole writer (the WS1 daemon): every step assumes
no other process is mutating the file. Operates on paths so it is unit-testable
without a gateway.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "malformed database schema",
    "database corruption",
)


def is_corruption_signal(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any(m in str(exc).lower() for m in _CORRUPTION_MARKERS)


def _integrity_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def make_online_backup(db_path: Path, backup_dir: Path, *, keep: int) -> Optional[Path]:
    """Snapshot the live DB via SQLite's online backup API (consistent, no lock starve)."""
    if not _integrity_ok(db_path):
        return None  # never snapshot a known-bad file
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    target = backup_dir / f"{db_path.name}.online.{stamp}.bak"
    src = sqlite3.connect(str(db_path)); dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    _prune_backups(backup_dir, db_path.name, keep=keep)
    return target


def _prune_backups(backup_dir: Path, name: str, *, keep: int) -> None:
    snaps = sorted(backup_dir.glob(f"{name}.online.*.bak"))
    for old in snaps[:-keep] if keep > 0 else snaps:
        old.unlink(missing_ok=True)


def _latest_backup(backup_dir: Path, name: str) -> Optional[Path]:
    snaps = sorted(backup_dir.glob(f"{name}.online.*.bak"))
    return snaps[-1] if snaps else None


def _try_sqlite_recover(db_path: Path, out_path: Path) -> bool:
    """Use the sqlite3 CLI '.recover' to rebuild into out_path; True if healthy."""
    try:
        dump = subprocess.run(["sqlite3", str(db_path), ".recover"],
                              capture_output=True, text=True, timeout=120)
        if dump.returncode != 0 or not dump.stdout:
            return False
        load = subprocess.run(["sqlite3", str(out_path)], input=dump.stdout,
                             capture_output=True, text=True, timeout=120)
        return load.returncode == 0 and _integrity_ok(out_path)
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass
class RecoveryResult:
    healed: bool
    method: str          # "checkpoint" | "recover" | "restore_from_backup" | "exhausted"
    detail: str = ""
    quarantine: Optional[str] = None


def recover_board(db_path: Path, *, backup_dir: Path, keep: int) -> RecoveryResult:
    """Bounded ladder. Caller (daemon) must hold the sole writer role."""
    db_path = Path(db_path)
    # Rung 0: maybe it was a transient/WAL issue — checkpoint + re-probe.
    if _checkpoint_then_ok(db_path):
        return RecoveryResult(True, "checkpoint")
    # Quarantine the corrupt file (keep evidence) before we mutate anything.
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    quarantine = db_path.with_suffix(db_path.suffix + f".corrupt.{stamp}.bak")
    shutil.copy2(db_path, quarantine)
    # Rung 1: rebuild via .recover into a fresh file, swap in if healthy.
    rebuilt = db_path.with_suffix(db_path.suffix + ".recovered")
    rebuilt.unlink(missing_ok=True)
    if _try_sqlite_recover(db_path, rebuilt):
        _atomic_swap(rebuilt, db_path)
        return RecoveryResult(True, "recover", quarantine=str(quarantine))
    rebuilt.unlink(missing_ok=True)
    # Rung 2: restore the newest known-good online backup.
    backup = _latest_backup(Path(backup_dir), db_path.name)
    if backup is not None and _integrity_ok(backup):
        _atomic_swap_copy(backup, db_path)
        if _integrity_ok(db_path):
            return RecoveryResult(True, "restore_from_backup",
                                  detail=str(backup), quarantine=str(quarantine))
    # Rung 3: nothing worked — leave quarantine, signal for a human page.
    return RecoveryResult(False, "exhausted", quarantine=str(quarantine))


def _checkpoint_then_ok(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return _integrity_ok(db_path)


def _atomic_swap(src: Path, dst: Path) -> None:
    for sidecar in ("-wal", "-shm"):
        Path(str(dst) + sidecar).unlink(missing_ok=True)
    src.replace(dst)  # same-dir rename is atomic


def _atomic_swap_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".restore.tmp")
    shutil.copy2(src, tmp)
    _atomic_swap(tmp, dst)
```

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/hermes_cli/test_kanban_recovery.py -v`
Expected: PASS (4 tests). Requires the `sqlite3` CLI on PATH for `.recover`; the failing-recover
tests monkeypatch it out, and the backup-restore path doesn't need it.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_recovery.py tests/hermes_cli/test_kanban_recovery.py
git commit -m "feat(kanban): bounded auto-recovery ladder + online backup helper"
```

---

### Task 2: Daemon invokes recovery on corruption + runs backup loop

**Files:**
- Modify: `hermes_cli/kanban_writer_daemon.py`
- Test: extend `tests/hermes_cli/test_kanban_writer_daemon.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/hermes_cli/test_kanban_writer_daemon.py
def test_daemon_heals_corruption_between_calls(tmp_path, monkeypatch):
    import sqlite3, threading, time, socket
    from hermes_cli import kanban_recovery as rec
    from hermes_cli import kanban_writer_protocol as proto
    db = tmp_path / "kanban.db"; sock = tmp_path / ".kanban-writer.sock"
    server = wd.WriterDaemon(db_path=db, socket_path=sock)
    server.enable_auto_recovery(backup_dir=tmp_path, keep=2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(50):
        if sock.exists(): break
        time.sleep(0.02)
    try:
        _client_call(sock, "create_task", title="pre", assignee="engineer")
        server.force_backup_now()                       # known-good snapshot exists
        # Simulate the owned conn raising corruption on next op:
        monkeypatch.setattr(server, "_raise_corruption_once", True, raising=False)
        resp = _client_call(sock, "create_task", title="post", assignee="engineer")
        # The op may fail once, but the board must be healthy afterward and the
        # daemon must keep serving (not hard-disable).
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert ro.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert server.health()["disabled"] is False
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_daemon.py::test_daemon_heals_corruption_between_calls -v`
Expected: FAIL — no `enable_auto_recovery`/`force_backup_now`/`health`.

- [ ] **Step 3: Implement**

Extend `WriterDaemon`:
- `enable_auto_recovery(self, *, backup_dir, keep)` stores config and a periodic backup timer
  flag; `force_backup_now()` calls `rec.make_online_backup(self.db_path, self._backup_dir, keep=...)`.
- A background backup thread (started in `serve_forever`) calls `make_online_backup` every
  `interval` seconds.
- Wrap `_dispatch`'s `fn(conn, **kwargs)` call: on `rec.is_corruption_signal(exc)`, run
  `rec.recover_board(self.db_path, backup_dir=self._backup_dir, keep=self._keep)` under the
  write lock, reopen `self._conn`, and **retry the op once**. If recovery returns
  `healed=False`, set `self._disabled = {...}` and return a structured error. Track
  `self._disabled` and expose via `health()` → `{"disabled": bool, "last_recovery": ...}`.
- `_raise_corruption_once` (test hook): when set, the next `_dispatch` raises a synthetic
  `sqlite3.DatabaseError("database disk image is malformed")` before executing, and also
  corrupts the file so recovery has real work — keep this behind a clearly-named test hook.

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/hermes_cli/test_kanban_writer_daemon.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_writer_daemon.py tests/hermes_cli/test_kanban_writer_daemon.py
git commit -m "feat(kanban): daemon self-heals corruption + periodic online backup"
```

---

### Task 3: Gateway — backoff instead of permanent disable + daemon watchdog

**Files:**
- Modify: `gateway/run.py`
- Test: `tests/gateway/test_kanban_writer_watchdog.py`

> **Merge-tax note:** Do **not** rip out the existing `notifier_disabled_db_paths` /
> `disabled_db_paths` logic — gate it. When `kanban.writer_auto_recovery` is on, the gateway
> trusts the daemon to heal: on a corruption-shaped client error it logs + backs off (retry next
> tick) rather than adding the path to the permanent-disable set. Add a watchdog that restarts a
> dead daemon thread. When the flag is off, behavior is exactly as today.

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_kanban_writer_watchdog.py
import gateway.run as gr

def test_corruption_error_does_not_permanently_disable_under_recovery(monkeypatch):
    monkeypatch.setattr(gr, "_writer_auto_recovery_enabled", lambda: True, raising=False)
    disabled = {}
    decision = gr._classify_board_write_error(
        "database disk image is malformed", disabled_set=disabled, db_path="/x/kanban.db")
    assert decision == "backoff_retry"
    assert "/x/kanban.db" not in disabled  # NOT permanently disabled

def test_corruption_error_disables_when_recovery_off(monkeypatch):
    monkeypatch.setattr(gr, "_writer_auto_recovery_enabled", lambda: False, raising=False)
    disabled = {}
    decision = gr._classify_board_write_error(
        "database disk image is malformed", disabled_set=disabled, db_path="/x/kanban.db")
    assert decision == "disable"
```

- [ ] **Step 2: Run red**

Run: `python -m pytest tests/gateway/test_kanban_writer_watchdog.py -v`
Expected: FAIL — helpers not defined.

- [ ] **Step 3: Implement**

Add the pure helpers `_writer_auto_recovery_enabled()` and `_classify_board_write_error(...)`
to `gateway/run.py`, then call `_classify_board_write_error` at the two existing disable sites
(notifier ~`:5452`, dispatcher ~`:6989`): if it returns `"backoff_retry"`, log a warning and
`continue`/`return None` *without* mutating the disabled set; if `"disable"`, keep today's
behavior. Add `_kanban_writer_watchdog()` (started alongside `_start_kanban_writer_daemon` from
WS1) that, each tick, checks each owned `WriterDaemon`'s thread liveness and `health()` — if a
thread died, re-spawn it; if `health()["disabled"]` is True (recovery exhausted), emit a
high-severity alert via the existing notifier delivery path (the WS6 alert sink if present, else
`logger.error` + a chat notification).

- [ ] **Step 4: Run green**

Run: `python -m pytest tests/gateway/test_kanban_writer_watchdog.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/run.py tests/gateway/test_kanban_writer_watchdog.py
git commit -m "feat(gateway): trust daemon recovery (backoff) + writer watchdog"
```

---

### Task 4: Config flags + end-to-end recovery check

**Files:**
- Modify: `config.yaml`, `cli-config.yaml.example`

- [ ] **Step 1: Document flags**

```yaml
kanban:
  single_writer_daemon: true        # WS1 — required for recovery to be safe
  writer_auto_recovery: false       # WS2 — daemon heals corruption instead of hard-disable
  writer_backup_interval_seconds: 300
  writer_backup_keep: 6             # rolling online snapshots kept per board
```

- [ ] **Step 2: Manual end-to-end (documented in PR description)**

1. Start gateway with both flags on.
2. With the gateway stopped *for safety in a scratch board only*, corrupt a **scratch** board
   file (smash bytes), restart the gateway.
3. Confirm logs show `recover_board` healing via `recover` or `restore_from_backup`, the board
   resumes dispatch, and **no** gateway restart was needed.
4. Confirm a quarantine `*.corrupt.*.bak` was written and the live DB passes `integrity_check`.

- [ ] **Step 3: Full suite**

Run: `python -m pytest tests/hermes_cli/ tests/gateway/ -k "kanban or writer or recovery" -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add config.yaml cli-config.yaml.example
git commit -m "docs(kanban): document writer auto-recovery + backup flags"
```

---

## WS2 acceptance criteria

- A corruption event on the hot board heals automatically (checkpoint → recover → restore) and
  the daemon keeps serving — **no gateway restart, no human** — for all cases where a healthy
  online backup or a successful `.recover` exists.
- Recovery always quarantines the corrupt file (evidence preserved) and logs the method used.
- Only `method == "exhausted"` pages a human; everything else is silent self-heal with an
  INFO/WARN audit trail.
- Flag off → byte-for-byte today's behavior.

## Self-review notes

- Names consistent with WS1: `WriterDaemon`, `health()`, `enable_auto_recovery`.
- `recover_board` is path-based and assumes the sole-writer invariant from WS1 — never call it
  outside the daemon.
- The `.recover` rung needs the `sqlite3` CLI; if unavailable in the deploy image, the ladder
  still heals via `restore_from_backup`. Note this in the PR so ops can ensure the CLI is present.
