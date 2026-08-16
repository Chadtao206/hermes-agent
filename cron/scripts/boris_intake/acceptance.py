"""Pre-authorized acceptance evidence for a review request.

The reviewer may only read acceptance evidence that was authorized for its own
request (see ``review_tools_mcp.read_acceptance_evidence``). Nothing populated
that store, so every review carried a "ticket acceptance criteria were
unavailable" verification gap. This module finds the ticket a pull request
claims to implement and fetches it through a narrow read-only helper.

Every failure here is survivable: a review without acceptance evidence is
exactly the review Boris produced before, so callers skip rather than fail.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

JIRA_RO = "/Users/ctao/.hermes/scripts/boris_intake/jira_ro.py"
FETCH_TIMEOUT_SECONDS = 45
MAX_KEYS = 2

_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,7})\b")
_BRACKETED = re.compile(r"\[([A-Z][A-Z0-9]{1,9}-\d{1,7})\]")
# Tokens that look exactly like an issue key but never are.
_NOT_PROJECTS = frozenset({
    "UTF", "SHA", "HTTP", "HTTPS", "RFC", "ISO", "AWS", "GPT", "IPV",
    "CVE", "SSL", "TLS", "API", "CI", "CD", "UI", "UX", "P", "V",
})


def ticket_keys(title: str, head_ref: str = "") -> tuple[str, ...]:
    """Issue keys this pull request claims to implement, best guess first.

    Every pull request reviewed so far names its ticket twice -- bracketed in
    the title and again in the branch -- so the bracketed form is trusted
    first and the branch is the fallback.
    """
    ordered: list[str] = []

    def add(candidate: str) -> None:
        key = candidate.upper()
        if key.split("-")[0] in _NOT_PROJECTS:
            return
        if key not in ordered:
            ordered.append(key)

    for match in _BRACKETED.finditer(title or ""):
        add(match.group(1))
    for source in (head_ref or "", title or ""):
        for match in _KEY.finditer(source):
            add(f"{match.group(1)}-{match.group(2)}")
    return tuple(ordered[:MAX_KEYS])


@dataclass(frozen=True)
class JiraAcceptanceAdapter:
    """Fetches ticket evidence out of process so it can never wedge intake."""

    helper: str = JIRA_RO
    python: str = "/Users/ctao/.hermes/hermes-agent/venv/bin/python"
    timeout: int = FETCH_TIMEOUT_SECONDS
    runner = staticmethod(subprocess.run)

    def fetch(self, title: str, head_ref: str) -> tuple[tuple[str, dict], ...]:
        """Return ``(reference, evidence)`` pairs; empty on any failure."""
        found: list[tuple[str, dict]] = []
        for key in ticket_keys(title, head_ref):
            evidence = self._fetch_one(key)
            if evidence is not None:
                found.append((key, evidence))
        return tuple(found)

    def _fetch_one(self, key: str) -> dict | None:
        try:
            proc = type(self).runner(
                [self.python, self.helper, key],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except Exception:
            return None
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        try:
            evidence = json.loads(proc.stdout)
        except ValueError:
            return None
        if not isinstance(evidence, dict) or not evidence.get("content"):
            return None
        return evidence


def helper_is_installed(path: str = JIRA_RO) -> bool:
    return Path(path).is_file()
