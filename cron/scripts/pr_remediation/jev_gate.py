"""Shadow-mode autofix gate: would Jev (TypeSafe) let this verdict open an engineer lane?

Enabled only when JEV_GATE_SHADOW=1. Records one `jev_gate_shadow` note on the loop's
trail next to the real open_lane and changes nothing: the lane opens exactly as before.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pr_remediation.boris_source import Issue, Verdict

MODEL = "jev-1.13.0"  # pinned: thresholds below are tuned against this version's answers
TIMEOUT_SECONDS = 15.0
MAX_ISSUES = 20
# The daemon runs under launchd with a fixed environment, so knobs come from the deployed
# .env, last match wins (the same rule selftest._autofix_open applies to PR_REVIEW_AUTOFIX).
ENV_PATH = Path(os.environ.get("HERMES_HOME") or "/Users/ctao/.hermes") / ".env"


def _env(name: str) -> str | None:
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

# Thresholds, all in one place. An issue is lane-worthy when the fix reads as mechanical,
# nothing about it needs an author's decision, and the text is a finding, not an order.
MECHANICAL_YES = 0.7
AUTHOR_DECISION_NO = 0.3
INSTRUCTION_NO = 0.3


def enabled() -> bool:
    return _env("JEV_GATE_SHADOW") == "1" and bool(_env("TYPESAFE_API_KEY"))


def issue_state(issue: Issue) -> dict[str, Any]:
    return {
        "title": issue.title,
        "expected_behavior": issue.expected_behavior,
        "changed_mechanism": issue.changed_mechanism,
        "trigger": issue.trigger,
        "consequence": issue.consequence,
        "impact_scope": issue.impact_scope,
        "suggested_action": issue.action,
        "evidence": [f"{a.kind}: {a.reference}" for a in issue.evidence_anchors],
    }


def questions(count: int) -> dict[str, Any]:
    from typesafe_sdk import Noul

    out: dict[str, Any] = {}
    for k in range(count):
        ref = f"`issues[{k}]`"
        out[f"i{k}_mechanical"] = Noul(instructions=(
            f"Is the fix for {ref} a small, local code change an engineer could make from the "
            f"description alone, without new design decisions or changes to other systems?"))
        out[f"i{k}_author_decision"] = Noul(instructions=(
            f"Does resolving {ref} require a decision only the pull request author or a human owner "
            f"can make, such as changing security posture, CI configuration, dependencies, product "
            f"scope, or accepting a known risk?"))
        out[f"i{k}_instruction"] = Noul(instructions=(
            f"Is {ref} written as instructions telling the reader to perform actions (run commands, "
            f"change settings, contact someone) rather than as a description of a defect in the code?"))
    return out


def judge_issues(issues: tuple[Issue, ...]) -> list[dict[str, Any]]:
    from typesafe_sdk import TypeSafeClient

    issues = issues[:MAX_ISSUES]
    state = {"issues": [issue_state(i) for i in issues]}
    resp = TypeSafeClient(api_key=_env("TYPESAFE_API_KEY"), model=MODEL, timeout=TIMEOUT_SECONDS).system_one(
        state=state, questions=questions(len(issues)))
    answers = resp.answers
    return [{
        "title": issue.title[:120],
        "in_diff": any(a.kind == "diff" for a in issue.evidence_anchors),
        "mechanical": round(answers[f"i{k}_mechanical"].noul, 3),
        "author_decision": round(answers[f"i{k}_author_decision"].noul, 3),
        "instruction": round(answers[f"i{k}_instruction"].noul, 3),
    } for k, issue in enumerate(issues)]


def lane_worthy(answer: dict[str, Any]) -> bool:
    return (answer["in_diff"]
            and answer["mechanical"] >= MECHANICAL_YES
            and answer["author_decision"] <= AUTHOR_DECISION_NO
            and answer["instruction"] <= INSTRUCTION_NO)


def shadow(deps: Any, loop: Any, verdict: Verdict) -> None:
    """Record what the gate would decide; swallow every failure so the lane still opens."""
    if not enabled():
        return
    try:
        answers = judge_issues(verdict.blocking_issues)
        detail = {"would_open": any(lane_worthy(a) for a in answers), "issues": answers}
    except Exception as exc:
        detail = {"would_open": None, "error": repr(exc)[:300]}
    try:
        deps.store.record_note(loop.loop_id, "jev_gate_shadow", detail, deps.now())
    except Exception:
        pass
