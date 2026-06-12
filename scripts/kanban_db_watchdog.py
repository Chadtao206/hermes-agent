#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_board_doctor  # noqa: E402

SEVERITY_ORDER = {
    "info": 0,
    "warning": 1,
    "error": 2,
    "critical": 3,
}


def _severity_value(value: str) -> int:
    return SEVERITY_ORDER.get(str(value or "").lower(), 0)


def select_issues(result: dict[str, Any], min_severity: str) -> list[dict[str, Any]]:
    threshold = _severity_value(min_severity)
    issues = result.get("issues") or []
    return [
        issue for issue in issues
        if _severity_value(str(issue.get("severity") or "")) >= threshold
    ]


def _human_kind(value: Any) -> str:
    return str(value or "unknown").replace("_", " ")


def _yes_no(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _format_detail_value(value: Any) -> str:
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _issue_label(issue: dict[str, Any]) -> str:
    kind = _human_kind(issue.get("kind"))
    severity = str(issue.get("severity") or "unknown").upper()
    task_id = issue.get("task_id")
    run_id = issue.get("run_id")
    if task_id:
        return f"{task_id}: {kind} ({severity})"
    if run_id:
        return f"run {run_id}: {kind} ({severity})"
    return f"{kind} ({severity})"


def _issue_details(issue: dict[str, Any]) -> str:
    preferred = [
        "assignee",
        "profile",
        "worker_pid",
        "pid_alive",
        "claim_expired",
        "heartbeat_age_seconds",
        "run_id",
        "task_status",
        "age_seconds",
        "parents",
    ]
    parts: list[str] = []
    for key in preferred:
        if key not in issue:
            continue
        value = issue.get(key)
        if value is None or value == "":
            continue
        parts.append(f"{key}={_format_detail_value(value)}")
    return ", ".join(parts)


def _command_suggestions(issues: list[dict[str, Any]]) -> list[str]:
    task_ids = []
    for issue in issues:
        task_id = issue.get("task_id")
        if task_id and task_id not in task_ids:
            task_ids.append(str(task_id))
    if not task_ids:
        return []

    first = task_ids[0]
    commands = [
        f"hermes kanban show {first}",
        f"hermes kanban log {first}",
    ]
    if any((issue.get("kind") == "stale_running_task") for issue in issues):
        commands.append(
            f"hermes kanban reclaim {first} --reason \"watchdog: dead or stale worker\""
        )
    return commands


def _format_count_map(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    parts = [
        f"{_human_kind(kind)}={count}"
        for kind, count in sorted(value.items(), key=lambda item: str(item[0]))
    ]
    return ", ".join(parts)


def _summarize_reconcile(reconcile: dict[str, Any]) -> list[str]:
    if not reconcile:
        return []

    lines = ["Reconcile context:"]
    lines.append(
        f"- Open actions: {int(reconcile.get('action_count') or 0)} "
        f"({_format_count_map(reconcile.get('kinds'))})"
    )
    wake_mode = reconcile.get("wake_mode")
    if wake_mode:
        wake_agent = _yes_no(bool(reconcile.get("wake_agent")))
        lines.append(f"- Wake mode: {wake_mode}; wake_agent={wake_agent}")

    suppressed_count = int(reconcile.get("suppressed_decision_packet_count") or 0)
    if suppressed_count:
        packets = reconcile.get("suppressed_decision_packets") or []
        task_bits = []
        for packet in packets[:4]:
            if not isinstance(packet, dict):
                continue
            task_id = packet.get("task_id")
            option = packet.get("option")
            if task_id and option:
                task_bits.append(f"{task_id}:{option}")
            elif task_id:
                task_bits.append(str(task_id))
        suffix = f" ({', '.join(task_bits)})" if task_bits else ""
        lines.append(
            f"- Already acknowledged decision packets: {suppressed_count}{suffix}"
        )

    suppressed_doctor = int(reconcile.get("suppressed_doctor_issue_count") or 0)
    if suppressed_doctor:
        lines.append(
            f"- Related doctor issues hidden by acknowledgements: {suppressed_doctor}"
        )
    return lines


def build_message(
    result: dict[str, Any],
    *,
    min_severity: str,
    max_issues: int = 6,
) -> str:
    issues = select_issues(result, min_severity)
    board = result.get("board") or "default"
    issue_word = "issue" if len(issues) == 1 else "issues"
    lines = [
        f"Kanban store needs attention: {len(issues)} {min_severity}+ {issue_word} on {board}",
        f"Store: {result.get('db_path')}",
        "",
        "Immediate issue:" if len(issues) == 1 else "Immediate issues:",
    ]
    for issue in issues[:max_issues]:
        message = issue.get("message") or "(no message)"
        lines.append(f"- {_issue_label(issue)}")
        lines.append(f"  Why: {message}")
        details = _issue_details(issue)
        if details:
            lines.append(f"  Evidence: {details}")
        action = issue.get("action")
        if action:
            lines.append(f"  Next: {action}")
    if len(issues) > max_issues:
        lines.append(f"- ...and {len(issues) - max_issues} more issue(s)")

    commands = _command_suggestions(issues)
    if commands:
        lines.extend(["", "Useful commands:"])
        for command in commands:
            lines.append(f"- {command}")

    reconcile = result.get("reconcile_summary") or {}
    reconcile_lines = _summarize_reconcile(reconcile)
    if reconcile_lines:
        lines.append("")
        lines.extend(reconcile_lines)
    return "\n".join(lines)



def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only kanban store watchdog. Silent on healthy boards; prints only "
            "when board-doctor findings meet the requested severity threshold."
        )
    )
    parser.add_argument(
        "--board",
        default=None,
        help="Kanban board slug (default: current board resolution chain)",
    )
    parser.add_argument(
        "--ready-age-seconds",
        type=int,
        default=15 * 60,
        help="Doctor threshold for old ready tasks (default: 900)",
    )
    parser.add_argument(
        "--min-severity",
        choices=tuple(SEVERITY_ORDER.keys()),
        default="critical",
        help="Emit only findings at or above this severity (default: critical)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of text when findings match the threshold",
    )
    return parser



def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    result = kanban_board_doctor.run_board_doctor(
        board=args.board,
        ready_age_seconds=max(1, int(args.ready_age_seconds or 900)),
    )
    matched = select_issues(result, args.min_severity)
    if not matched:
        return 0

    payload = copy.deepcopy(result)
    payload["issues"] = matched
    payload["watchdog_threshold"] = args.min_severity
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(build_message(payload, min_severity=args.min_severity))

    return 2 if any(
        str(issue.get("severity") or "").lower() == "critical"
        for issue in matched
    ) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
