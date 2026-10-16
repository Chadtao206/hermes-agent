"""GitHub reads and the single write this daemon is allowed to perform.

Not routed through scripts/pr/ghx_ro.sh: that wrapper exists for Boris's review
lanes and explicitly refuses ``--approve``. This is a different actor with a
different mandate, so it gets its own minimal path rather than a loosened
shared one.

Both calls use an argv list with no shell, and every interpolated value is a
validated scalar (owner/repo/number from the URL grammar, SHA matched against
/^[0-9a-f]{40}$/), so message text cannot reach the command line.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from jdi_autoapprove.models import PRFacts

_SHA = re.compile(r"^[0-9a-f]{40}$")
_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"


def _env(token: str) -> dict:
    return {"GH_TOKEN": token, "PATH": _PATH}


def read_token(path: Path) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def viewer_login(token: str, runner=subprocess.run) -> str:
    """The account the token acts as. Used to detect self-authored PRs.

    Resolved at startup rather than hardcoded so rotating to a machine account
    changes behaviour correctly with no code edit.
    """
    try:
        proc = runner(["gh", "api", "user", "--jq", ".login"],
                      capture_output=True, text=True, timeout=30, env=_env(token))
    except Exception:
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def fetch_pr(owner: str, repo: str, number: int, token: str,
             runner=subprocess.run) -> PRFacts | None:
    """Return PRFacts, or None on any failure. Never raises — callers fail closed."""
    try:
        proc = runner(
            ["gh", "pr", "view", str(number), "-R", f"{owner}/{repo}",
             "--json", "headRefOid,author,state,reviewDecision"],
            capture_output=True, text=True, timeout=30, env=_env(token),
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
    return PRFacts(
        head_sha=sha,
        author=str((data.get("author") or {}).get("login") or ""),
        state=str(data.get("state") or "").upper(),
        review_decision=str(data.get("reviewDecision") or "").upper(),
    )


def approve(owner: str, repo: str, number: int, head_sha: str, body: str,
            token: str, runner=subprocess.run) -> tuple[bool, str]:
    """Submit an APPROVE review pinned to head_sha. Returns (ok, detail).

    ``commit_id`` binds the review to that revision; it does not atomically
    assert that the PR head is unchanged. Current-head gate evidence must be
    checked separately.
    """
    if not _SHA.match((head_sha or "").lower()):
        return False, "refusing to approve without a valid head sha"
    argv = [
        "gh", "api", "--method", "POST",
        f"repos/{owner}/{repo}/pulls/{number}/reviews",
        "-f", f"commit_id={head_sha}",
        "-f", "event=APPROVE",
    ]
    if body:
        argv += ["-f", f"body={body}"]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=60, env=_env(token))
    except Exception as exc:
        return False, str(exc)[:300]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, ""
