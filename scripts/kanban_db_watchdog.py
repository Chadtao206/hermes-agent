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



def build_message(
    result: dict[str, Any],
    *,
    min_severity: str,
    max_issues: int = 6,
) -> str:
    issues = select_issues(result, min_severity)
    board = result.get("board") or "default"
    lines = [
        f"Kanban store watchdog alert ({board})",
        f"store: {result.get('db_path')}",
        f"matched_issues: {len(issues)} (threshold={min_severity})",
    ]
    for issue in issues[:max_issues]:
        severity = issue.get("severity") or "unknown"
        kind = issue.get("kind") or "unknown"
        message = issue.get("message") or "(no message)"
        lines.append(f"- {severity}/{kind}: {message}")
        action = issue.get("action")
        if action:
            lines.append(f"  action: {action}")
    reconcile = result.get("reconcile_summary") or {}
    if reconcile:
        lines.append(
            "reconcile_summary: "
            + json.dumps(reconcile, ensure_ascii=False, sort_keys=True)
        )
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
