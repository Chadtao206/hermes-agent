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
    """Snapshot the live DB via SQLite's online backup API. Returns None if the
    live DB is not healthy (never snapshot a known-bad file)."""
    db_path = Path(db_path); backup_dir = Path(backup_dir)
    if not _integrity_ok(db_path):
        return None
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
    drop = snaps[:-keep] if keep > 0 else snaps
    for old in drop:
        old.unlink(missing_ok=True)


def _latest_backup(backup_dir: Path, name: str) -> Optional[Path]:
    snaps = sorted(Path(backup_dir).glob(f"{name}.online.*.bak"))
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
    """Bounded recovery ladder. Caller (daemon) must hold the sole writer role
    and must have CLOSED its connection before calling."""
    db_path = Path(db_path)
    # Rung 0: maybe a transient/WAL issue — checkpoint + re-probe.
    if _checkpoint_then_ok(db_path):
        return RecoveryResult(True, "checkpoint")
    # Quarantine the corrupt file (keep evidence) before mutating anything.
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
