#!/usr/bin/env python3
"""pre_tool_call hook: deny-by-default terminal allowlist for Slack-invoked lanes.

Under spec D6 the GitHub credential provides no restriction, so this hook is the
only boundary. Design rules, in order of importance:

1.  Deny by default. Two argv[0] values are permitted; everything else blocked.
2.  Refuse rather than interpret. Any shell metacharacter, or any command shlex
    cannot parse, is blocked outright — the guard never tries to reason about
    what a compound command would do.
3.  Compare resolved absolute paths, never substrings, so `gh` under another
    name or an absolute path cannot slip past a name check.
4.  Restrict ONLY lanes positively identified as Slack-invoked. An unidentifiable
    lane is left alone so normal reviewer lanes are never broken; the daemon's
    startup self-test is the fail-closed counterweight that refuses `live`
    unless this guard provably fires.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path

GHX_RO = Path("/Users/ctao/.hermes/scripts/pr/ghx_ro.sh")
WRITER = Path("/Users/ctao/.hermes/scripts/boris_intake/write_review_body.py")
KANBAN_DB = "/Users/ctao/.hermes/kanban.db"
INTAKE_CREATED_BY = "boris-slack-intake"

#: Shell-active outside quotes only. Inside any quoting they are inert literals,
#: and review bodies legitimately contain all of them (markdown quote lines,
#: tables, code, parens) — so scanning the RAW string would block every real
#: review. Quote state is tracked instead.
UNQUOTED_METACHARS = frozenset(";|&><()\n\r")

#: Shell-active outside quotes AND inside double quotes. `$` covers both `$(...)`
#: command substitution and bare `$VAR` expansion — the latter matters because
#: `--text "$GH_TOKEN"` would otherwise interpolate a secret into a PR comment.
ALWAYS_ACTIVE = frozenset("$`")


def _shell_active_metachar(raw: str) -> str | None:
    """Return the first metacharacter a shell would act on, or None.

    Walks the string tracking quote state. Single quotes make everything
    literal; double quotes keep `$` and backtick live; unquoted text keeps
    everything live. A trailing backslash or unbalanced quote is left for
    shlex.split to reject.
    """
    quote = None  # None | "'" | '"'
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote is None and ch == "\\":
            i += 2
            continue
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if quote is None and ch in "\"'":
            quote = ch
        elif quote is not None and ch == quote:
            quote = None
        elif quote != "'":
            if ch in ALWAYS_ACTIVE:
                return ch
            if quote is None and ch in UNQUOTED_METACHARS:
                return ch
        i += 1
    return None


def _resolve(candidate: str) -> Path:
    return Path(os.path.expanduser(candidate)).resolve()


def decide(command: str, workspace: str) -> str | None:
    """Return a block reason, or None to allow."""
    raw = (command or "").strip()
    if not raw:
        return "empty command"
    active = _shell_active_metachar(raw)
    if active is not None:
        return (f"shell metacharacter {active!r} is not permitted outside quotes "
                "(single-quote literal text such as a review body)")
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        return f"command could not be parsed ({exc})"
    if not argv:
        return "empty command"

    try:
        prog = _resolve(argv[0])
    except (OSError, RuntimeError):
        return "argv[0] could not be resolved"

    if prog == GHX_RO.resolve():
        return None

    if prog == WRITER.resolve():
        out = None
        for i, a in enumerate(argv):
            if a == "--out" and i + 1 < len(argv):
                out = argv[i + 1]
            elif a.startswith("--out="):
                out = a.split("=", 1)[1]
        if not out:
            return "write_review_body requires --out"
        if not workspace:
            return "no run workspace is set"
        try:
            ws = Path(workspace).resolve()
            target = _resolve(out)
        except (OSError, RuntimeError):
            return "output path could not be resolved"
        if not str(target).startswith(str(ws) + os.sep):
            return f"write target {target} is outside the run workspace"
        return None

    return (f"only {GHX_RO} and {WRITER} may be run from a Slack-invoked "
            f"review lane (got {prog})")


def is_slack_lane(payload: dict, db_path: str = KANBAN_DB) -> bool:
    """True only when this lane is positively identified as Slack-invoked."""
    if os.environ.get("BORIS_REVIEW_MODE") == "slack-invoked":
        return True
    task_id = ((payload or {}).get("extra") or {}).get("task_id") or ""
    if not task_id:
        return False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT created_by FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == INTAKE_CREATED_BY


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        print("{}")
        return 0

    if payload.get("tool_name") not in {"terminal", "shell", "bash"}:
        print("{}")
        return 0
    if not is_slack_lane(payload):
        print("{}")
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    workspace = os.environ.get("PR_REVIEW_RUN_WORKSPACE", "")
    reason = decide(command, workspace)
    if reason:
        print(json.dumps({
            "decision": "block",
            "reason": f"boris-slack-intake terminal guard: {reason}",
        }))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
