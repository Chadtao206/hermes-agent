"""Canonical read-only health bundle for Kanban SQLite databases.

This helper is intended for canaries and ops diagnostics that need one
consistent ordered probe shape with phase-level attribution.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

PHASE_SQLITE3_CLI_QUICK_CHECK = "sqlite3_cli_quick_check"
PHASE_PYTHON_RO_CONNECT = "python_ro_connect"
PHASE_PYTHON_RO_SELECT_1 = "python_ro_select_1"
PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK = "python_ro_pragma_quick_check"

PHASE_ORDER = (
    PHASE_SQLITE3_CLI_QUICK_CHECK,
    PHASE_PYTHON_RO_CONNECT,
    PHASE_PYTHON_RO_SELECT_1,
    PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK,
)


def _failure(
    phase: str,
    *,
    exc: BaseException | None = None,
    message: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phase": phase,
        "status": "failed",
    }
    if exc is not None:
        payload["exception_class"] = type(exc).__name__
        payload["exception_message"] = str(exc)
    if message:
        payload["message"] = message
    if extra:
        payload.update(extra)
    return payload


def _ok(phase: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"phase": phase, "status": "ok"}
    payload.update(extra)
    return payload


def _skipped(phase: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phase": phase,
        "status": "skipped",
        "reason": reason,
    }
    payload.update(extra)
    return payload


def _ro_uri(path: Path) -> str:
    # immutable=1 keeps SQLite on a strictly read-only code path and avoids
    # creating transient WAL sidecars during diagnostic probes.
    return f"file:{path.resolve()}?mode=ro&immutable=1"


def run_readonly_health_bundle(
    path: Path | str,
    *,
    sqlite3_bin: str | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Run canonical ordered read-only health phases against a Kanban DB path."""
    db_path = Path(path).expanduser()
    phases: list[dict[str, Any]] = []
    conn: sqlite3.Connection | None = None

    # Phase 1: sqlite3 CLI quick_check.
    phase = PHASE_SQLITE3_CLI_QUICK_CHECK
    bin_path = sqlite3_bin or shutil.which("sqlite3")
    if not bin_path:
        phases.append(_skipped(phase, "sqlite3_binary_unavailable"))
    else:
        try:
            proc = subprocess.run(
                [bin_path, _ro_uri(db_path), "PRAGMA quick_check;"],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            rows = [row.strip() for row in stdout.splitlines() if row.strip()]
            if proc.returncode != 0:
                phases.append(
                    _failure(
                        phase,
                        message="sqlite3 quick_check returned non-zero exit",
                        extra={
                            "returncode": proc.returncode,
                            "stdout": stdout,
                            "stderr": stderr,
                        },
                    )
                )
            elif rows != ["ok"]:
                phases.append(
                    _failure(
                        phase,
                        message="sqlite3 quick_check did not return ok",
                        extra={
                            "returncode": proc.returncode,
                            "stdout": stdout,
                            "stderr": stderr,
                            "quick_check_rows": rows,
                        },
                    )
                )
            else:
                phases.append(
                    _ok(
                        phase,
                        returncode=proc.returncode,
                        stdout=stdout,
                        stderr=stderr,
                        quick_check_rows=rows,
                    )
                )
        except Exception as exc:
            phases.append(_failure(phase, exc=exc))

    # Phase 2: python sqlite3 mode=ro connect + query_only.
    phase = PHASE_PYTHON_RO_CONNECT
    connect_failed = False
    try:
        conn = sqlite3.connect(
            _ro_uri(db_path),
            uri=True,
            isolation_level=None,
            timeout=timeout_seconds,
        )
        conn.execute("PRAGMA query_only=ON")
        phases.append(_ok(phase))
    except Exception as exc:
        phases.append(_failure(phase, exc=exc))
        connect_failed = True

    # Phase 3 + 4 require a successful Python read-only connection.
    if connect_failed or conn is None:
        phases.append(_skipped(PHASE_PYTHON_RO_SELECT_1, "python_ro_connect_failed"))
        phases.append(_skipped(PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK, "python_ro_connect_failed"))
    else:
        try:
            phase = PHASE_PYTHON_RO_SELECT_1
            row = conn.execute("SELECT 1").fetchone()
            if row is None:
                phases.append(_failure(phase, message="SELECT 1 returned no rows"))
            elif row[0] != 1:
                phases.append(_failure(phase, message=f"SELECT 1 returned {row!r}"))
            else:
                phases.append(_ok(phase, row=[row[0]]))
        except Exception as exc:
            phases.append(_failure(PHASE_PYTHON_RO_SELECT_1, exc=exc))

        try:
            phase = PHASE_PYTHON_RO_PRAGMA_QUICK_CHECK
            rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            if rows != ["ok"]:
                phases.append(
                    _failure(
                        phase,
                        message="python read-only quick_check did not return ok",
                        extra={"quick_check_rows": rows[:50]},
                    )
                )
            else:
                phases.append(_ok(phase, quick_check_rows=rows))
        except Exception as exc:
            phases.append(_failure(phase, exc=exc))

    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass

    failures = [entry for entry in phases if entry.get("status") == "failed"]
    return {
        "ok": not failures,
        "db_path": str(db_path),
        "phase_order": list(PHASE_ORDER),
        "phases": phases,
        "failure_count": len(failures),
        "checked_at": int(time.time()),
    }
