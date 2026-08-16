"""Write-capable GitHub adapter for the remediation loop.

Deliberately NOT ghx_ro.sh: that wrapper is the review path's read-only
boundary and stays that way. This adapter carries the write credential and is
the only component in the system that may mutate a pull request.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pr_remediation.config import RemediationConfig

_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
TIMEOUT = 30


class GitHubIO:
    def __init__(self, cfg: RemediationConfig, token_path: Path, runner=subprocess.run):
        self._cfg = cfg
        self._token_path = Path(token_path)
        self._runner = runner

    def _run(self, args: list[str]) -> str | None:
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            proc = self._runner(
                ["gh", *args], capture_output=True, text=True, timeout=TIMEOUT,
                env={"PATH": _PATH, "GH_TOKEN": token},
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    def _view(self, owner: str, repo: str, pr: int) -> dict | None:
        out = self._run([
            "pr", "view", str(pr), "-R", f"{owner}/{repo}",
            "--json", "labels,headRefOid,author",
        ])
        if out is None:
            return None
        try:
            data = json.loads(out or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def head_sha(self, owner: str, repo: str, pr: int) -> str | None:
        data = self._view(owner, repo, pr)
        return (data or {}).get("headRefOid") or None

    def author(self, owner: str, repo: str, pr: int) -> str | None:
        data = self._view(owner, repo, pr)
        return ((data or {}).get("author") or {}).get("login") or None

    def has_hold(self, owner: str, repo: str, pr: int) -> bool:
        """True also when GitHub could not be read: an unknown state is a hold."""
        data = self._view(owner, repo, pr)
        if data is None:
            return True
        names = {str(l.get("name", "")) for l in data.get("labels") or []}
        return self._cfg.hold_label in names

    def claim(self, owner: str, repo: str, pr: int) -> None:
        self._run(["pr", "edit", str(pr), "-R", f"{owner}/{repo}",
                   "--add-label", self._cfg.owner_label])

    def release(self, owner: str, repo: str, pr: int) -> None:
        self._run(["pr", "edit", str(pr), "-R", f"{owner}/{repo}",
                   "--remove-label", self._cfg.owner_label])
