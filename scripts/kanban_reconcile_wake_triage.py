#!/usr/bin/env python3
"""Script-first Kanban reconcile wake triage wrapper.

Designed for cron/script watchdogs.  It runs the read-only Kanban reconciler,
uses the deterministic ``wake_triage`` metadata, and emits only the minimum
operator signal needed for the selected mode.

Default ``--mode no-agent`` is intended for cron ``no_agent=True`` jobs:
  * auto_silent -> ``{"wakeAgent": false}`` (scheduler treats as silent)
  * compact_notify -> compact Slack-safe text, no LLM
  * jensen_decision_required -> compact Jensen handoff prompt, no mutation

``--mode agent-gate`` is intended as a pre-run script for an LLM cron job:
  * auto_silent / compact_notify -> ``{"wakeAgent": false}``
  * jensen_decision_required -> compact prompt so the agent wakes only then
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Make the repository importable when the script is executed directly from the
# checked-out scripts/ directory or copied into a profile-local scripts folder.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_reconciler as rec  # noqa: E402


def _bucket_counts(triage: dict[str, Any]) -> str:
    summary = triage.get("summary") or {}
    return (
        f"auto_silent={int(summary.get(rec.WAKE_BUCKET_AUTO_SILENT) or 0)}, "
        f"compact_notify={int(summary.get(rec.WAKE_BUCKET_COMPACT_NOTIFY) or 0)}, "
        f"jensen_decision_required={int(summary.get(rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED) or 0)}"
    )


def _compact_notify_text(result: dict[str, Any], *, examples: int) -> str:
    triage = result.get("wake_triage") or {}
    lines = [
        "Kanban reconcile compact notification",
        f"Mode: {triage.get('mode')} | wake_agent={str(bool(triage.get('wake_agent'))).lower()}",
        f"Actions: {int(triage.get('total_actions') or 0)} | Buckets: {_bucket_counts(triage)}",
        "",
        rec.format_reconcile_text(result, max_examples=examples),
    ]
    return "\n".join(lines).strip()


def _jensen_prompt_text(result: dict[str, Any], *, examples: int) -> str:
    triage = result.get("wake_triage") or {}
    lines = [
        "Kanban reconcile requires Jensen decision",
        "",
        "Objective: resolve Kanban reconcile decision-required actions without broad dispatcher mutation.",
        "Constraints: keep actions deterministic, preserve DB integrity, avoid retry storms, and mutate only after explicit safe scope.",
        f"Mode: {triage.get('mode')} | wake_agent={str(bool(triage.get('wake_agent'))).lower()}",
        f"Actions: {int(triage.get('total_actions') or 0)} | Buckets: {_bucket_counts(triage)}",
        f"Reason: {triage.get('reason')}",
        "",
        rec.format_reconcile_text(result, max_examples=examples),
        "",
        "Recommended Jensen action: inspect the decision-required examples, choose keep-parked/unblock/close/remediate explicitly, then re-run `hermes kanban reconcile --json`.",
    ]
    return "\n".join(lines).strip()


def render_output(
    result: dict[str, Any],
    *,
    mode: str = "no-agent",
    examples: int = 3,
    json_output: bool = False,
) -> str:
    """Render cron/script output for a reconciler result."""
    triage = result.get("wake_triage") or rec.classify_wake_triage(result.get("actions") or [])
    triage_mode = triage.get("mode")

    if json_output:
        return json.dumps(
            {
                "mode": mode,
                "triage": triage,
                "result": result,
                "rendered_text": render_output(
                    result,
                    mode=mode,
                    examples=examples,
                    json_output=False,
                ),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )

    if triage_mode == rec.WAKE_BUCKET_AUTO_SILENT:
        return json.dumps({"wakeAgent": False, "mode": triage_mode})

    if mode == "agent-gate" and triage_mode != rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED:
        return json.dumps({"wakeAgent": False, "mode": triage_mode})

    if triage_mode == rec.WAKE_BUCKET_COMPACT_NOTIFY:
        return _compact_notify_text(result, examples=examples)

    if triage_mode == rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED:
        return _jensen_prompt_text(result, examples=examples)

    # Conservative fallback for future modes: produce a compact notification
    # rather than silent failure.
    return _compact_notify_text(result, examples=examples)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run kanban reconcile and emit deterministic wake-triage script output."
    )
    parser.add_argument("--board", default=None, help="Kanban board slug (default: current board)")
    parser.add_argument(
        "--ready-age-seconds",
        type=int,
        default=15 * 60,
        help="Ready/review age threshold passed through to reconcile (default: 900)",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=3,
        help="Maximum examples in compact text output (default: 3)",
    )
    parser.add_argument(
        "--mode",
        choices=["no-agent", "agent-gate"],
        default="no-agent",
        help="Output contract: no-agent delivers compact text; agent-gate wakes only for Jensen decisions.",
    )
    parser.add_argument("--json", action="store_true", help="Emit diagnostic JSON for tests/debugging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = rec.run_reconciler(
        board=args.board,
        ready_age_seconds=max(1, int(args.ready_age_seconds or 900)),
    )
    output = render_output(
        result,
        mode=args.mode,
        examples=max(0, int(args.examples or 0)),
        json_output=bool(args.json),
    )
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
