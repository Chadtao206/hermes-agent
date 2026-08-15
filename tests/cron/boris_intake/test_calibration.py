import json
from dataclasses import replace
from pathlib import Path

import pytest

from boris_intake.calibration import (
    CaseRun,
    CorpusCase,
    CorpusManifest,
    gate_passes,
    load_manifest,
    main,
    render_report,
    score_runs,
)


READY = "READY_FOR_HUMAN_REVIEW"
ISSUES = "ISSUES_FOUND"


def make_manifest(
    *, nonblocking=20, material_defects=20, baseline_false_blockers=8
):
    cases = []
    for number in range(nonblocking):
        case_id = f"nonblocking-{number:02d}"
        cases.append(CorpusCase(
            case_id=case_id,
            repository="Ylopo/example",
            head_sha=f"{number + 1:040x}",
            evidence_bundle=f"evidence/{case_id}.json",
            label_id=f"label-{case_id}",
            case_type="nonblocking",
            expected_outcome=READY,
            expected_classification=f"ready:{case_id}",
            baseline_result_refs=(f"baseline/{case_id}.json",),
            holdout=True,
            sentinel=False,
            baseline_false_blocker=number < baseline_false_blockers,
            synthetic=False,
        ))
    for number in range(material_defects):
        case_id = f"defect-{number:02d}"
        cases.append(CorpusCase(
            case_id=case_id,
            repository="Ylopo/example",
            head_sha=f"{nonblocking + number + 1:040x}",
            evidence_bundle=f"evidence/{case_id}.json",
            label_id=f"label-{case_id}",
            case_type="material_defect",
            expected_outcome=ISSUES,
            expected_classification=f"blocking:{case_id}",
            baseline_result_refs=(f"baseline/{case_id}.json",),
            holdout=True,
            sentinel=False,
            baseline_false_blocker=False,
            synthetic=False,
        ))
    return CorpusManifest(schema_version=1, cases=tuple(cases))


def make_runs(manifest):
    runs = []
    for case in manifest.cases:
        for rubric in ("current", "candidate"):
            outcome = case.expected_outcome
            classification = case.expected_classification
            if rubric == "current" and case.baseline_false_blocker:
                outcome = ISSUES
                classification = f"false-blocker:{case.case_id}"
            for run_number in (1, 2, 3):
                runs.append(CaseRun(
                    case_id=case.case_id,
                    rubric=rubric,
                    run_number=run_number,
                    outcome=outcome,
                    classification=classification,
                    schema_valid=True,
                ))
    return tuple(runs)


def replace_case_runs(runs, case_id, rubric, replacements):
    kept = [
        run for run in runs
        if not (run.case_id == case_id and run.rubric == rubric)
    ]
    return tuple([*kept, *replacements])


def runs_for(case, rubric, results):
    return tuple(
        CaseRun(
            case_id=case.case_id,
            rubric=rubric,
            run_number=number,
            outcome=outcome,
            classification=classification,
            schema_valid=schema_valid,
        )
        for number, (outcome, classification, schema_valid) in enumerate(
            results, start=1
        )
    )


def failure_codes(report):
    return {failure.code for failure in report.failures}


SENTINELS = Path(__file__).with_name("fixtures") / "calibration" / "sentinels.json"


def test_load_manifest_preserves_pinned_evidence_and_blinded_label(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "cases": [{
            "case_id": "clean-01",
            "evidence_bundle": {
                "repository": "Ylopo/example",
                "head_sha": "a" * 40,
                "path": "evidence/clean-01.json",
            },
            "adjudication": {
                "label_id": "blinded-017",
                "case_type": "nonblocking",
                "expected_outcome": READY,
                "expected_classification": "ready:clean",
            },
            "baseline_result_refs": ["baseline/clean-01.json"],
            "holdout": True,
            "sentinel": False,
            "baseline_false_blocker": False,
            "synthetic": False,
        }],
    }))

    manifest = load_manifest(path)

    assert manifest.schema_version == 1
    assert manifest.cases == (CorpusCase(
        case_id="clean-01",
        repository="Ylopo/example",
        head_sha="a" * 40,
        evidence_bundle="evidence/clean-01.json",
        label_id="blinded-017",
        case_type="nonblocking",
        expected_outcome=READY,
        expected_classification="ready:clean",
        baseline_result_refs=("baseline/clean-01.json",),
        holdout=True,
        sentinel=False,
        baseline_false_blocker=False,
        synthetic=False,
    ),)


def test_versioned_sentinels_are_synthetic_and_cannot_stand_in_for_the_corpus():
    manifest = load_manifest(SENTINELS)

    report = score_runs(manifest, ())

    assert all(case.synthetic and case.sentinel for case in manifest.cases)
    assert any(case.case_type == "gate_only" for case in manifest.cases)
    assert "INSUFFICIENT_NONBLOCKING_HOLDOUT" in failure_codes(report)
    assert "INSUFFICIENT_MATERIAL_DEFECT_HOLDOUT" in failure_codes(report)
    assert gate_passes(report) is False


def test_score_cli_exits_nonzero_for_a_failed_gate(tmp_path, capsys):
    assert main([
        "score",
        "--manifest", str(SENTINELS),
        "--runs", str(tmp_path),
    ]) == 1
    assert "Review Quality Gate: FAIL" in capsys.readouterr().out


def test_score_cli_reports_an_unavailable_runs_directory_as_input_error(
    tmp_path, capsys
):
    missing = tmp_path / "missing-runs"

    assert main([
        "score",
        "--manifest", str(SENTINELS),
        "--runs", str(missing),
    ]) == 2
    assert "calibration input error" in capsys.readouterr().err


def test_gate_requires_full_holdout_denominators():
    manifest = make_manifest(
        nonblocking=19,
        material_defects=19,
        baseline_false_blockers=7,
    )

    report = score_runs(manifest, make_runs(manifest))

    assert {
        "INSUFFICIENT_NONBLOCKING_HOLDOUT",
        "INSUFFICIENT_BASELINE_FALSE_BLOCKERS",
        "INSUFFICIENT_MATERIAL_DEFECT_HOLDOUT",
    } <= failure_codes(report)
    assert gate_passes(report) is False


def test_synthetic_cases_do_not_satisfy_historical_holdout_denominators():
    manifest = make_manifest()
    manifest = replace(
        manifest,
        cases=tuple(replace(case, synthetic=True) for case in manifest.cases),
    )

    report = score_runs(manifest, make_runs(manifest))

    assert {
        "INSUFFICIENT_NONBLOCKING_HOLDOUT",
        "INSUFFICIENT_BASELINE_FALSE_BLOCKERS",
        "INSUFFICIENT_MATERIAL_DEFECT_HOLDOUT",
    } <= failure_codes(report)
    assert gate_passes(report) is False


def test_synthetic_holdout_cannot_create_historical_false_blocker_reduction():
    manifest = make_manifest()
    synthetic_case = replace(
        manifest.cases[0],
        case_id="synthetic-improvement",
        head_sha="e" * 40,
        evidence_bundle="synthetic://improvement",
        label_id="synthetic-improvement-label",
        baseline_result_refs=("synthetic://baseline/improvement",),
        synthetic=True,
    )
    manifest = replace(manifest, cases=(*manifest.cases, synthetic_case))
    runs = []
    cases = {case.case_id: case for case in manifest.cases}
    for run in make_runs(manifest):
        case = cases[run.case_id]
        if (
            run.rubric == "candidate"
            and case.baseline_false_blocker
            and not case.synthetic
        ):
            run = replace(
                run,
                outcome=ISSUES,
                classification=f"false-blocker:{case.case_id}",
            )
        runs.append(run)

    report = score_runs(manifest, tuple(runs))

    assert report.current_false_blockers == 8
    assert report.candidate_false_blockers == 8
    assert "FALSE_BLOCKERS_NOT_REDUCED" in failure_codes(report)
    assert gate_passes(report) is False


def test_declared_baseline_false_blocker_matches_current_run_majority():
    manifest = make_manifest()
    case = manifest.cases[0]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "current",
        runs_for(case, "current", [
            (READY, case.expected_classification, True),
            (READY, case.expected_classification, True),
            (READY, case.expected_classification, True),
        ]),
    )

    report = score_runs(manifest, runs)

    assert "BASELINE_FALSE_BLOCKER_MISMATCH" in failure_codes(report)
    assert gate_passes(report) is False


def test_each_rubric_requires_exactly_three_numbered_runs_per_case():
    manifest = make_manifest()
    runs = tuple(
        run for run in make_runs(manifest)
        if not (
            run.case_id == "nonblocking-09"
            and run.rubric == "candidate"
            and run.run_number == 3
        )
    )

    report = score_runs(manifest, runs)

    assert "RUN_COUNT" in failure_codes(report)
    assert gate_passes(report) is False


@pytest.mark.parametrize("rubric", ["current", "candidate"])
def test_each_sentinel_rubric_requires_three_of_three_correct_runs(rubric):
    manifest = make_manifest()
    sentinel = replace(manifest.cases[9], sentinel=True)
    manifest = replace(
        manifest,
        cases=(sentinel, *manifest.cases[:9], *manifest.cases[10:]),
    )
    runs = make_runs(manifest)
    runs = replace_case_runs(runs, sentinel.case_id, rubric, runs_for(
        sentinel,
        rubric,
        [
            (READY, sentinel.expected_classification, True),
            (READY, sentinel.expected_classification, True),
            (ISSUES, "false-blocker:sentinel", True),
        ],
    ))

    report = score_runs(manifest, runs)

    assert "SENTINEL_3_OF_3" in failure_codes(report)
    assert gate_passes(report) is False


def test_gate_only_holdout_cases_are_scored_outside_code_judgment_denominators():
    manifest = make_manifest()
    gate_case = CorpusCase(
        case_id="gate-only-00",
        repository="Ylopo/example",
        head_sha="f" * 40,
        evidence_bundle="evidence/gate-only-00.json",
        label_id="label-gate-only-00",
        case_type="gate_only",
        expected_outcome=ISSUES,
        expected_classification="gate_only:required-ci",
        baseline_result_refs=("baseline/gate-only-00.json",),
        holdout=True,
        sentinel=False,
        baseline_false_blocker=False,
        synthetic=True,
    )
    manifest = replace(manifest, cases=(*manifest.cases, gate_case))

    report = score_runs(manifest, make_runs(manifest))

    assert report.gate_only_holdout_cases == 1
    assert report.current_false_blockers == 8
    assert report.candidate_false_blockers == 0
    assert gate_passes(report) is True


def test_holdout_uses_two_of_three_majority_result():
    manifest = make_manifest()
    case = manifest.cases[9]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "candidate",
        runs_for(case, "candidate", [
            (READY, case.expected_classification, True),
            (ISSUES, "false-blocker:one-run", True),
            (READY, case.expected_classification, True),
        ]),
    )

    report = score_runs(manifest, runs)
    score = next(score for score in report.case_scores if score.case_id == case.case_id)

    assert score.candidate_majority == (
        case.expected_outcome,
        case.expected_classification,
    )
    assert score.candidate_unstable is False
    assert gate_passes(report) is True


def test_three_way_inconsistent_holdout_case_fails():
    manifest = make_manifest()
    case = manifest.cases[9]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "candidate",
        runs_for(case, "candidate", [
            (READY, case.expected_classification, True),
            (ISSUES, "blocking:unrelated", True),
            ("REVIEW_INCOMPLETE", "gap:unknown", True),
        ]),
    )

    report = score_runs(manifest, runs)

    assert "THREE_WAY_INCONSISTENT" in failure_codes(report)
    assert gate_passes(report) is False


def test_more_than_ten_percent_unstable_holdout_cases_fails():
    manifest = make_manifest()
    runs = make_runs(manifest)
    for case in manifest.cases[:5]:
        runs = replace_case_runs(
            runs,
            case.case_id,
            "candidate",
            runs_for(case, "candidate", [
                (READY, f"ready:a:{case.case_id}", True),
                (ISSUES, f"blocking:b:{case.case_id}", True),
                ("REVIEW_INCOMPLETE", f"gap:c:{case.case_id}", True),
            ]),
        )

    report = score_runs(manifest, runs)

    assert report.unstable_holdout_rate == 0.125
    assert "UNSTABLE_HOLDOUT_RATE" in failure_codes(report)
    assert gate_passes(report) is False


def test_candidate_must_strictly_reduce_false_blockers():
    manifest = make_manifest()
    unchanged_runs = []
    for run in make_runs(manifest):
        case = next(case for case in manifest.cases if case.case_id == run.case_id)
        if run.rubric == "candidate" and case.baseline_false_blocker:
            run = replace(
                run,
                outcome=ISSUES,
                classification=f"false-blocker:{case.case_id}",
            )
        unchanged_runs.append(run)

    report = score_runs(manifest, tuple(unchanged_runs))

    assert report.current_false_blockers == 8
    assert report.candidate_false_blockers == 8
    assert "FALSE_BLOCKERS_NOT_REDUCED" in failure_codes(report)
    assert gate_passes(report) is False


def test_any_new_false_blocker_fails_even_when_total_is_reduced():
    manifest = make_manifest()
    case = manifest.cases[9]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "candidate",
        runs_for(case, "candidate", [
            (ISSUES, "false-blocker:new", True),
            (ISSUES, "false-blocker:new", True),
            (ISSUES, "false-blocker:new", True),
        ]),
    )

    report = score_runs(manifest, runs)

    assert report.current_false_blockers == 8
    assert report.candidate_false_blockers == 1
    assert "NEW_FALSE_BLOCKER" in failure_codes(report)
    assert gate_passes(report) is False


def test_candidate_cannot_miss_a_material_defect_caught_by_baseline():
    manifest = make_manifest()
    case = manifest.cases[20]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "candidate",
        runs_for(case, "candidate", [
            (READY, "ready:missed-defect", True),
            (READY, "ready:missed-defect", True),
            (READY, "ready:missed-defect", True),
        ]),
    )

    report = score_runs(manifest, runs)

    assert "MISSED_BASELINE_DEFECT" in failure_codes(report)
    assert gate_passes(report) is False


def test_any_holdout_classification_regression_fails():
    manifest = make_manifest()
    case = manifest.cases[9]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "candidate",
        runs_for(case, "candidate", [
            ("REVIEW_INCOMPLETE", "gap:invented", True),
            ("REVIEW_INCOMPLETE", "gap:invented", True),
            ("REVIEW_INCOMPLETE", "gap:invented", True),
        ]),
    )

    report = score_runs(manifest, runs)

    assert "HOLDOUT_REGRESSION" in failure_codes(report)
    assert gate_passes(report) is False


def test_schema_invalid_run_fails_the_gate():
    manifest = make_manifest()
    case = manifest.cases[9]
    replacements = list(runs_for(case, "candidate", [
        (READY, case.expected_classification, True),
        (READY, case.expected_classification, True),
        (READY, case.expected_classification, True),
    ]))
    replacements[1] = replace(replacements[1], schema_valid=False)
    runs = replace_case_runs(
        make_runs(manifest), case.case_id, "candidate", replacements
    )

    report = score_runs(manifest, runs)

    assert "SCHEMA_INVALID" in failure_codes(report)
    assert gate_passes(report) is False


def test_bootstrap_interval_is_diagnostic_and_cannot_override_case_regression():
    manifest = make_manifest(baseline_false_blockers=15)
    case = manifest.cases[16]
    runs = replace_case_runs(
        make_runs(manifest),
        case.case_id,
        "candidate",
        runs_for(case, "candidate", [
            (ISSUES, "false-blocker:new", True),
            (ISSUES, "false-blocker:new", True),
            (ISSUES, "false-blocker:new", True),
        ]),
    )

    report = score_runs(manifest, runs)
    false_blocker_interval = next(
        interval for interval in report.diagnostics
        if interval.metric == "false_blocker_reduction"
    )

    assert false_blocker_interval.lower > 0
    assert "NEW_FALSE_BLOCKER" in failure_codes(report)
    assert gate_passes(report) is False


def test_passing_report_is_rendered_with_counts_and_diagnostics():
    manifest = make_manifest()

    report = score_runs(manifest, make_runs(manifest))
    rendered = render_report(report)

    assert gate_passes(report) is True
    assert "PASS" in rendered
    assert "false blockers: current=8 candidate=0" in rendered
    assert "false_blocker_reduction" in rendered
