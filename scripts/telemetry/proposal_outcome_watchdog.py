#!/usr/bin/env python3
"""Silent dry-run proposal outcome reconciliation watchdog for no-agent cron.

Behavior:
- Runs the live reconcile_proposal_outcomes.py script in dry-run mode only.
- Pins HOME and HERMES_HOME to the real user/default-profile paths.
- Emits a compact alert only when actionable dry-run findings appear and differ
  from the last alerted signature.
- Emits no stdout on clean runs or unchanged repeated findings.
- Exits non-zero on watchdog/script failure so cron can raise an error alert.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

USER_HOME = Path("/Users/ctao")
HERMES_HOME = USER_HOME / ".hermes"
REPO = HERMES_HOME / "hermes-agent"
PYTHON = REPO / "venv" / "bin" / "python"
RECONCILE_SCRIPT = HERMES_HOME / "scripts" / "telemetry" / "reconcile_proposal_outcomes.py"
STATE_PATH = HERMES_HOME / "state" / "proposal_outcome_watchdog_state.json"
TIMEOUT_SECONDS = 120
MAX_ITEMS = 5


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}



def save_state(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)



def actionable_observations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    observations = payload.get("observations") or []
    actionable: list[dict[str, Any]] = []
    for observation in observations:
        transition = observation.get("transition") or {}
        if bool(transition.get("execute_update")):
            actionable.append(observation)
    actionable.sort(key=lambda item: str(item.get("proposal_id") or ""))
    return actionable



def signature_payload(observation: dict[str, Any]) -> dict[str, Any]:
    transition = observation.get("transition") or {}
    kanban = observation.get("kanban") or {}
    return {
        "proposal_id": observation.get("proposal_id"),
        "action": transition.get("action"),
        "kanban_task_id": transition.get("kanban_task_id") or kanban.get("resolved_task_id"),
        "kanban_task_status": transition.get("kanban_task_status") or kanban.get("status"),
        "new_status": transition.get("new_status"),
        "new_outcome": transition.get("new_outcome"),
        "link_source": kanban.get("link_source"),
        "reason": transition.get("reason"),
    }



def actionable_signature(actionable: list[dict[str, Any]]) -> str:
    material = [signature_payload(item) for item in actionable]
    raw = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()



def format_item(observation: dict[str, Any]) -> str:
    transition = observation.get("transition") or {}
    kanban = observation.get("kanban") or {}
    proposal_id = str(observation.get("proposal_id") or "unknown-proposal")
    task_id = transition.get("kanban_task_id") or kanban.get("resolved_task_id") or "unlinked"
    task_status = transition.get("kanban_task_status") or kanban.get("status") or "unknown"
    link_source = kanban.get("link_source") or "none"
    new_status = transition.get("new_status") or "unknown"
    new_outcome = transition.get("new_outcome") or "unknown"
    action = transition.get("action") or "unknown"
    return (
        f"- {proposal_id} | task={task_id} ({task_status}) | "
        f"plan={new_status}/{new_outcome} | action={action} | link={link_source}"
    )



def main() -> int:
    if not PYTHON.exists():
        print(f"missing Python executable: {PYTHON}", file=sys.stderr)
        return 1
    if not RECONCILE_SCRIPT.exists():
        print(f"missing reconcile script: {RECONCILE_SCRIPT}", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["HOME"] = str(USER_HOME)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env.setdefault("PYTHONPATH", str(REPO))

    proc = subprocess.run(
        [str(PYTHON), str(RECONCILE_SCRIPT), "--json"],
        cwd=str(REPO),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        print(
            "proposal outcome watchdog command failed\n"
            f"exit={proc.returncode}\n"
            f"stderr={proc.stderr.strip()}\n"
            f"stdout={(proc.stdout or '')[:2000].strip()}",
            file=sys.stderr,
        )
        return proc.returncode or 1

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        print(
            "proposal outcome watchdog returned non-JSON output\n"
            f"error={exc}\n"
            f"stderr={proc.stderr.strip()}\n"
            f"stdout={(proc.stdout or '')[:2000].strip()}",
            file=sys.stderr,
        )
        return 1

    if not payload.get("ok", False):
        print(f"proposal outcome watchdog returned not-ok payload: {json.dumps(payload, sort_keys=True)}", file=sys.stderr)
        return 1

    actionable = actionable_observations(payload)
    current_signature = actionable_signature(actionable)
    previous = load_state()
    previous_signature = str(previous.get("actionable_signature") or "")

    state = {
        "last_checked_at": int(time.time()),
        "last_mode": payload.get("mode"),
        "last_observed": int(payload.get("observed") or 0),
        "last_eligible_updates": int(payload.get("eligible_updates") or 0),
        "actionable_signature": current_signature,
        "actionable_count": len(actionable),
        "actionable_items": [signature_payload(item) for item in actionable],
    }
    save_state(state)

    if not actionable:
        return 0
    if current_signature == previous_signature:
        return 0

    lines = [
        (
            "PROPOSAL OUTCOME WATCHDOG: "
            f"{len(actionable)} actionable dry-run reconciliation item(s) detected."
        ),
        (
            f"Observed={payload.get('observed', 0)} | "
            f"Eligible={payload.get('eligible_updates', 0)} | mode={payload.get('mode')}"
        ),
    ]
    for observation in actionable[:MAX_ITEMS]:
        lines.append(format_item(observation))
    remaining = len(actionable) - MAX_ITEMS
    if remaining > 0:
        lines.append(f"- ... {remaining} more item(s)")
    lines.append("Dry-run only; no DB mutation performed.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
