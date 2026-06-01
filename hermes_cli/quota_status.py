"""Local quota telemetry readers for dashboard status chips.

The provider CLIs do not expose one stable cross-vendor quota command.  This
module keeps the dashboard read-only and defensive: it harvests quota windows
from live local CLI control surfaces when available, falls back to local CLI
telemetry, and reports explicit unavailable states when a provider only exposes
plan/auth data rather than true quota windows.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MAX_FILES_PER_PROVIDER = 300
MAX_LINES_PER_FILE = 2000
FIVE_HOUR_MINUTES = 5 * 60
WEEKLY_MINUTES = 7 * 24 * 60
LIVE_CODEX_TIMEOUT_SECONDS = 12
LIVE_CLAUDE_TIMEOUT_SECONDS = 8


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch_seconds(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    # Some tools may persist milliseconds. Codex currently writes seconds.
    if seconds > 10_000_000_000:
        seconds = seconds / 1000
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _window_key(window_minutes: Any) -> str:
    try:
        minutes = int(window_minutes)
    except (TypeError, ValueError):
        return "unknown"
    if minutes == FIVE_HOUR_MINUTES:
        return "five_hour"
    if minutes == WEEKLY_MINUTES:
        return "weekly"
    return f"{minutes}_minute"


def _window_label(key: str, window_minutes: Any) -> str:
    if key == "five_hour":
        return "5h"
    if key == "weekly":
        return "W"
    try:
        minutes = int(window_minutes)
    except (TypeError, ValueError):
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _parse_timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _jsonl_files(root: Path, pattern: str) -> list[Path]:
    if not root.exists():
        return []
    try:
        return sorted(
            root.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:MAX_FILES_PER_PROVIDER]
    except OSError:
        return []


def _iter_jsonl_objects(files: Iterable[Path]) -> Iterable[tuple[dict[str, Any], Path]]:
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for idx, line in enumerate(handle):
                    if idx >= MAX_LINES_PER_FILE:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        yield parsed, path
        except OSError:
            continue


def _extract_rate_limit_payload(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Return a rate_limits dict from known and future telemetry shapes."""
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
        return payload["rate_limits"]
    if isinstance(obj.get("rate_limits"), dict):
        return obj["rate_limits"]
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("rate_limits"), dict):
        return message["rate_limits"]
    diagnostics = message.get("diagnostics") if isinstance(message, dict) else None
    if isinstance(diagnostics, dict) and isinstance(diagnostics.get("rate_limits"), dict):
        return diagnostics["rate_limits"]
    return None


def _camel_or_snake(raw: dict[str, Any], snake: str, camel: str) -> Any:
    return raw.get(snake, raw.get(camel))


def _normalise_camel_window(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "used_percent": _camel_or_snake(raw, "used_percent", "usedPercent"),
        "window_minutes": _camel_or_snake(raw, "window_minutes", "windowDurationMins"),
        "resets_at": _camel_or_snake(raw, "resets_at", "resetsAt"),
    }


def _normalise_camel_credits(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "has_credits": _camel_or_snake(raw, "has_credits", "hasCredits"),
        "unlimited": raw.get("unlimited"),
        "balance": raw.get("balance"),
        "overage_limit_reached": _camel_or_snake(
            raw,
            "overage_limit_reached",
            "overageLimitReached",
        ),
        "approx_local_messages": _camel_or_snake(
            raw,
            "approx_local_messages",
            "approxLocalMessages",
        ),
        "approx_cloud_messages": _camel_or_snake(
            raw,
            "approx_cloud_messages",
            "approxCloudMessages",
        ),
    }


def _normalise_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "limit_id": _camel_or_snake(raw, "limit_id", "limitId"),
        "limit_name": _camel_or_snake(raw, "limit_name", "limitName"),
        "primary": _normalise_camel_window(raw.get("primary")),
        "secondary": _normalise_camel_window(raw.get("secondary")),
        "credits": _normalise_camel_credits(raw.get("credits")),
        "plan_type": _camel_or_snake(raw, "plan_type", "planType"),
        "rate_limit_reached_type": _camel_or_snake(
            raw,
            "rate_limit_reached_type",
            "rateLimitReachedType",
        ),
    }


def _normalise_window(raw: dict[str, Any], key_hint: str | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    raw = _normalise_camel_window(raw) or raw
    used_percent = raw.get("used_percent")
    window_minutes = raw.get("window_minutes")
    if used_percent is None and window_minutes is None:
        return None
    key = key_hint or _window_key(window_minutes)
    return {
        "key": key,
        "label": _window_label(key, window_minutes),
        "used_percent": used_percent,
        "window_minutes": window_minutes,
        "resets_at": raw.get("resets_at"),
        "resets_at_iso": _iso_from_epoch_seconds(raw.get("resets_at")),
    }


def _status_from_snapshot(
    snapshot: dict[str, Any],
    *,
    provider_id: str,
    provider_label: str,
    source: str,
    observed_at: float | None = None,
    no_window_error: str = "no_rate_limit_windows_found",
) -> dict[str, Any]:
    normalized = _normalise_snapshot(snapshot)
    windows: dict[str, dict[str, Any]] = {}

    for field in ("primary", "secondary"):
        raw_field = normalized.get(field)
        if not isinstance(raw_field, dict):
            continue
        window = _normalise_window(raw_field)
        if window:
            windows[window["key"]] = window

    windows_obj = snapshot.get("windows")
    if isinstance(windows_obj, dict):
        for key_hint, raw_window in windows_obj.items():
            raw = _normalise_camel_window(raw_window) or raw_window
            window = _normalise_window(raw, str(key_hint))
            if window:
                windows[window["key"]] = window

    return {
        "provider": provider_id,
        "label": provider_label,
        "available": bool(windows),
        "windows": dict(sorted(windows.items())),
        "plan_type": normalized.get("plan_type"),
        "credits": normalized.get("credits"),
        "rate_limit_reached_type": normalized.get("rate_limit_reached_type"),
        "source": source,
        "updated_at": datetime.fromtimestamp(observed_at or time.time(), tz=timezone.utc).isoformat(),
        "error": None if windows else no_window_error,
    }


def _empty_provider_status(
    *,
    provider_id: str,
    provider_label: str,
    source: str,
    error: str,
    plan_type: str | None = None,
    credits: dict[str, Any] | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider_id,
        "label": provider_label,
        "available": False,
        "windows": {},
        "plan_type": plan_type,
        "credits": credits,
        "rate_limit_reached_type": None,
        "source": source,
        "updated_at": updated_at,
        "error": error,
    }


def _collect_rate_limit_windows(
    files: Iterable[Path],
    *,
    provider_id: str,
    provider_label: str,
    source: str,
) -> dict[str, Any]:
    latest_by_key: dict[str, tuple[float, dict[str, Any]]] = {}
    latest_snapshot: tuple[float, dict[str, Any], Path] | None = None
    saw_file = False

    for obj, path in _iter_jsonl_objects(files):
        saw_file = True
        rate_limits = _extract_rate_limit_payload(obj)
        if not rate_limits:
            continue
        observed_at = obj.get("timestamp") or obj.get("time") or obj.get("created_at")
        observed_ts = _parse_timestamp(observed_at) or path.stat().st_mtime
        if latest_snapshot is None or observed_ts >= latest_snapshot[0]:
            latest_snapshot = (observed_ts, rate_limits, path)

        for field in ("primary", "secondary"):
            raw_field = rate_limits.get(field)
            if not isinstance(raw_field, dict):
                continue
            window = _normalise_window(raw_field)
            if not window:
                continue
            key = window["key"]
            if key not in latest_by_key or observed_ts >= latest_by_key[key][0]:
                latest_by_key[key] = (observed_ts, window)

        # Future-proof for providers that may emit named windows directly.
        windows_obj = rate_limits.get("windows")
        if isinstance(windows_obj, dict):
            for key_hint, raw_window in windows_obj.items():
                window = _normalise_window(raw_window, key_hint=str(key_hint))
                if not window:
                    continue
                key = window["key"]
                if key not in latest_by_key or observed_ts >= latest_by_key[key][0]:
                    latest_by_key[key] = (observed_ts, window)

    windows = {key: value for key, (_, value) in sorted(latest_by_key.items())}
    latest_rate_limits = _normalise_snapshot(latest_snapshot[1]) if latest_snapshot else {}
    latest_window_ts = max((ts for ts, _ in latest_by_key.values()), default=0.0)
    updated_at = (
        datetime.fromtimestamp(latest_window_ts, tz=timezone.utc).isoformat()
        if latest_window_ts
        else None
    )

    return {
        "provider": provider_id,
        "label": provider_label,
        "available": bool(windows),
        "windows": windows,
        "plan_type": latest_rate_limits.get("plan_type"),
        "credits": latest_rate_limits.get("credits"),
        "rate_limit_reached_type": latest_rate_limits.get("rate_limit_reached_type"),
        "source": source,
        "updated_at": updated_at,
        "error": None if windows else (
            "no_rate_limit_windows_found" if saw_file else "no_telemetry_files_found"
        ),
    }


def _codex_home(home: Path) -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else home / ".codex"


def _claude_home(home: Path) -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME")
    return Path(raw).expanduser() if raw else home / ".claude"


def _get_codex_app_server_rate_limits() -> dict[str, Any] | None:
    """Ask the installed Codex app-server for the live account snapshot the TUI uses."""
    request_lines = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "hermes-dashboard", "version": "0"},
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [],
                },
            },
        },
        {"id": 2, "method": "account/rateLimits/read", "params": None},
    ]
    payload = "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in request_lines)
    try:
        proc = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None

    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.flush()
        deadline = time.time() + LIVE_CODEX_TIMEOUT_SECONDS
        while time.time() < deadline:
            if proc.stdout is None:
                break
            readable, _, _ = select.select([proc.stdout], [], [], 0.2)
            if not readable:
                continue
            raw_line = proc.stdout.readline()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") != 2:
                continue
            result = obj.get("result")
            if not isinstance(result, dict):
                return None
            by_id = result.get("rateLimitsByLimitId")
            if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
                return by_id["codex"]
            snapshot = result.get("rateLimits")
            if isinstance(snapshot, dict):
                return snapshot
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return None


def _get_claude_auth_status() -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--json"],
            text=True,
            capture_output=True,
            timeout=LIVE_CLAUDE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("loggedIn") else None


def get_codex_quota_status(home: Path | None = None) -> dict[str, Any]:
    if home is None:
        live_snapshot = _get_codex_app_server_rate_limits()
        if live_snapshot is not None:
            return _status_from_snapshot(
                live_snapshot,
                provider_id="codex",
                provider_label="Codex",
                source="codex_app_server_live",
                no_window_error="codex_plan_has_no_fixed_5h_or_weekly_windows",
            )

    home = Path.home() if home is None else Path(home)
    root = _codex_home(home)
    return _collect_rate_limit_windows(
        _jsonl_files(root, "sessions/**/*.jsonl"),
        provider_id="codex",
        provider_label="Codex",
        source="codex_session_rate_limits",
    )


def get_claude_quota_status(home: Path | None = None) -> dict[str, Any]:
    home = Path.home() if home is None else Path(home)
    root = _claude_home(home)
    status = _collect_rate_limit_windows(
        _jsonl_files(root, "projects/**/*.jsonl"),
        provider_id="claude",
        provider_label="Claude Code",
        source="claude_code_local_telemetry",
    )
    if not status["available"] and home == Path.home():
        auth = _get_claude_auth_status()
        if auth:
            status["plan_type"] = auth.get("subscriptionType")
            status["source"] = "claude_auth_status"
            status["updated_at"] = _utc_now_iso()
    if not status["available"] and status["error"] == "no_rate_limit_windows_found":
        status["error"] = "claude_code_quota_windows_not_exposed_locally"
    return status


def get_quota_status(home: Path | None = None) -> dict[str, Any]:
    return {
        "generated_at": _utc_now_iso(),
        "providers": {
            "codex": get_codex_quota_status(home),
            "claude": get_claude_quota_status(home),
        },
    }
