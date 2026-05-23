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
import hashlib
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

# Make the repository importable when the script is executed directly from the
# checked-out scripts/ directory or copied into HERMES_HOME/scripts/ for cron.
def _candidate_repo_roots() -> list[Path]:
    script_path = Path(__file__).resolve()
    candidates: list[Path] = []
    env_repo = os.environ.get("HERMES_AGENT_REPO")
    if env_repo:
        candidates.append(Path(env_repo).expanduser())
    # Repo checkout: <repo>/scripts/this_file.py
    candidates.append(script_path.parents[1])
    # Cron copy: <HERMES_HOME>/scripts/this_file.py, repo often sits beside it.
    candidates.append(script_path.parents[1] / "hermes-agent")
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "hermes-agent")
    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def _resolve_repo_root() -> Path:
    for candidate in _candidate_repo_roots():
        if (candidate / "hermes_cli" / "kanban_reconciler.py").is_file():
            return candidate
    # Preserve the old behavior as a final fallback; import will fail loudly if
    # no installed package / checkout is available.
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _resolve_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_reconciler as rec  # noqa: E402

_VOLATILE_DETAIL_KEYS = {
    "age_seconds",
    "created_at",
    "started_at",
    "last_heartbeat_at",
    "current_run_id",
    "claim_expires",
    "worker_pid",
    "run_id",
}


def _bucket_counts(triage: dict[str, Any]) -> str:
    summary = triage.get("summary") or {}
    return (
        f"auto_silent={int(summary.get(rec.WAKE_BUCKET_AUTO_SILENT) or 0)}, "
        f"compact_notify={int(summary.get(rec.WAKE_BUCKET_COMPACT_NOTIFY) or 0)}, "
        f"jensen_decision_required={int(summary.get(rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED) or 0)}"
    )


def default_cron_script_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "scripts"
    except Exception:
        return Path.home() / ".hermes" / "scripts"


def cron_setup_instructions(
    *,
    schedule: str = "every 15m",
    deliver: str = "origin",
    script_name: str = "kanban_reconcile_wake_triage.py",
    job_name: str = "Kanban reconcile wake triage",
) -> str:
    """Return a non-mutating operator runbook for creating the cron job."""
    source_script = Path(__file__).resolve()
    script_dir = default_cron_script_dir()
    target_script = script_dir / script_name
    prompt = (
        "Script-only Kanban reconcile wake triage. The script emits nothing/"
        "wakeAgent=false for suppressed repeats and compact text only when "
        "operator action is needed."
    )
    cron_command = " \\\n  ".join([
        "hermes cron create " + shlex.quote(schedule),
        shlex.quote(prompt),
        "--name " + shlex.quote(job_name),
        "--script " + shlex.quote(script_name),
        "--no-agent",
        "--deliver " + shlex.quote(deliver),
    ])
    payload = {
        "action": "create",
        "name": job_name,
        "schedule": schedule,
        "prompt": prompt,
        "script": script_name,
        "no_agent": True,
        "deliver": deliver,
    }
    lines = [
        "Kanban reconcile wake-triage cron setup (example only; does not create a job)",
        "",
        "1. Mirror the script into the cron script directory:",
        "```bash",
        f"mkdir -p {shlex.quote(str(script_dir))}",
        f"cp {shlex.quote(str(source_script))} {shlex.quote(str(target_script))}",
        "```",
        "",
        "2. Create the script-only cron job after approving cadence and delivery:",
        "```bash",
        cron_command,
        "```",
        "",
        "Equivalent cronjob tool payload:",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
        "",
        "Notes:",
        "- The job is script-only (`--no-agent`): no LLM runs for compact/suppressed signals.",
        "- Repeated unchanged signals are deduped in a sidecar JSON file outside kanban.db.",
        "- Do not enable this automatically without explicit approval of schedule and delivery target.",
    ]
    return "\n".join(lines)


def default_state_path() -> Path:
    """Return the sidecar state path used for emission dedupe.

    This deliberately lives outside ``kanban.db`` so monitoring state cannot
    corrupt or contend with the authoritative board database.
    """
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "kanban_reconcile_wake_triage_state.json"
    except Exception:
        return Path.home() / ".hermes" / "kanban_reconcile_wake_triage_state.json"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _normalize_for_digest(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_for_digest(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_DETAIL_KEYS
        }
    if isinstance(value, list):
        return [_normalize_for_digest(item) for item in value]
    return value


def stable_action_digest(result: dict[str, Any]) -> str:
    """Fingerprint reconcile signal excluding volatile age/timestamp fields."""
    actions = []
    for action in result.get("actions") or []:
        if not isinstance(action, dict):
            continue
        actions.append({
            "kind": action.get("kind"),
            "task_id": action.get("task_id"),
            "severity": action.get("severity"),
            "safe_to_apply": bool(action.get("safe_to_apply")),
            "details": _normalize_for_digest(action.get("details") or {}),
        })
    actions.sort(key=lambda item: _stable_json(item))
    triage = result.get("wake_triage") or {}
    material = {
        "board": result.get("board"),
        "mode": triage.get("mode"),
        "actions": actions,
    }
    return hashlib.sha1(_stable_json(material).encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        # Corrupt dedupe state must not block watchdog output. Treat it as
        # empty; the next emitted signal will replace it atomically.
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def dedupe_key(*, mode: str, result: dict[str, Any]) -> str:
    triage = result.get("wake_triage") or {}
    board = str(result.get("board") or "default")
    triage_mode = str(triage.get("mode") or "unknown")
    return f"{mode}:{board}:{triage_mode}"


def should_emit_with_dedupe(
    result: dict[str, Any],
    *,
    mode: str,
    state_path: Path,
    min_repeat_seconds: int,
    now: int | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Return whether to emit and persist sidecar dedupe state if needed."""
    triage = result.get("wake_triage") or {}
    triage_mode = triage.get("mode")
    if triage_mode == rec.WAKE_BUCKET_AUTO_SILENT:
        return True, {"dedupe": "auto_silent_not_tracked"}

    as_of = int(now if now is not None else time.time())
    repeat_window = max(0, int(min_repeat_seconds or 0))
    digest = stable_action_digest(result)
    key = dedupe_key(mode=mode, result=result)
    state = load_state(state_path)
    entries = state.setdefault("entries", {})
    previous = entries.get(key) if isinstance(entries, dict) else None
    previous = previous if isinstance(previous, dict) else {}
    previous_digest = previous.get("digest")
    previous_emitted_at = int(previous.get("emitted_at") or 0)
    age = as_of - previous_emitted_at if previous_emitted_at else None

    suppressed = (
        previous_digest == digest
        and previous_emitted_at > 0
        and repeat_window > 0
        and as_of - previous_emitted_at < repeat_window
    )
    metadata = {
        "dedupe": "suppressed" if suppressed else "emitted",
        "dedupe_key": key,
        "digest": digest,
        "previous_emitted_at": previous_emitted_at or None,
        "age_seconds": age,
        "min_repeat_seconds": repeat_window,
        "state_path": str(state_path),
    }
    if suppressed:
        return False, metadata

    entries[key] = {
        "digest": digest,
        "mode": mode,
        "triage_mode": triage_mode,
        "board": result.get("board"),
        "emitted_at": as_of,
        "total_actions": triage.get("total_actions"),
    }
    state["version"] = 1
    state["updated_at"] = as_of
    save_state(state_path, state)
    return True, metadata


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


def _slack_safe_truncate(text: str, *, max_chars: int) -> str:
    """Bound watchdog output so Slack delivery never floods context/channels."""
    limit = int(max_chars or 0)
    if limit <= 0 or len(text) <= limit:
        return text
    marker = (
        "\n\n[truncated: output exceeded "
        f"{limit} chars; run `hermes kanban reconcile --json` for full details]"
    )
    if limit <= len(marker):
        return marker[-limit:]
    return text[: limit - len(marker)].rstrip() + marker


def render_output(
    result: dict[str, Any],
    *,
    mode: str = "no-agent",
    examples: int = 3,
    json_output: bool = False,
    max_chars: int = 3500,
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
                    max_chars=max_chars,
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
        return _slack_safe_truncate(_compact_notify_text(result, examples=examples), max_chars=max_chars)

    if triage_mode == rec.WAKE_BUCKET_JENSEN_DECISION_REQUIRED:
        return _slack_safe_truncate(_jensen_prompt_text(result, examples=examples), max_chars=max_chars)

    # Conservative fallback for future modes: produce a compact notification
    # rather than silent failure.
    return _slack_safe_truncate(_compact_notify_text(result, examples=examples), max_chars=max_chars)


def render_suppressed_output(result: dict[str, Any], metadata: dict[str, Any]) -> str:
    triage = result.get("wake_triage") or {}
    return json.dumps({
        "wakeAgent": False,
        "mode": triage.get("mode"),
        "dedupe": "suppressed",
        "dedupe_key": metadata.get("dedupe_key"),
        "age_seconds": metadata.get("age_seconds"),
        "min_repeat_seconds": metadata.get("min_repeat_seconds"),
    })


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
        "--max-chars",
        type=int,
        default=3500,
        help="Maximum text output characters for Slack-safe delivery (default: 3500; <=0 disables)",
    )
    parser.add_argument(
        "--mode",
        choices=["no-agent", "agent-gate"],
        default="no-agent",
        help="Output contract: no-agent delivers compact text; agent-gate wakes only for Jensen decisions.",
    )
    parser.add_argument("--json", action="store_true", help="Emit diagnostic JSON for tests/debugging")
    parser.add_argument(
        "--state-path",
        default=None,
        help="Sidecar dedupe state path (default: $HERMES_HOME/kanban_reconcile_wake_triage_state.json)",
    )
    parser.add_argument(
        "--min-repeat-seconds",
        type=int,
        default=60 * 60,
        help="Suppress unchanged emitted signals for this many seconds (default: 3600)",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Disable sidecar dedupe/rate-limit state for this run",
    )
    parser.add_argument(
        "--print-cron-setup",
        action="store_true",
        help="Print a non-mutating cron setup runbook and exit without reconciling",
    )
    parser.add_argument(
        "--setup-schedule",
        default="every 15m",
        help="Schedule to include in --print-cron-setup output (default: every 15m)",
    )
    parser.add_argument(
        "--setup-deliver",
        default="origin",
        help="Delivery target to include in --print-cron-setup output (default: origin)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.print_cron_setup:
        print(cron_setup_instructions(
            schedule=str(args.setup_schedule or "every 15m"),
            deliver=str(args.setup_deliver or "origin"),
        ))
        return 0

    result = rec.run_reconciler(
        board=args.board,
        ready_age_seconds=max(1, int(args.ready_age_seconds or 900)),
    )
    if not args.json and not args.no_dedupe:
        emit, metadata = should_emit_with_dedupe(
            result,
            mode=args.mode,
            state_path=Path(args.state_path).expanduser() if args.state_path else default_state_path(),
            min_repeat_seconds=max(0, int(args.min_repeat_seconds or 0)),
        )
        if not emit:
            print(render_suppressed_output(result, metadata))
            return 0

    output = render_output(
        result,
        mode=args.mode,
        examples=max(0, int(args.examples or 0)),
        json_output=bool(args.json),
        max_chars=int(args.max_chars or 0),
    )
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
