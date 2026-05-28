#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_health  # noqa: E402


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run canonical read-only Kanban DB health phases with phase-level attribution."
        )
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Explicit DB path (default: board-resolved kanban_db_path).",
    )
    parser.add_argument(
        "--board",
        default=None,
        help="Board slug used when --db-path is omitted.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Per-phase timeout for sqlite3 CLI and Python connect (default: 20).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON payload (default: human-readable text).",
    )
    return parser


def _format_text(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"kanban_db: {result.get('db_path')}")
    lines.append(f"ok: {result.get('ok')}")
    lines.append(f"failure_count: {result.get('failure_count')}")
    for phase in result.get("phases", []):
        status = phase.get("status")
        name = phase.get("phase")
        lines.append(f"- {name}: {status}")
        if status == "skipped":
            lines.append(f"  reason: {phase.get('reason')}")
        if phase.get("exception_class"):
            lines.append(
                f"  exception: {phase.get('exception_class')}: {phase.get('exception_message')}"
            )
        if phase.get("message"):
            lines.append(f"  message: {phase.get('message')}")
        if "quick_check_rows" in phase:
            lines.append(f"  quick_check_rows: {phase.get('quick_check_rows')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.db_path:
        db_path = Path(args.db_path).expanduser()
    else:
        db_path = kb.kanban_db_path(board=args.board)

    result = kanban_health.run_readonly_health_bundle(
        db_path,
        timeout_seconds=max(1.0, float(args.timeout_seconds or 20.0)),
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(_format_text(result))

    return 0 if result.get("ok") else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
