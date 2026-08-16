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
        except Exception:
            # Any failure to obtain a usable token (missing file, permission
            # error, non-UTF-8 bytes, etc.) must fail closed by returning
            # None here rather than raising. Callers like has_hold() rely on
            # None propagating up as "unreadable" -> "assume hold", so this
            # must never escape as an exception.
            return None
        if not token:
            # An empty (or whitespace-only, after strip()) token file is a
            # locally-detectable unusable-token case. Short-circuit here
            # instead of handing "" to gh: it avoids a pointless network
            # call and does not make the fail-closed guarantee depend on
            # gh's behavior toward an empty GH_TOKEN.
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
            # `state` is not optional here: without it this adapter cannot tell
            # an open pull request from one that was merged or closed between a
            # verdict and the tick that acts on it, and the loop goes on to
            # label it, dispatch an engineer lane at "this head" and post a
            # re-review against a branch nobody can push to any more.
            "--json", "labels,headRefOid,author,state",
        ])
        if out is None:
            return None
        if not out.strip():
            # Empty stdout is nothing to parse, not an implicit "{}". Treat
            # it the same as an unreadable response rather than manufacturing
            # a fake empty object that downstream code would read as a known
            # (label-free) state.
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def head_sha(self, owner: str, repo: str, pr: int) -> str | None:
        data = self._view(owner, repo, pr)
        return (data or {}).get("headRefOid") or None

    def author(self, owner: str, repo: str, pr: int) -> str | None:
        """The PR's author login, or None when GitHub's answer cannot be
        trusted to name one.

        Shape-checked exactly like `has_hold` below, and for the same reason.
        The chained-`.get` form this replaced -- `(data.get("author") or {})
        .get("login")` -- raises AttributeError the moment `author` is a scalar
        rather than a dict (a `"author": "Chadtao206"` payload, a truncated
        response, a schema change). That raise lands inside `tick`'s per-verdict
        handler *after* the verdict has already been marked seen, so the verdict
        is spent, the loop is left in `observed`, and nothing routes it.

        None is a safe answer here where an exception is not: the driver reads
        it as "unknown author" and records a `author_unreadable` observation,
        which is exactly what a transient GitHub is meant to produce.
        """
        data = self._view(owner, repo, pr)
        if not isinstance(data, dict):
            return None
        author = data.get("author")
        if not isinstance(author, dict):
            return None
        login = author.get("login")
        return login if isinstance(login, str) and login else None

    def state(self, owner: str, repo: str, pr: int) -> str | None:
        """`OPEN`, `MERGED` or `CLOSED` -- upper-cased, as `gh` reports it --
        or None when GitHub's answer cannot be trusted to name one.

        Shape-checked like `author` and `has_hold`, and None means *unknown*,
        never "not open". The driver only writes a loop off as stale on a state
        it positively read, because guessing the other way would abandon a live
        pull request on a network blip -- and a loop driven terminal is one
        `active_for_pr` will never let a later generation recover.
        """
        data = self._view(owner, repo, pr)
        if not isinstance(data, dict):
            return None
        value = data.get("state")
        return value.upper() if isinstance(value, str) and value else None

    def has_hold(self, owner: str, repo: str, pr: int) -> bool:
        """True also when GitHub could not be read, or the response's shape
        cannot be trusted to mean "no hold": an unknown state is a hold.

        Only a `labels` value that is *exactly* a list of dicts, each with a
        string `name`, is a recognised shape. Anything else -- missing key,
        null, a scalar, a list of non-dicts, or a dict missing `name` -- is
        unrecognised and must fail open (return True), the same as an
        unreadable response. A `labels: []` list is a genuinely known,
        label-free state and must still return False.
        """
        data = self._view(owner, repo, pr)
        if data is None:
            return True
        labels = data.get("labels")
        if not isinstance(labels, list):
            return True
        names = set()
        for label in labels:
            if not isinstance(label, dict):
                return True
            name = label.get("name")
            if not isinstance(name, str):
                return True
            names.add(name)
        return self._cfg.hold_label in names

    def claim(self, owner: str, repo: str, pr: int) -> bool:
        """Add the ownership label. Returns True only if the write
        succeeded; callers must treat False as "do not proceed as owner"."""
        return self._run(["pr", "edit", str(pr), "-R", f"{owner}/{repo}",
                           "--add-label", self._cfg.owner_label]) is not None

    def release(self, owner: str, repo: str, pr: int) -> bool:
        """Remove the ownership label. Returns True only if the write
        succeeded."""
        return self._run(["pr", "edit", str(pr), "-R", f"{owner}/{repo}",
                           "--remove-label", self._cfg.owner_label]) is not None
