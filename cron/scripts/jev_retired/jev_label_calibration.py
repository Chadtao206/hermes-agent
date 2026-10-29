"""Score Jev against the human calibration labels, not against the watchdog's own regexes.

The shadow report measures agreement with verdict-text markers, which is only good news if
those markers are right. This scores the one corpus with human labels on it: the 59 baseline
cases under state/boris_calibration, classified true_blocker / contract_only / gate_only /
not_substantiated when rubric 2.1 was set.

Run:  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/jev_label_calibration.py [--refresh]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_judge

HOME = Path("/Users/ctao/.hermes")
BASELINE = HOME / "state" / "boris_calibration" / "baseline"
CACHE = HOME / "state" / "jev_label_calibration.json"
MODEL = jev_judge.MODEL
MAX_ISSUES = 4


def state_for(judgment: dict) -> dict:
    """Only the parts a reviewer would read to make this call; the rest is bookkeeping."""
    issues = [
        {k: v for k, v in issue.items()
         if k in ("title", "changed_mechanism", "consequence", "expected_behavior", "impact_scope")}
        for issue in (judgment.get("blocking_issues") or [])[:MAX_ISSUES]
    ]
    return jev_judge.trim({"summary": judgment.get("summary"), "blocking_issues": issues})


def questions() -> dict:
    from typesafe_sdk import Choice, Noul

    return {
        "finding_kind": Choice(
            instructions="A code reviewer blocked a pull request over the finding(s) below. Under "
                         "this team's rubric a finding only blocks merge when it describes how the "
                         "built system will actually misbehave. What kind of finding is this?",
            criteria={
                "true_blocker": "At least one finding describes a defect in real behaviour: wrong "
                                "results, data loss, a crash, a race, a security hole, or required "
                                "functionality that is absent from the implementation.",
                "contract_only": "The findings only describe a mismatch, gap, ambiguity, or "
                                 "contradiction between written specifications, schemas, contracts, "
                                 "or design documents. The described system would still behave "
                                 "correctly; what is wrong is the agreement about it.",
                "not_substantiated": "The finding asserts a problem the report does not support "
                                     "with a concrete mechanism or evidence.",
            },
        ),
        "genuine_defect": Noul(
            instructions="Does at least one finding describe the built system actually misbehaving "
                         "at runtime -- wrong output, data loss, failure, or missing required "
                         "functionality -- rather than only a disagreement between written "
                         "specifications or contracts?"),
        # Polarity-flipped control: if Jev only looks yes-biased, asking for the other class
        # should separate just as well. It does not -- both sit at +0.11 -- so the signal is absent.
        "spec_only": Noul(
            instructions="Is this finding ONLY about written artifacts disagreeing -- a "
                         "specification, schema, API contract, or design document that is "
                         "incomplete, ambiguous, or inconsistent with another document -- with no "
                         "claim that running code produces a wrong result, loses data, crashes, or "
                         "is missing implemented behaviour?"),
        "needs_author_decision": Noul(
            instructions="Would resolving this require a product or design decision by the author, "
                         "rather than a mechanical fix?"),
    }


def judge(state: dict) -> dict:
    from typesafe_sdk import TypeSafeClient

    resp = TypeSafeClient(api_key=jev_judge._env("TYPESAFE_API_KEY"), model=MODEL,
                          timeout=20.0).system_one(state=state, questions=questions())
    a = resp.answers
    return {
        "kind": a["finding_kind"].choice,
        "confidence": round(a["finding_kind"].confidence, 3),
        "probs": {k: round(v, 3) for k, v in a["finding_kind"].probabilities.items()},
        "defect": round(a["genuine_defect"].noul, 3),
        "spec_only": round(a["spec_only"].noul, 3),
        "author_decision": round(a["needs_author_decision"].noul, 3),
        "tokens": resp.usage.input_tokens,
    }


def collect(refresh: bool) -> list:
    cached = json.loads(CACHE.read_text()) if CACHE.exists() and not refresh else {}
    rows = []
    for path in sorted(BASELINE.glob("*.json")):
        case = json.loads(path.read_text())
        truth = case["classification"].replace("baseline:", "")
        judgment = case.get("judgment") or {}
        row = {"case_id": case["case_id"], "truth": truth}
        # Code answers this one: gate_only is exactly "the reviewer filed no blocking issue".
        if not (judgment.get("blocking_issues") or []):
            row["jev"] = {"kind": "gate_only", "by": "code"}
        elif case["case_id"] in cached:
            row["jev"] = cached[case["case_id"]]
        else:
            try:
                row["jev"] = judge(state_for(judgment))
            except Exception as exc:
                row["jev"] = {"error": repr(exc)[:200]}
            cached[case["case_id"]] = row["jev"]
        rows.append(row)
    CACHE.write_text(json.dumps(cached, indent=1, sort_keys=True))
    return rows


def report(rows: list) -> None:
    asked = [r for r in rows if r["jev"].get("by") != "code" and "error" not in r["jev"]]
    errors = [r for r in rows if "error" in r["jev"]]
    print(f"human-labelled corpus: {len(rows)} cases, {len(asked)} put to Jev, "
          f"{len(rows) - len(asked) - len(errors)} answered by code, {len(errors)} errors")
    for r in errors[:3]:
        print(f"    error {r['case_id']}: {r['jev']['error'][:120]}")

    exact = sum(1 for r in rows if r["jev"].get("kind") == r["truth"])
    print(f"\nexact label agreement: {exact}/{len(rows)} = {exact / len(rows):.1%}")

    print("\nconfusion (human -> jev):")
    for (t, j), n in Counter((r["truth"], r["jev"].get("kind", "?")) for r in rows).most_common():
        print(f"  {t:>18} -> {j:<18} {n}{'' if t == j else '   <- disagree'}")

    # The decision the rubric actually makes: does this block the merge or not?
    binary = [r for r in rows if "defect" in r["jev"]]
    if binary:
        print("\nBLOCKING DECISION (true_blocker vs everything else), swept over the defect probability:")
        print(f"  {'threshold':>10} {'precision':>10} {'recall':>8} {'accuracy':>9}")
        for t in (0.3, 0.5, 0.7, 0.9):
            pred = [r for r in binary if r["jev"]["defect"] >= t]
            tp = sum(1 for r in pred if r["truth"] == "true_blocker")
            actual = sum(1 for r in binary if r["truth"] == "true_blocker")
            acc = sum(1 for r in binary
                      if (r["jev"]["defect"] >= t) == (r["truth"] == "true_blocker")) / len(binary)
            prec = tp / len(pred) if pred else 0.0
            print(f"  {t:>10.2f} {prec:>9.1%} {tp / actual:>8.1%} {acc:>9.1%}")

    sep = [r for r in rows if "spec_only" in r["jev"] and r["truth"] in ("true_blocker", "contract_only")]
    if sep:
        print("\nSEPARATION (can Jev tell contract_only from true_blocker at all?)")
        for key, positive in (("defect", "true_blocker"), ("spec_only", "contract_only")):
            pos = [r["jev"][key] for r in sep if r["truth"] == positive]
            neg = [r["jev"][key] for r in sep if r["truth"] != positive]
            print(f"  {key:>10}: mean {sum(pos) / len(pos):.2f} on {positive}, "
                  f"{sum(neg) / len(neg):.2f} on the other -> separation {sum(pos) / len(pos) - sum(neg) / len(neg):+.2f}")
        print("  Both polarities land near +0.1, so the wording is not what is failing.")

    toks = [r["jev"]["tokens"] for r in asked if r["jev"].get("tokens")]
    if toks:
        print(f"\ncost: {sum(toks)} input tokens over {len(toks)} calls "
              f"(~${sum(toks) / 1e6 * 0.042:.4f} at $0.042/M)")


if __name__ == "__main__":
    report(collect("--refresh" in sys.argv))
