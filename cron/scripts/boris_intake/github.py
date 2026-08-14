"""Resolve authoritative PR facts. Head SHA never comes from Slack."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from boris_intake.models import GateSnapshot, PRInfo
from boris_intake.result import ValidationError, build_gate_snapshot, load_gate_registry

GHX_RO = "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_GATE_REGISTRY = Path(__file__).with_name("gate_registry.json")
_RUN_ENV = {
    "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
}


def fetch_pr(owner: str, repo: str, number: int, runner=subprocess.run) -> PRInfo | None:
    """Return PRInfo, or None on any failure. Never raises — callers fail closed."""
    try:
        proc = runner(
            [GHX_RO, "pr", "view", str(number), "-R", f"{owner}/{repo}",
             "--json", "headRefOid,author,state"],
            capture_output=True, text=True, timeout=30,
            env={"BORIS_PR_SCOPE": f"{owner}/{repo}#{number}", **_RUN_ENV},
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    sha = str(data.get("headRefOid") or "").lower()
    if not _SHA.match(sha):
        return None
    return PRInfo(
        head_sha=sha,
        author=str((data.get("author") or {}).get("login") or ""),
        state=str(data.get("state") or "").upper(),
    )


def _run_json(command: list[str], scope: str, runner, allowed_codes=(0,)):
    try:
        proc = runner(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            env={"BORIS_PR_SCOPE": scope, **_RUN_ENV},
        )
    except Exception:
        return None
    if proc.returncode not in allowed_codes:
        return None
    try:
        return json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None


def fetch_gate_snapshot(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    captured_at: str,
    runner=subprocess.run,
) -> GateSnapshot | None:
    scope = f"{owner}/{repo}#{number}"
    repository = f"{owner}/{repo}"
    head = _run_json(
        [GHX_RO, "pr", "view", str(number), "-R", repository, "--json", "headRefOid"],
        scope,
        runner,
    )
    if not isinstance(head, dict):
        return None
    current_head = str(head.get("headRefOid") or "").lower()
    if not _SHA.fullmatch(current_head) or current_head != expected_head_sha.lower():
        return None

    checks = _run_json(
        [GHX_RO, "pr", "checks", str(number), "-R", repository, "--json", "name,state,link"],
        scope,
        runner,
        (0, 1),
    )
    required = _run_json(
        [
            GHX_RO,
            "pr",
            "checks",
            str(number),
            "-R",
            repository,
            "--required",
            "--json",
            "name,state,link",
        ],
        scope,
        runner,
        (0, 1),
    )
    if not isinstance(checks, list) or not isinstance(required, list):
        return None
    if not all(isinstance(item, dict) for item in checks + required):
        return None
    required_names = {str(item.get("name") or "") for item in required}
    all_names = {str(item.get("name") or "") for item in checks}
    if not required_names <= all_names:
        return None
    rows = [dict(item, required=str(item.get("name") or "") in required_names) for item in checks]
    try:
        return build_gate_snapshot(rows, captured_at, load_gate_registry(_GATE_REGISTRY))
    except ValidationError:
        return None
