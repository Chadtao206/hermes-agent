"""Quiesced Kanban DB repair guard.

This module intentionally does not auto-initialize or migrate the live board DB.
It exists to make the dangerous part of a malformed ``kanban.db`` recovery
explicit, evidence-backed, and fail-closed.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb

DURABLE_TABLES = (
    "tasks",
    "task_events",
    "task_runs",
    "task_links",
    "task_comments",
    "kanban_profile_event_subs",
    "kanban_profile_wake_events",
)


def _readonly_connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _pragma_rows(conn: sqlite3.Connection, pragma: str) -> list[str]:
    return [str(row[0]) for row in conn.execute(f"PRAGMA {pragma}").fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _max_expr(columns: set[str]) -> str:
    parts: list[str] = []
    for col in ("id", "created_at", "updated_at", "started_at", "finished_at", "completed_at"):
        if col in columns:
            parts.append(f"MAX({col}) AS max_{col}")
    return ", ".join(parts)


def _durable_markers(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    markers: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    with _readonly_connect(path) as conn:
        for table in DURABLE_TABLES:
            if not _table_exists(conn, table):
                markers[table] = {"exists": False, "count": None}
                continue
            try:
                columns = _table_columns(conn, table)
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                item: dict[str, Any] = {"exists": True, "count": count}
                expr = _max_expr(columns)
                if expr:
                    row = conn.execute(f"SELECT {expr} FROM {table}").fetchone()
                    if row is not None:
                        item.update({key: row[key] for key in row.keys()})
                markers[table] = item
            except Exception as exc:  # pragma: no cover - defensive evidence path
                errors.append({"table": table, "error": f"{type(exc).__name__}: {exc}"})
                markers[table] = {"exists": True, "error": f"{type(exc).__name__}: {exc}"}
    return markers, errors


def verify_candidate(candidate: Path) -> dict[str, Any]:
    candidate = Path(candidate).expanduser()
    result: dict[str, Any] = {
        "path": str(candidate),
        "exists": candidate.exists(),
        "ok": False,
        "quick_check": [],
        "integrity_check": [],
        "durable_markers": {},
        "errors": [],
    }
    if not candidate.exists():
        result["errors"].append("candidate_missing")
        return result
    if not candidate.is_file():
        result["errors"].append("candidate_not_file")
        return result
    try:
        with _readonly_connect(candidate) as conn:
            quick = _pragma_rows(conn, "quick_check")
            integrity = _pragma_rows(conn, "integrity_check")
        markers, marker_errors = _durable_markers(candidate)
        result.update(
            {
                "quick_check": quick,
                "integrity_check": integrity,
                "durable_markers": markers,
            }
        )
        result["errors"].extend(marker_errors)
        result["ok"] = quick == ["ok"] and integrity == ["ok"] and not marker_errors
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def _open_handle_check(paths: list[Path]) -> dict[str, Any]:
    existing = [str(path) for path in paths if path.exists()]
    if not existing:
        return {"ok": True, "tool": "lsof", "open_handles": "", "checked_paths": []}
    if shutil.which("lsof") is None:
        return {
            "ok": False,
            "tool": "lsof",
            "reason": "lsof_unavailable",
            "checked_paths": existing,
            "open_handles": "",
        }
    proc = subprocess.run(
        ["lsof", *existing],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = (proc.stdout or proc.stderr or "").strip()
    # lsof exits 1 when there are no matching open files.
    if proc.returncode == 1 and not output:
        return {"ok": True, "tool": "lsof", "checked_paths": existing, "open_handles": ""}
    return {
        "ok": proc.returncode == 1 and not output,
        "tool": "lsof",
        "returncode": proc.returncode,
        "checked_paths": existing,
        "open_handles": output,
    }


def _default_evidence_dir() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return kb.kanban_home() / "forensics" / f"kanban-live-repair-{stamp}"


def _copy_if_exists(src: Path, dst_dir: Path) -> str | None:
    if not src.exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    return str(dst)


def _move_if_exists(src: Path, dst_dir: Path) -> str | None:
    if not src.exists():
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        dst = dst_dir / f"{src.name}.{int(time.time())}"
    shutil.move(str(src), str(dst))
    return str(dst)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _runbook(live_path: Path) -> list[str]:
    return [
        "Do not replace a live Kanban DB while gateway/dashboard/cron writers are active.",
        "Use a non-gateway substrate or self-contained maintenance script with disk evidence.",
        "Stop dashboard, cron/scheduler writers, then gateway; verify no PIDs or open lsof handles remain.",
        "Capture live kanban.db plus -wal/-shm sidecars into an evidence directory before mutation.",
        "Verify candidate PRAGMA quick_check and integrity_check are both ok.",
        "Compare durable-table freshness markers from live vs candidate; proceed only if candidate is equal/newer or explicit human loss acceptance is recorded.",
        f"Install candidate to {live_path} only after the quiesced/open-handle gate passes.",
        "Remove stale live -wal/-shm sidecars, then re-run quick_check/integrity_check before restarting services.",
        "Restart gateway/dashboard/cron only after installed DB verification passes; run `hermes kanban doctor --json` after restart.",
    ]


def run_repair_guard(
    *,
    board: str | None = None,
    candidate: str | Path | None = None,
    install: bool = False,
    confirm_quiesced: bool = False,
    confirm_freshness_checked: bool = False,
    evidence_dir: str | Path | None = None,
) -> dict[str, Any]:
    live_path = kb.kanban_db_path(board=board)
    sidecars = [live_path.with_name(live_path.name + suffix) for suffix in ("-wal", "-shm")]
    candidate_path = Path(candidate).expanduser() if candidate else None
    evidence = Path(evidence_dir).expanduser() if evidence_dir else _default_evidence_dir()
    result: dict[str, Any] = {
        "ok": False,
        "board": board or kb.get_current_board(),
        "db_path": str(live_path),
        "install_requested": install,
        "installed": False,
        "candidate": None,
        "evidence_dir": str(evidence),
        "issues": [],
        "runbook": _runbook(live_path),
    }

    if candidate_path is None:
        if install:
            result["issues"].append({"kind": "candidate_required", "severity": "error"})
        else:
            result["ok"] = True
            result["issues"].append({"kind": "runbook_only", "severity": "info"})
        return result

    if candidate_path.resolve() == live_path.resolve():
        result["candidate"] = {"path": str(candidate_path), "ok": False, "errors": ["candidate_is_live_db"]}
        result["issues"].append({"kind": "candidate_is_live_db", "severity": "error"})
        return result

    candidate_result = verify_candidate(candidate_path)
    result["candidate"] = candidate_result
    if not candidate_result.get("ok"):
        result["issues"].append({"kind": "candidate_verification_failed", "severity": "error"})
        return result

    if not install:
        result["ok"] = True
        result["issues"].append({"kind": "dry_run_only", "severity": "info"})
        return result

    missing = []
    if not confirm_quiesced:
        missing.append("--confirm-quiesced")
    if not confirm_freshness_checked:
        missing.append("--confirm-freshness-checked")
    if missing:
        result["issues"].append(
            {
                "kind": "missing_confirmation",
                "severity": "error",
                "missing": missing,
                "message": "Refusing live replacement without explicit quiescence and freshness gates.",
            }
        )
        return result

    if not live_path.exists():
        result["issues"].append({"kind": "live_db_missing", "severity": "error"})
        return result

    handle_check = _open_handle_check([live_path, *sidecars])
    result["open_handle_check"] = handle_check
    if not handle_check.get("ok"):
        result["issues"].append(
            {
                "kind": "open_handles_or_unverified",
                "severity": "error",
                "message": "Refusing live replacement because open-handle verification did not pass.",
            }
        )
        return result

    live_before = evidence / "live-before"
    try:
        copied = [_copy_if_exists(path, live_before) for path in [live_path, *sidecars]]
        _write_text(evidence / "preflight-candidate.txt", repr(candidate_result))
        moved = [_move_if_exists(path, live_before) for path in [live_path, *sidecars]]
        tmp = live_path.with_name(live_path.name + ".repair-tmp")
        shutil.copy2(candidate_path, tmp)
        os.replace(tmp, live_path)
        for sidecar in sidecars:
            if sidecar.exists():
                sidecar.unlink()
        installed = verify_candidate(live_path)
        _write_text(evidence / "installed-verification.txt", repr(installed))
        if not installed.get("ok"):
            # Best-effort restore. Leave evidence for operator inspection.
            backup = live_before / live_path.name
            if backup.exists():
                os.replace(str(backup), str(live_path))
            _write_text(evidence / "FAILED", "installed verification failed\n")
            result["issues"].append({"kind": "installed_verification_failed", "severity": "error"})
            result["copied_evidence"] = [p for p in copied if p]
            result["moved_evidence"] = [p for p in moved if p]
            return result
        _write_text(evidence / "SUCCESS", "kanban db replacement installed and verified\n")
        result.update(
            {
                "ok": True,
                "installed": True,
                "installed_verification": installed,
                "copied_evidence": [p for p in copied if p],
                "moved_evidence": [p for p in moved if p],
            }
        )
        return result
    except Exception as exc:  # pragma: no cover - defensive operational path
        _write_text(evidence / "FAILED", f"{type(exc).__name__}: {exc}\n")
        result["issues"].append({"kind": "repair_exception", "severity": "error", "error": f"{type(exc).__name__}: {exc}"})
        return result


def format_repair_text(result: dict[str, Any]) -> str:
    status = "ok" if result.get("ok") else "blocked"
    lines = [f"Kanban DB repair guard: {status} ({result.get('board')})", f"DB: {result.get('db_path')}"]
    if result.get("candidate"):
        candidate = result["candidate"]
        lines.append(f"Candidate: {candidate.get('path')} verified={candidate.get('ok')}")
    lines.append(f"Evidence dir: {result.get('evidence_dir')}")
    for issue in result.get("issues") or []:
        kind = issue.get("kind")
        msg = issue.get("message") or issue.get("missing") or issue.get("severity")
        lines.append(f"- {kind}: {msg}")
    if not result.get("installed"):
        lines.append("Runbook guardrails:")
        for step in result.get("runbook") or []:
            lines.append(f"  - {step}")
    else:
        lines.append("Installed replacement and wrote SUCCESS marker.")
    return "\n".join(lines)
