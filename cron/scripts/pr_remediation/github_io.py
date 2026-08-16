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

# The two review bots whose unresolved threads make a "ready" notice
# overclaim. Fixed, like `RemediationConfig`'s other identity constants
# (chad_github_login, hold_label) -- not something an operator tunes per
# repo, so it lives beside the code that reads it rather than in config.py.
REVIEW_BOT_LOGINS = frozenset({"chatgpt-codex-connector", "github-advanced-security"})

# Thread resolution has no REST/`gh pr view --json` equivalent -- it is a
# GraphQL-only field on `PullRequestReviewThread` -- so this is the one read
# in this adapter that goes through `gh api graphql` instead of `_view`'s
# `gh pr view`. `comments(first: 1)` is deliberate, not a pagination
# shortcut taken for convenience: a thread's `path` is only exposed here as a
# property of its comments (there is no thread-level `path` this query asks
# for), and the first comment is the one that opened the thread and is what
# "authored by a review bot" means below -- a human reply further down the
# same thread must not launder a bot-opened thread out of this check, nor
# manufacture one a bot never opened.
_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          isOutdated
          comments(first: 1) {
            nodes {
              path
              author { login }
            }
          }
        }
      }
    }
  }
}
"""


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

    def unresolved_bot_threads(self, owner: str, repo: str,
                              pr: int) -> list[dict] | None:
        """Unresolved review threads opened by Codex or CodeQL, or None when
        that could not be determined.

        Unlike `has_hold`, None here is deliberately NOT folded into either
        answer a caller could act on -- there is no safe substitute for "we
        don't know how many bot threads are still open" the way `True`
        substitutes for "assume held". A transient GraphQL error must never
        collapse into an empty list: that would read as a verified-clean PR,
        recreating exactly the false all-clear this method exists to prevent.
        The caller (`SlackIO.notify_ready`) is where that ambiguity gets
        resolved -- None becomes its own sentence in the ready notice ("the
        check could not be made"), never a silent clean bill of health, and
        never a reason to hold the loop open or escalate it either: the loop
        still closes ready, it just says less than it thinks it knows.

        Every node is shape-checked before anything from it is trusted, the
        same discipline `has_hold` and `author` use, and for the same
        reason: a raise here would land inside `_finish_ready`, past the
        point the verdict already routed to READY, so it must never happen.
        Unlike `has_hold`, an unrecognised shape does not fail toward a safe
        default per-thread -- there is none -- it fails the whole read (this
        method returns None), rather than silently dropping the one
        unresolved thread that happened to be malformed while reporting the
        rest as if the picture were complete.
        """
        out = self._run([
            "api", "graphql",
            "-f", f"query={_REVIEW_THREADS_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"repo={repo}",
            "-F", f"number={pr}",
        ])
        if out is None or not out.strip():
            return None
        try:
            payload = json.loads(out)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            pull_request = payload["data"]["repository"]["pullRequest"]
            nodes = pull_request["reviewThreads"]["nodes"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list):
            return None

        threads: list[dict] = []
        for node in nodes:
            if not isinstance(node, dict):
                return None
            resolved = node.get("isResolved")
            if not isinstance(resolved, bool):
                return None
            if resolved:
                continue
            outdated = node.get("isOutdated")
            if not isinstance(outdated, bool):
                return None
            comments = node.get("comments")
            if not isinstance(comments, dict):
                return None
            comment_nodes = comments.get("nodes")
            if not isinstance(comment_nodes, list) or not comment_nodes:
                return None
            opener = comment_nodes[0]
            if not isinstance(opener, dict):
                return None
            author = opener.get("author")
            if not isinstance(author, dict):
                return None
            login = author.get("login")
            if not isinstance(login, str) or not login:
                return None
            path = opener.get("path")
            if not isinstance(path, str) or not path:
                return None
            if login not in REVIEW_BOT_LOGINS:
                continue
            threads.append({"path": path, "outdated": outdated})
        return threads

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
