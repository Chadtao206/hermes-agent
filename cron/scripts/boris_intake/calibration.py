#!/usr/bin/env python3
"""Deterministic scoring for the Boris review calibration corpus."""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


_OUTCOMES = {
    "ISSUES_FOUND",
    "READY_FOR_HUMAN_REVIEW",
    "REVIEW_INCOMPLETE",
}
_RUBRICS = ("current", "candidate")


@dataclass(frozen=True)
class CorpusCase:
    case_id: str
    repository: str
    head_sha: str
    evidence_bundle: str
    label_id: str
    case_type: str
    expected_outcome: str
    expected_classification: str
    baseline_result_refs: tuple[str, ...]
    holdout: bool
    sentinel: bool
    baseline_false_blocker: bool
    synthetic: bool


@dataclass(frozen=True)
class CorpusManifest:
    schema_version: int
    cases: tuple[CorpusCase, ...]


@dataclass(frozen=True)
class CaseRun:
    case_id: str
    rubric: str
    run_number: int
    outcome: str
    classification: str
    schema_valid: bool


@dataclass(frozen=True)
class GateFailure:
    code: str
    case_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    current_majority: tuple[str, str] | None
    candidate_majority: tuple[str, str] | None
    current_unstable: bool
    candidate_unstable: bool


@dataclass(frozen=True)
class DiagnosticInterval:
    metric: str
    estimate: float
    lower: float
    upper: float


@dataclass(frozen=True)
class GateReport:
    case_scores: tuple[CaseScore, ...]
    failures: tuple[GateFailure, ...]
    diagnostics: tuple[DiagnosticInterval, ...]
    holdout_cases: int
    gate_only_holdout_cases: int
    current_false_blockers: int
    candidate_false_blockers: int
    unstable_holdout_cases: int
    unstable_holdout_rate: float


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _parse_case(raw: object) -> CorpusCase:
    if not isinstance(raw, dict):
        raise ValueError("each corpus case must be an object")
    evidence = raw.get("evidence_bundle")
    adjudication = raw.get("adjudication")
    if not isinstance(evidence, dict):
        raise ValueError("evidence_bundle must be an object")
    if not isinstance(adjudication, dict):
        raise ValueError("adjudication must be an object")

    case_type = _string(adjudication.get("case_type"), "case_type")
    if case_type not in {"nonblocking", "material_defect", "gate_only"}:
        raise ValueError(f"unsupported case_type: {case_type}")
    outcome = _string(adjudication.get("expected_outcome"), "expected_outcome")
    if outcome not in _OUTCOMES:
        raise ValueError(f"unsupported expected_outcome: {outcome}")
    head_sha = _string(evidence.get("head_sha"), "head_sha")
    if len(head_sha) != 40 or any(c not in "0123456789abcdef" for c in head_sha):
        raise ValueError("head_sha must be 40 lowercase hexadecimal characters")
    references = raw.get("baseline_result_refs")
    if not isinstance(references, list) or not references:
        raise ValueError("baseline_result_refs must be a non-empty list")
    baseline_result_refs = tuple(
        _string(reference, "baseline_result_refs") for reference in references
    )
    baseline_false_blocker = _boolean(
        raw.get("baseline_false_blocker"), "baseline_false_blocker"
    )
    if baseline_false_blocker and case_type != "nonblocking":
        raise ValueError("only nonblocking cases can be baseline false blockers")

    return CorpusCase(
        case_id=_string(raw.get("case_id"), "case_id"),
        repository=_string(evidence.get("repository"), "repository"),
        head_sha=head_sha,
        evidence_bundle=_string(evidence.get("path"), "evidence_bundle.path"),
        label_id=_string(adjudication.get("label_id"), "label_id"),
        case_type=case_type,
        expected_outcome=outcome,
        expected_classification=_string(
            adjudication.get("expected_classification"),
            "expected_classification",
        ),
        baseline_result_refs=baseline_result_refs,
        holdout=_boolean(raw.get("holdout"), "holdout"),
        sentinel=_boolean(raw.get("sentinel"), "sentinel"),
        baseline_false_blocker=baseline_false_blocker,
        synthetic=_boolean(raw.get("synthetic"), "synthetic"),
    )


def load_manifest(path: Path) -> CorpusManifest:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("manifest cases must be a non-empty list")
    cases = tuple(_parse_case(case) for case in raw_cases)
    case_ids = [case.case_id for case in cases]
    label_ids = [case.label_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("manifest case_id values must be unique")
    if len(label_ids) != len(set(label_ids)):
        raise ValueError("manifest label_id values must be unique")
    return CorpusManifest(schema_version=1, cases=cases)


def _majority(
    runs: Sequence[CaseRun],
) -> tuple[tuple[str, str] | None, bool]:
    if len(runs) != 3 or {run.run_number for run in runs} != {1, 2, 3}:
        return None, False
    signatures = [(run.outcome, run.classification) for run in runs]
    counts = Counter(signatures)
    if len(counts) == 3:
        return None, True
    return counts.most_common(1)[0][0], False


def _interval(metric: str, values: Sequence[int]) -> DiagnosticInterval:
    if not values:
        return DiagnosticInterval(metric, 0.0, 0.0, 0.0)
    estimate = sum(values) / len(values)
    rng = random.Random(0)
    samples = sorted(
        sum(values[rng.randrange(len(values))] for _ in values) / len(values)
        for _ in range(2000)
    )
    return DiagnosticInterval(
        metric=metric,
        estimate=estimate,
        lower=samples[int(0.025 * (len(samples) - 1))],
        upper=samples[int(0.975 * (len(samples) - 1))],
    )


def score_runs(
    manifest: CorpusManifest,
    runs: Sequence[CaseRun],
) -> GateReport:
    failures: list[GateFailure] = []
    cases_by_id = {case.case_id: case for case in manifest.cases}
    grouped: dict[tuple[str, str], list[CaseRun]] = {}

    for run in runs:
        if run.case_id not in cases_by_id:
            failures.append(GateFailure("UNKNOWN_CASE", run.case_id))
            continue
        if run.rubric not in _RUBRICS:
            failures.append(GateFailure("UNKNOWN_RUBRIC", run.case_id, run.rubric))
            continue
        grouped.setdefault((run.case_id, run.rubric), []).append(run)

    holdout = tuple(case for case in manifest.cases if case.holdout)
    historical_holdout = tuple(case for case in holdout if not case.synthetic)
    nonblocking = tuple(
        case for case in historical_holdout if case.case_type == "nonblocking"
    )
    material_defects = tuple(
        case for case in historical_holdout if case.case_type == "material_defect"
    )
    gate_only = tuple(case for case in holdout if case.case_type == "gate_only")
    labelled_false_blockers = tuple(
        case for case in nonblocking if case.baseline_false_blocker
    )
    if len(nonblocking) < 20:
        failures.append(GateFailure(
            "INSUFFICIENT_NONBLOCKING_HOLDOUT",
            detail=f"found {len(nonblocking)}, need 20",
        ))
    if len(labelled_false_blockers) < 8:
        failures.append(GateFailure(
            "INSUFFICIENT_BASELINE_FALSE_BLOCKERS",
            detail=f"found {len(labelled_false_blockers)}, need 8",
        ))
    if len(material_defects) < 20:
        failures.append(GateFailure(
            "INSUFFICIENT_MATERIAL_DEFECT_HOLDOUT",
            detail=f"found {len(material_defects)}, need 20",
        ))

    case_scores: list[CaseScore] = []
    current_false_blockers = 0
    candidate_false_blockers = 0
    unstable_holdout_ids: set[str] = set()
    false_blocker_pairs: list[int] = []
    defect_catch_pairs: list[int] = []

    for case in manifest.cases:
        rubrics: dict[str, tuple[tuple[str, str] | None, bool]] = {}
        expected = (case.expected_outcome, case.expected_classification)
        for rubric in _RUBRICS:
            case_runs = grouped.get((case.case_id, rubric), [])
            if len(case_runs) != 3 or {run.run_number for run in case_runs} != {1, 2, 3}:
                failures.append(GateFailure(
                    "RUN_COUNT",
                    case.case_id,
                    f"{rubric} runs must be numbered 1, 2, and 3 exactly once",
                ))
            if any(not run.schema_valid for run in case_runs):
                failures.append(GateFailure(
                    "SCHEMA_INVALID", case.case_id, f"{rubric} has an invalid run"
                ))
            rubrics[rubric] = _majority(case_runs)

        current, current_unstable = rubrics["current"]
        candidate, candidate_unstable = rubrics["candidate"]
        if current_unstable:
            failures.append(GateFailure(
                "THREE_WAY_INCONSISTENT", case.case_id, "current"
            ))
        if candidate_unstable:
            failures.append(GateFailure(
                "THREE_WAY_INCONSISTENT", case.case_id, "candidate"
            ))
        if case.holdout and (current_unstable or candidate_unstable):
            unstable_holdout_ids.add(case.case_id)

        if case.sentinel:
            for rubric in _RUBRICS:
                sentinel_runs = grouped.get((case.case_id, rubric), [])
                if (
                    len(sentinel_runs) != 3
                    or any(
                        not run.schema_valid
                        or (run.outcome, run.classification) != expected
                        for run in sentinel_runs
                    )
                ):
                    failures.append(GateFailure(
                        "SENTINEL_3_OF_3", case.case_id, rubric
                    ))

        case_scores.append(CaseScore(
            case_id=case.case_id,
            current_majority=current,
            candidate_majority=candidate,
            current_unstable=current_unstable,
            candidate_unstable=candidate_unstable,
        ))

        current_false_blocker = (
            case.case_type == "nonblocking"
            and current is not None
            and current[0] == "ISSUES_FOUND"
        )
        if (
            case.baseline_false_blocker
            and current is not None
            and not current_false_blocker
        ):
            failures.append(GateFailure(
                "BASELINE_FALSE_BLOCKER_MISMATCH", case.case_id
            ))

        if not case.holdout or current is None or candidate is None:
            continue
        candidate_false_blocker = (
            case.case_type == "nonblocking" and candidate[0] == "ISSUES_FOUND"
        )
        if not case.synthetic:
            current_false_blockers += int(current_false_blocker)
            candidate_false_blockers += int(candidate_false_blocker)

        if (
            not case.synthetic
            and candidate_false_blocker
            and not current_false_blocker
        ):
            failures.append(GateFailure("NEW_FALSE_BLOCKER", case.case_id))
        if current == expected and candidate != expected:
            failures.append(GateFailure("HOLDOUT_REGRESSION", case.case_id))
        if (
            not case.synthetic
            and case.case_type == "material_defect"
            and current == expected
            and candidate != expected
        ):
            failures.append(GateFailure("MISSED_BASELINE_DEFECT", case.case_id))

        if not case.synthetic and case.case_type == "nonblocking":
            false_blocker_pairs.append(
                int(current_false_blocker) - int(candidate_false_blocker)
            )
        if not case.synthetic and case.case_type == "material_defect":
            defect_catch_pairs.append(
                int(candidate == expected) - int(current == expected)
            )

    if candidate_false_blockers >= current_false_blockers:
        failures.append(GateFailure(
            "FALSE_BLOCKERS_NOT_REDUCED",
            detail=(
                f"current={current_false_blockers}, "
                f"candidate={candidate_false_blockers}"
            ),
        ))

    holdout_count = len(holdout)
    unstable_count = len(unstable_holdout_ids)
    unstable_rate = unstable_count / holdout_count if holdout_count else 0.0
    if unstable_rate > 0.10:
        failures.append(GateFailure(
            "UNSTABLE_HOLDOUT_RATE",
            detail=f"{unstable_count}/{holdout_count} ({unstable_rate:.1%})",
        ))

    return GateReport(
        case_scores=tuple(case_scores),
        failures=tuple(failures),
        diagnostics=(
            _interval("false_blocker_reduction", false_blocker_pairs),
            _interval("material_defect_catch_delta", defect_catch_pairs),
        ),
        holdout_cases=holdout_count,
        gate_only_holdout_cases=len(gate_only),
        current_false_blockers=current_false_blockers,
        candidate_false_blockers=candidate_false_blockers,
        unstable_holdout_cases=unstable_count,
        unstable_holdout_rate=unstable_rate,
    )


def gate_passes(report: GateReport) -> bool:
    return not report.failures


def render_report(report: GateReport) -> str:
    lines = [
        f"Review Quality Gate: {'PASS' if gate_passes(report) else 'FAIL'}",
        f"holdout cases: {report.holdout_cases}",
        f"gate-only holdout cases: {report.gate_only_holdout_cases}",
        (
            "false blockers: "
            f"current={report.current_false_blockers} "
            f"candidate={report.candidate_false_blockers}"
        ),
        (
            "unstable holdout: "
            f"{report.unstable_holdout_cases}/{report.holdout_cases} "
            f"({report.unstable_holdout_rate:.1%})"
        ),
        "diagnostic paired bootstrap intervals (95%):",
    ]
    lines.extend(
        f"  {interval.metric}: {interval.estimate:.3f} "
        f"[{interval.lower:.3f}, {interval.upper:.3f}]"
        for interval in report.diagnostics
    )
    if report.failures:
        lines.append("failures:")
        lines.extend(
            "  " + " ".join(part for part in (
                failure.code,
                failure.case_id or "",
                failure.detail,
            ) if part)
            for failure in report.failures
        )
    return "\n".join(lines)


def _load_runs(path: Path) -> tuple[CaseRun, ...]:
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"runs directory unavailable: {path}")
    runs = []
    for result_path in sorted(path.rglob("*.json")):
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        records = raw if isinstance(raw, list) else [raw]
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"run record in {result_path} must be an object")
            outcome = _string(record.get("outcome"), "outcome")
            if outcome not in _OUTCOMES:
                raise ValueError(f"unsupported run outcome: {outcome}")
            run_number = record.get("run_number")
            if not isinstance(run_number, int):
                raise ValueError("run_number must be an integer")
            runs.append(CaseRun(
                case_id=_string(record.get("case_id"), "case_id"),
                rubric=_string(record.get("rubric"), "rubric"),
                run_number=run_number,
                outcome=outcome,
                classification=_string(
                    record.get("classification"), "classification"
                ),
                schema_valid=_boolean(record.get("schema_valid"), "schema_valid"),
            ))
    return tuple(runs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    score = subparsers.add_parser("score")
    score.add_argument("--manifest", type=Path, required=True)
    score.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        runs = _load_runs(args.runs)
        report = score_runs(manifest, runs)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"calibration input error: {error}", file=sys.stderr)
        return 2
    print(render_report(report))
    return 0 if gate_passes(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
