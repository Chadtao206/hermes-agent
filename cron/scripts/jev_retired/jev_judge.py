"""Shadow-mode Jev (TypeSafe) judgment of a reviewer run, logged next to the watchdog's own verdict.

Enabled only when JEV_SHADOW=1. Never raises. This module itself decides nothing -- it logs one
JSON line to SHADOW_LOG per classified reviewer run and returns the judgment. Whether that
judgment moves a lane is the caller's choice, gated separately behind JEV_ENFORCE; see
_jev_enforces_block in just_do_it_review_reaction_watchdog.py for the one place it may.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

MODEL = "jev-1.13.0"  # pinned: thresholds are compared against this version's answers
TIMEOUT_SECONDS = 10.0
MAX_STATE_CHARS = 8000
SHADOW_LOG = Path(os.environ.get("HERMES_HOME") or "/Users/ctao/.hermes") / "state" / "jev_shadow_review_reactions.jsonl"

ENV_PATH = SHADOW_LOG.parent.parent / ".env"

_QUESTIONS: dict[str, Any] | None = None


def _env(name: str) -> str | None:
    """Process env first, then the deployed .env (last match wins): the cron runner does not
    always export .env into no-agent scripts (multiplex gateway skips the process-global load)."""
    if name in os.environ:
        return os.environ[name]
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    value = None
    for line in lines:
        if line.startswith(name + "="):
            value = line.split("=", 1)[1].strip().strip('"\'')
    return value


def enabled() -> bool:
    return _env("JEV_SHADOW") == "1" and bool(_env("TYPESAFE_API_KEY"))


def trim(value: Any, depth: int = 0) -> Any:
    """Cap strings and lists so the state stays small; structure is otherwise unchanged."""
    if isinstance(value, dict):
        return {k: trim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [trim(v, depth + 1) for v in value[:20]]
    if isinstance(value, str):
        return value[:600]
    return value


def questions() -> dict[str, Any]:
    global _QUESTIONS
    if _QUESTIONS is None:
        from typesafe_sdk import Choice, Noul

        _QUESTIONS = {
            "review_state": Choice(
                instructions="A code reviewer bot finished reviewing a pull request and wrote this structured "
                             "report. What is the reviewer's overall verdict on the pull request?",
                criteria={
                    "blocked": "The reviewer says the PR is not ready, not safe, needs fixes or changes, or lists "
                               "at least one current unresolved blocking finding. Resolved, prior, or historical "
                               "blockers do not count. A review the reviewer itself labels non-blocking or "
                               "comment-only is not blocked.",
                    "notes": "The reviewer did not block the PR but delivered non-blocking comments, findings, "
                             "nits, suggestions, concerns, or follow-ups worth reading.",
                    "approved": "The reviewer approved the PR or submitted an approval review, with no current "
                                "blockers and no substantive findings.",
                    "none": "The report states no verdict about the PR: it is empty, only bookkeeping such as "
                            "PR state, commit ids, or flags set to false, or has empty finding lists and no "
                            "approval. Absence of approval alone is not a block.",
                },
            ),
            "has_current_blocker": Noul(
                instructions="Does the report list at least one CURRENT, unresolved finding the reviewer treats "
                             "as blocking merge? Ignore findings marked resolved, fixed, prior, historical, or "
                             "explicitly non-blocking."),
            "approval_submitted": Noul(
                instructions="Does the report say an approval review was submitted or already exists on GitHub "
                             "for the reviewed commit?"),
            "has_nonblocking_findings": Noul(
                instructions="Does the report contain substantive non-blocking comments, findings, nits, or "
                             "suggestions (not merely empty lists or 'none')?"),
        }
    return _QUESTIONS


def judge(metadata: dict[str, Any]) -> dict[str, Any]:
    from typesafe_sdk import TypeSafeClient

    state = trim(metadata)
    if len(json.dumps(state)) > MAX_STATE_CHARS:
        return {"skipped": "state too large"}
    started = time.time()
    resp = TypeSafeClient(api_key=_env("TYPESAFE_API_KEY"), model=MODEL, timeout=TIMEOUT_SECONDS).system_one(
        state=state, questions=questions())
    a = resp.answers
    return {
        "state": a["review_state"].choice,
        "confidence": round(a["review_state"].confidence, 3),
        "probs": {k: round(v, 3) for k, v in a["review_state"].probabilities.items()},
        "blocker": round(a["has_current_blocker"].noul, 3),
        "approval": round(a["approval_submitted"].noul, 3),
        "notes": round(a["has_nonblocking_findings"].noul, 3),
        "tokens": resp.usage.input_tokens,
        "model": resp.model,
        "ms": int((time.time() - started) * 1000),
    }


def shadow(task_id: str, run_row: dict[str, Any] | None, wd_state: str | None,
           wd_why: str) -> dict[str, Any] | None:
    """Append one comparison line and hand the judgment back; never raise.

    The return value is what lets a caller act on the answer without paying for a second call.
    None means no judgment was made, which every failure path also returns, so a caller that
    treats None as "Jev said nothing" degrades to the pre-Jev behaviour on its own.
    """
    if not enabled() or not isinstance(run_row, dict) or not isinstance(run_row.get("metadata"), dict):
        return None
    line = {"ts": int(time.time()), "task_id": task_id, "run_id": run_row.get("id"),
            "wd": {"state": wd_state, "why": wd_why}}
    try:
        line["jev"] = judge(run_row["metadata"])
    except Exception as exc:
        line["jev"] = {"error": repr(exc)[:300]}
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError:
        pass
    return line["jev"] if "error" not in line["jev"] and "skipped" not in line["jev"] else None
