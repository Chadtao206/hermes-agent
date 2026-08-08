"""Resolve authoritative PR facts. Head SHA never comes from Slack."""
from __future__ import annotations

import json
import re
import subprocess

from boris_intake.models import PRInfo

GHX_RO = "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def fetch_pr(owner: str, repo: str, number: int, runner=subprocess.run) -> PRInfo | None:
    """Return PRInfo, or None on any failure. Never raises — callers fail closed."""
    try:
        proc = runner(
            [GHX_RO, "pr", "view", str(number), "-R", f"{owner}/{repo}",
             "--json", "headRefOid,author,state"],
            capture_output=True, text=True, timeout=30,
            env={"BORIS_PR_SCOPE": f"{owner}/{repo}#{number}",
                 "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
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
