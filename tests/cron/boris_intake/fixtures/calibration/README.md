# Boris calibration fixture format

`sentinels.json` is a non-sensitive, synthetic manifest fragment for schema and
category examples. It is not the adjudicated corpus and cannot satisfy the Review
Quality Gate denominators. Proprietary diffs, acceptance contracts, review results,
human labels, and holdout membership belong under
`/Users/ctao/.hermes/state/boris_calibration/` and must not be committed.

Historical cases require two independent adjudicators blinded to rubric identity.
A third adjudicator resolves disagreement before the label is frozen. Freeze
holdout membership before candidate runs. Synthetic cases may supplement boundary
coverage but cannot replace the required historical holdout.

## Manifest

The manifest is one JSON object:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "case_id": "stable-case-id",
      "evidence_bundle": {
        "repository": "Ylopo/repository",
        "head_sha": "40-lowercase-hex",
        "path": "evidence/stable-case-id.json"
      },
      "adjudication": {
        "label_id": "blinded-label-id",
        "case_type": "nonblocking|material_defect|gate_only",
        "expected_outcome": "READY_FOR_HUMAN_REVIEW|ISSUES_FOUND|REVIEW_INCOMPLETE",
        "expected_classification": "human-adjudicated canonical classification"
      },
      "baseline_result_refs": ["baseline/stable-case-id-run-1.json"],
      "holdout": true,
      "sentinel": false,
      "baseline_false_blocker": false,
      "synthetic": false
    }
  ]
}
```

`evidence_bundle.path` identifies the pinned diff, metadata, acceptance contract,
CI/security evidence, access boundary, and repository documentation visible for the
case. `baseline_result_refs` identifies preserved current-rubric results.
`expected_classification` is assigned by the blinded human adjudication protocol; it
may encode category placement and root-cause identity without requiring exact prose.
Only a `nonblocking` case may set `baseline_false_blocker`. `gate_only` cases
exercise code-owned Gate Snapshot classification and terminal derivation; the
scorer reports them separately from code-judgment denominators.

The restricted holdout must contain at least 20 non-synthetic nonblocking cases,
including at least eight adjudicated baseline false blockers, and at least 20
non-synthetic confirmed material defects. `sentinel` cases are evaluated on all
three current and candidate runs. A real manifest should use `synthetic: false`
for historical cases.

## Stored runs

The runs directory contains JSON objects, or arrays of objects, with this shape:

```json
{
  "case_id": "stable-case-id",
  "rubric": "current|candidate",
  "run_number": 1,
  "outcome": "READY_FOR_HUMAN_REVIEW|ISSUES_FOUND|REVIEW_INCOMPLETE",
  "classification": "human-adjudicated canonical classification",
  "schema_valid": true
}
```

Store exactly three independently produced runs for each rubric and case. The
sealed calibration workflow captures the structured result with publication and
delivery disabled. Deterministic validation supplies `schema_valid`; blinded human
adjudicators map semantic claims, category placement, and root-cause identity to
`classification`. The scorer never asks a model to reinterpret prose.

Run the gate with:

```bash
python cron/scripts/boris_intake/calibration.py score \
  --manifest /Users/ctao/.hermes/state/boris_calibration/manifest.json \
  --runs /Users/ctao/.hermes/state/boris_calibration/runs
```

Exit status `0` means every required case-level rule passed. Status `1` means a
quality-gate failure; status `2` means malformed or unavailable input. Paired
bootstrap intervals in the report are diagnostic and never override a failure.
