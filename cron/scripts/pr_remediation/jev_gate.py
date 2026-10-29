"""Shadow-mode autofix gate: would Jev (TypeSafe) let this verdict open an engineer lane?

Enabled only when JEV_GATE_SHADOW=1. Records one `jev_gate_shadow` note on the loop's
trail wherever a tick routes to open_lane -- after the real lane opens on a live tick, and
on a rehearsing (shadow-mode) tick too -- and changes nothing: the decision is never read.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from pr_remediation.boris_source import Issue, Verdict

MODEL = "jev-1.14.0"  # pinned (typesafe-sdk 0.7.5): thresholds below are judged against this version
TIMEOUT_SECONDS = 10.0  # the SDK's per-HTTP-operation timeout
# A wall-clock bound on the whole judgment, connect and read included: the tick waits no
# longer than this, and a call still running past it is abandoned to its daemon thread.
HARD_TIMEOUT_SECONDS = 15.0
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
# MECHANICAL_YES is uncalibrated; JEV_GATE_MECHANICAL_YES overrides it until the report's
# outcome-by-band table says where it belongs.
MECHANICAL_YES = 0.7
AUTHOR_DECISION_NO = 0.3
INSTRUCTION_NO = 0.3


def mechanical_yes() -> float:
    try:
        return float(_env("JEV_GATE_MECHANICAL_YES") or MECHANICAL_YES)
    except ValueError:
        return MECHANICAL_YES


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


def input_sha256(issues: tuple[Issue, ...]) -> str:
    state = {"issues": [issue_state(i) for i in issues[:MAX_ISSUES]]}
    text = json.dumps(state, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def judge_issues(issues: tuple[Issue, ...], meta: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One API call, never retried. `meta`, when given, receives the served model and usage."""
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    issues = issues[:MAX_ISSUES]
    state = {"issues": [issue_state(i) for i in issues]}
    client = TypeSafeClient(api_key=_env("TYPESAFE_API_KEY"), model=MODEL,
                            timeout=TIMEOUT_SECONDS, retry=RetryPolicy(max_retries=0))
    resp = client.system_one(state=state, questions=questions(len(issues)))
    if meta is not None:
        usage = getattr(resp, "usage", None)
        meta["model"] = getattr(resp, "model", None) or MODEL
        meta["usage"] = {"input_tokens": getattr(usage, "input_tokens", None),
                         "output_tokens": getattr(usage, "output_tokens", None)}
    answers = resp.answers
    return [{
        "title": issue.title[:120],
        "in_diff": any(a.kind == "diff" for a in issue.evidence_anchors),
        "mechanical": round(answers[f"i{k}_mechanical"].noul, 3),
        "author_decision": round(answers[f"i{k}_author_decision"].noul, 3),
        "instruction": round(answers[f"i{k}_instruction"].noul, 3),
    } for k, issue in enumerate(issues)]


def lane_worthy(answer: dict[str, Any], mechanical_min: float | None = None) -> bool:
    return (answer["in_diff"]
            and answer["mechanical"] >= (mechanical_yes() if mechanical_min is None else mechanical_min)
            and answer["author_decision"] <= AUTHOR_DECISION_NO
            and answer["instruction"] <= INSTRUCTION_NO)


def _judge_bounded(issues: tuple[Issue, ...], meta: dict[str, Any]) -> list[dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["answers"] = judge_issues(issues, meta)
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, name="jev-gate-shadow", daemon=True)
    worker.start()
    worker.join(HARD_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise TimeoutError(f"jev gate exceeded {HARD_TIMEOUT_SECONDS:g}s")
    if "error" in box:
        raise box["error"]
    return box["answers"]


def shadow(deps: Any, loop: Any, verdict: Verdict) -> None:
    """Record what the gate would decide; swallow every failure so the tick goes on."""
    if not enabled():
        return
    try:
        issues = verdict.blocking_issues
        threshold = mechanical_yes()
        detail: dict[str, Any] = {"model": MODEL, "head_sha": verdict.head_sha,
                                  "input_sha256": input_sha256(issues), "mechanical_yes": threshold}
        if not issues:
            detail.update(would_open=None, skipped="no_issues")
        else:
            meta: dict[str, Any] = {}
            started = time.monotonic()
            try:
                answers = _judge_bounded(issues, meta)
                detail.update(would_open=any(lane_worthy(a, threshold) for a in answers),
                              issues=answers)
            except Exception as exc:
                detail.update(would_open=None, error=repr(exc)[:300])
            detail["latency_ms"] = round((time.monotonic() - started) * 1000)
            detail.update(meta)
        deps.store.record_note(loop.loop_id, "jev_gate_shadow", detail, deps.now())
    except Exception:
        pass
