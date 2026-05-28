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
import sys
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
FRESHNESS_FIELDS = (
    "count",
    "max_id",
    "max_updated_at",
    "max_completed_at",
    "max_finished_at",
    "max_created_at",
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
    try:
        conn_ctx = kb.snapshot_connect(db_path=path)
        with conn_ctx as conn:
            for table in DURABLE_TABLES:
                try:
                    if not _table_exists(conn, table):
                        markers[table] = {"exists": False, "count": None}
                        continue
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
    except Exception as exc:
        errors.append({"table": "__database__", "error": f"{type(exc).__name__}: {exc}"})
    return markers, errors


def _marker_value(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return text


def _value_regressed(*, candidate: Any, live: Any) -> bool:
    live_v = _marker_value(live)
    if live_v is None:
        return False
    candidate_v = _marker_value(candidate)
    if candidate_v is None:
        return True
    if isinstance(live_v, int) and isinstance(candidate_v, int):
        return candidate_v < live_v
    return str(candidate_v) < str(live_v)


def compare_freshness(
    live_markers: dict[str, Any],
    candidate_markers: dict[str, Any],
) -> dict[str, Any]:
    regressions: list[dict[str, Any]] = []
    table_deltas: list[dict[str, Any]] = []

    for table in DURABLE_TABLES:
        live = live_markers.get(table) or {}
        candidate = candidate_markers.get(table) or {}
        table_deltas.append({"table": table, "live": live, "candidate": candidate})

        live_exists = bool(live.get("exists"))
        live_count = _marker_value(live.get("count"))
        if not live_exists or not isinstance(live_count, int) or live_count <= 0:
            continue

        if not candidate.get("exists"):
            regressions.append(
                {
                    "table": table,
                    "field": "exists",
                    "kind": "missing_table",
                    "live": True,
                    "candidate": False,
                }
            )
            continue

        for field in FRESHNESS_FIELDS:
            live_value = live.get(field)
            if _marker_value(live_value) is None:
                continue
            candidate_value = candidate.get(field)
            if _value_regressed(candidate=candidate_value, live=live_value):
                regressions.append(
                    {
                        "table": table,
                        "field": field,
                        "kind": "staler_count" if field == "count" else f"staler_{field}",
                        "live": live_value,
                        "candidate": candidate_value,
                    }
                )

    return {"ok": not regressions, "regressions": regressions, "table_deltas": table_deltas}


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


def _session_context() -> dict[str, Any]:
    """Return the current invocation context relevant to live DB repair.

    Gateway sessions set task-local contextvars, while subprocesses spawned
    from those sessions may only inherit legacy environment variables. Check
    both so a Slack/gateway-originated install cannot bypass the guard merely
    by shelling out to ``hermes kanban repair-db``.
    """
    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or ""
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        platform = os.getenv("HERMES_SESSION_PLATFORM", "") or ""
        chat_id = os.getenv("HERMES_SESSION_CHAT_ID", "") or ""

    source = os.getenv("HERMES_SESSION_SOURCE", "") or ""
    gateway_markers = {
        name: os.getenv(name, "")
        for name in ("_HERMES_GATEWAY", "HERMES_GATEWAY_SESSION")
        if os.getenv(name, "")
    }
    platform_l = platform.lower().strip()
    source_l = source.lower().strip()
    gateway_like_source = bool(platform_l and platform_l not in {"cli", "cron", "local"})
    if source_l and source_l not in {"cli", "cron", "local", "tool"}:
        gateway_like_source = True
    active = bool(gateway_markers or gateway_like_source)
    return {
        "active_gateway_context": active,
        "platform": platform,
        "chat_id": chat_id,
        "source": source,
        "gateway_markers": gateway_markers,
    }


def _cmdline_for_pid(pid: int) -> str:
    try:
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            if raw:
                return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _scan_processes_with_ps() -> tuple[list[dict[str, Any]], str | None]:
    try:
        proc = subprocess.run(
            ["ps", "axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return [], f"ps_unavailable: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return [], f"ps_failed: {(proc.stderr or proc.stdout or '').strip()}"
    rows: list[dict[str, Any]] = []
    current_pid = os.getpid()
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_s, _, command = stripped.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        command_l = command.lower()
        # Ignore shell/status wrappers that merely contain writer command text in
        # their argv (for example an operator one-liner that ran
        # ``hermes dashboard --status`` before invoking repair-db).  The actual
        # gateway/dashboard/cron writer, if alive, appears as its own process and
        # will still be caught below.
        if (
            command_l.startswith(("/bin/bash -c", "bash -c", "/bin/sh -c", "sh -c", "zsh -c", "/bin/zsh -c"))
            or "hermes dashboard --status" in command_l
            or "hermes dashboard --stop" in command_l
        ):
            continue
        kind = None
        if "hermes" in command_l and "dashboard" in command_l:
            kind = "dashboard"
        elif "dashboard/server.py" in command_l or "tui_gateway/server.py" in command_l:
            kind = "dashboard"
        elif "cron.scheduler" in command_l or "cron/scheduler.py" in command_l:
            kind = "cron"
        elif "gateway/run.py" in command_l or ("hermes" in command_l and "gateway" in command_l):
            kind = "gateway"
        if kind:
            rows.append({"pid": pid, "kind": kind, "command": command})
    return rows, None


def _writer_process_check() -> dict[str, Any]:
    """Fail-closed writer-process gate beyond open-file checks.

    SQLite open handles are necessary but not sufficient: a live gateway,
    dashboard, or cron scheduler can reopen ``kanban.db`` immediately after
    an ``lsof`` sample. This gate blocks install while known Hermes writer
    services are running or while process verification is unavailable.
    """
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    current_pid = os.getpid()

    try:
        from gateway.status import get_running_pid

        gateway_pid = get_running_pid(cleanup_stale=False)
        if gateway_pid and int(gateway_pid) != current_pid:
            rows.append({
                "pid": int(gateway_pid),
                "kind": "gateway",
                "source": "gateway.status",
                "command": _cmdline_for_pid(int(gateway_pid)),
            })
    except Exception as exc:
        errors.append(f"gateway_status_unavailable: {type(exc).__name__}: {exc}")

    ps_rows, ps_error = _scan_processes_with_ps()
    if ps_error:
        errors.append(ps_error)
    rows.extend(ps_rows)

    seen: set[tuple[int, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (int(row.get("pid") or 0), str(row.get("kind") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    blocking_errors = [err for err in errors if err.startswith("ps_")]
    return {
        "ok": not unique and not blocking_errors,
        "running_writers": unique,
        "errors": errors,
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
        "Use a non-gateway substrate or self-contained maintenance script with disk evidence; live replacement from Slack/gateway context is refused.",
        "Stop dashboard, cron/scheduler writers, then gateway; verify no matching PIDs and no open lsof handles remain.",
        "Capture live kanban.db plus -wal/-shm sidecars into an evidence directory before mutation.",
        "Verify candidate PRAGMA quick_check and integrity_check are both ok.",
        "Compare durable-table freshness markers from live vs candidate; proceed only if candidate is equal/newer or explicit --allow-data-loss human loss acceptance is recorded.",
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
    allow_data_loss: bool = False,
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
        "allow_data_loss": bool(allow_data_loss),
        "installed": False,
        "candidate": None,
        "evidence_dir": str(evidence),
        "session_context": _session_context(),
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

    live_markers: dict[str, Any] = {}
    if live_path.exists():
        live_markers, live_marker_errors = _durable_markers(live_path)
        result["live_durable_markers"] = live_markers
        if live_marker_errors:
            issue = {
                "kind": "live_marker_read_failed",
                "severity": "warning" if allow_data_loss else "error",
                "message": (
                    "Live durable markers could not be read. This is treated as a freshness/loss risk; "
                    "install requires explicit --allow-data-loss plus normal quiescence confirmations."
                ),
                "errors": live_marker_errors,
            }
            result["issues"].append(issue)
            result["freshness_comparison"] = {
                "ok": False,
                "regressions": [],
                "table_deltas": [],
                "unknown": True,
                "live_marker_errors": live_marker_errors,
            }
            if not allow_data_loss:
                return result
        else:
            freshness = compare_freshness(live_markers, candidate_result.get("durable_markers") or {})
            result["freshness_comparison"] = freshness
            if not freshness.get("ok") and not allow_data_loss:
                result["issues"].append(
                    {
                        "kind": "freshness_regression",
                        "severity": "error",
                        "message": "Candidate durable markers regress versus live DB; refusing install without explicit --allow-data-loss.",
                        "regressions": freshness.get("regressions") or [],
                    }
                )
                return result

    if not install:
        result["ok"] = True
        result["issues"].append({"kind": "dry_run_only", "severity": "info"})
        return result

    if result["session_context"].get("active_gateway_context"):
        result["issues"].append(
            {
                "kind": "active_gateway_context_refused",
                "severity": "error",
                "message": "Refusing live DB replacement from a gateway/Slack-style session; launch an out-of-band maintenance shell/script after stopping services.",
                "context": result["session_context"],
            }
        )
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

    freshness = result.get("freshness_comparison") or {}
    if allow_data_loss and not freshness.get("ok", True):
        result["issues"].append(
            {
                "kind": "allow_data_loss_override",
                "severity": "warning",
                "message": "Proceeding with --allow-data-loss despite freshness regressions.",
                "regressions": freshness.get("regressions") or [],
            }
        )

    if not live_path.exists():
        result["issues"].append({"kind": "live_db_missing", "severity": "error"})
        return result

    writer_check = _writer_process_check()
    result["writer_process_check"] = writer_check
    if not writer_check.get("ok"):
        result["issues"].append(
            {
                "kind": "writer_processes_or_unverified",
                "severity": "error",
                "message": "Refusing live replacement because gateway/dashboard/cron writer-process verification did not pass.",
            }
        )
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
