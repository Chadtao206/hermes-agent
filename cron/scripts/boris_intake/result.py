from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from boris_intake.models import GateCheck, GateSnapshot, LifecycleRequest, ReviewPolicy


class ValidationError(ValueError):
    pass


_JUDGMENT_FIELDS = frozenset({
    "summary",
    "evidence_reviewed",
    "verification_limits",
    "blocking_issues",
    "non_blocking_issues",
    "risk_hypotheses",
    "verification_gaps",
    "release_conditions",
    "nits",
})
_ANCHOR_KINDS = frozenset({"diff", "test", "ci", "ticket", "log", "documentation"})
_RELEASE_PHASES = frozenset({
    "merge",
    "deployment",
    "migration",
    "rollout",
    "production_validation",
})
_PENDING_STATES = frozenset({"PENDING", "QUEUED", "IN_PROGRESS"})
_MAX_STRING = 2_000
_MAX_SUMMARY = 4_000
_MAX_ITEMS = 50


@dataclass(frozen=True)
class EvidenceAnchor:
    kind: str
    reference: str
    detail: str | None = None


@dataclass(frozen=True)
class Issue:
    title: str
    expected_behavior: str
    changed_mechanism: str
    trigger: str
    consequence: str
    impact_scope: str
    evidence_anchors: tuple[EvidenceAnchor, ...]
    action: str


@dataclass(frozen=True)
class RiskHypothesis:
    title: str
    observed_facts: str
    hypothesized_link: str
    unresolved_link: str
    evidence_anchors: tuple[EvidenceAnchor, ...]
    verification_step: str


@dataclass(frozen=True)
class VerificationGap:
    title: str
    unassessed_claim: str
    evidence_inspected: tuple[EvidenceAnchor, ...]
    missing_evidence: str
    prevents_core_assessment: bool
    next_step: str
    reason_code: str | None = None


@dataclass(frozen=True)
class ReleaseCondition:
    title: str
    requirement: str
    authoritative_source: str
    phase: str
    completion_criterion: str


@dataclass(frozen=True)
class Nit:
    title: str
    location: str
    suggestion: str | None = None


@dataclass(frozen=True)
class ModelJudgment:
    summary: str
    evidence_reviewed: tuple[EvidenceAnchor, ...]
    verification_limits: tuple[str, ...]
    blocking_issues: tuple[Issue, ...]
    non_blocking_issues: tuple[Issue, ...]
    risk_hypotheses: tuple[RiskHypothesis, ...]
    verification_gaps: tuple[VerificationGap, ...]
    release_conditions: tuple[ReleaseCondition, ...]
    nits: tuple[Nit, ...]


@dataclass(frozen=True)
class GateRegistry:
    version: int
    security_checks: frozenset[str]
    ordinary_checks: frozenset[str]

    def classify(self, check_name: str, required: bool) -> str:
        if check_name in self.security_checks:
            return "security"
        if check_name in self.ordinary_checks:
            return "ordinary"
        return "unclassified_required" if required else "unclassified"


@dataclass(frozen=True)
class GateCondition:
    kind: str
    check_name: str
    conclusion: str
    requirement: str
    url: str
    action: str


@dataclass(frozen=True)
class GateAssessment:
    blocking_conditions: tuple[GateCondition, ...]
    waived_conditions: tuple[GateCondition, ...]
    pending_checks: tuple[GateCheck, ...]


@dataclass(frozen=True)
class SystemFailure:
    reason_code: str
    detail: str
    retry_guidance: str


@dataclass(frozen=True)
class Incomplete:
    origin: str
    reason_codes: tuple[str, ...]
    detail: str
    retry_guidance: str


@dataclass(frozen=True)
class DerivedResult:
    outcome: str
    presentation: str


@dataclass(frozen=True)
class ReviewTarget:
    owner: str
    repo: str
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class StructuredResult:
    schema_version: int
    request_id: str
    target: ReviewTarget
    policy: ReviewPolicy
    summary: str
    evidence_reviewed: tuple[EvidenceAnchor, ...]
    verification_limits: tuple[str, ...]
    blocking_issues: tuple[Issue, ...]
    non_blocking_issues: tuple[Issue, ...]
    risk_hypotheses: tuple[RiskHypothesis, ...]
    verification_gaps: tuple[VerificationGap, ...]
    release_conditions: tuple[ReleaseCondition, ...]
    nits: tuple[Nit, ...]
    gate_snapshot: GateSnapshot
    blocking_gate_conditions: tuple[GateCondition, ...]
    waived_ci_conditions: tuple[GateCondition, ...]
    incomplete: Incomplete | None
    derived: DerivedResult
    assembled_at: str
    result_hash: str


def _mapping(
    value: object,
    path: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must be an object")
    required_fields = frozenset(required)
    allowed = required_fields | frozenset(optional)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"{path} has unknown field {unknown[0]}")
    missing = sorted(required_fields - set(value))
    if missing:
        raise ValidationError(f"{path} requires {missing[0]}")
    return value


def _string(value: object, path: str, limit: int = _MAX_STRING) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} must be a non-empty string")
    if len(value) > limit:
        raise ValidationError(f"{path} exceeds {limit} characters")
    return value.strip()


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{path} must be a boolean")
    return value


def _collection(value: object, path: str, *, require_one: bool = False) -> list[object]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    if require_one and not value:
        raise ValidationError(f"{path} must contain at least one item")
    if len(value) > _MAX_ITEMS:
        raise ValidationError(f"{path} exceeds {_MAX_ITEMS} items")
    return value


def _primitive(value: object) -> Any:
    if is_dataclass(value):
        return {
            field.name: _primitive(getattr(value, field.name))
            for field in fields(value)
            if getattr(value, field.name) is not None
        }
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def _canonical(value: object) -> str:
    return json.dumps(_primitive(value), separators=(",", ":"), sort_keys=True)


def _sorted(items: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(sorted(items, key=_canonical))


def _anchor(value: object, path: str) -> EvidenceAnchor:
    raw = _mapping(value, path, ("kind", "reference"), ("detail",))
    kind = _string(raw["kind"], f"{path}.kind", 32)
    if kind not in _ANCHOR_KINDS:
        raise ValidationError(f"{path}.kind is not supported")
    detail = raw.get("detail")
    return EvidenceAnchor(
        kind=kind,
        reference=_string(raw["reference"], f"{path}.reference"),
        detail=None if detail is None else _string(detail, f"{path}.detail"),
    )


def _anchors(value: object, path: str, *, require_one: bool = True) -> tuple[EvidenceAnchor, ...]:
    items = _collection(value, path, require_one=require_one)
    return _sorted(_anchor(item, f"{path}[{index}]") for index, item in enumerate(items))


def _issue(value: object, path: str) -> Issue:
    names = (
        "title",
        "expected_behavior",
        "changed_mechanism",
        "trigger",
        "consequence",
        "impact_scope",
        "evidence_anchors",
        "action",
    )
    raw = _mapping(value, path, names)
    return Issue(
        title=_string(raw["title"], f"{path}.title"),
        expected_behavior=_string(raw["expected_behavior"], f"{path}.expected_behavior"),
        changed_mechanism=_string(raw["changed_mechanism"], f"{path}.changed_mechanism"),
        trigger=_string(raw["trigger"], f"{path}.trigger"),
        consequence=_string(raw["consequence"], f"{path}.consequence"),
        impact_scope=_string(raw["impact_scope"], f"{path}.impact_scope"),
        evidence_anchors=_anchors(raw["evidence_anchors"], f"{path}.evidence_anchors"),
        action=_string(raw["action"], f"{path}.action"),
    )


def _risk(value: object, path: str) -> RiskHypothesis:
    names = (
        "title",
        "observed_facts",
        "hypothesized_link",
        "unresolved_link",
        "evidence_anchors",
        "verification_step",
    )
    raw = _mapping(value, path, names)
    return RiskHypothesis(
        title=_string(raw["title"], f"{path}.title"),
        observed_facts=_string(raw["observed_facts"], f"{path}.observed_facts"),
        hypothesized_link=_string(raw["hypothesized_link"], f"{path}.hypothesized_link"),
        unresolved_link=_string(raw["unresolved_link"], f"{path}.unresolved_link"),
        evidence_anchors=_anchors(raw["evidence_anchors"], f"{path}.evidence_anchors"),
        verification_step=_string(raw["verification_step"], f"{path}.verification_step"),
    )


def _gap(value: object, path: str) -> VerificationGap:
    names = (
        "title",
        "unassessed_claim",
        "evidence_inspected",
        "missing_evidence",
        "prevents_core_assessment",
        "next_step",
    )
    raw = _mapping(value, path, names, ("reason_code",))
    core = _boolean(raw["prevents_core_assessment"], f"{path}.prevents_core_assessment")
    reason = raw.get("reason_code")
    if core and reason is None:
        raise ValidationError(f"{path}.reason_code is required for a core assessment gap")
    if not core and reason is not None:
        raise ValidationError(f"{path}.reason_code is only valid for a core assessment gap")
    return VerificationGap(
        title=_string(raw["title"], f"{path}.title"),
        unassessed_claim=_string(raw["unassessed_claim"], f"{path}.unassessed_claim"),
        evidence_inspected=_anchors(raw["evidence_inspected"], f"{path}.evidence_inspected"),
        missing_evidence=_string(raw["missing_evidence"], f"{path}.missing_evidence"),
        prevents_core_assessment=core,
        next_step=_string(raw["next_step"], f"{path}.next_step"),
        reason_code=None if reason is None else _string(reason, f"{path}.reason_code", 128),
    )


def _release_condition(value: object, path: str) -> ReleaseCondition:
    names = (
        "title",
        "requirement",
        "authoritative_source",
        "phase",
        "completion_criterion",
    )
    raw = _mapping(value, path, names)
    phase = _string(raw["phase"], f"{path}.phase", 64)
    if phase not in _RELEASE_PHASES:
        raise ValidationError(f"{path}.phase is not supported")
    return ReleaseCondition(
        title=_string(raw["title"], f"{path}.title"),
        requirement=_string(raw["requirement"], f"{path}.requirement"),
        authoritative_source=_string(raw["authoritative_source"], f"{path}.authoritative_source"),
        phase=phase,
        completion_criterion=_string(raw["completion_criterion"], f"{path}.completion_criterion"),
    )


def _nit(value: object, path: str) -> Nit:
    raw = _mapping(value, path, ("title", "location"), ("suggestion",))
    suggestion = raw.get("suggestion")
    return Nit(
        title=_string(raw["title"], f"{path}.title"),
        location=_string(raw["location"], f"{path}.location"),
        suggestion=None if suggestion is None else _string(suggestion, f"{path}.suggestion"),
    )


def _typed_collection(value: object, path: str, parser) -> tuple[Any, ...]:
    items = _collection(value, path)
    return _sorted(parser(item, f"{path}[{index}]") for index, item in enumerate(items))


def validate_judgment(payload: object) -> ModelJudgment:
    raw = _mapping(payload, "judgment", _JUDGMENT_FIELDS)
    limits = tuple(
        sorted(
            _string(item, f"verification_limits[{index}]")
            for index, item in enumerate(_collection(raw["verification_limits"], "verification_limits"))
        )
    )
    judgment = ModelJudgment(
        summary=_string(raw["summary"], "summary", _MAX_SUMMARY),
        evidence_reviewed=_anchors(raw["evidence_reviewed"], "evidence_reviewed"),
        verification_limits=limits,
        blocking_issues=_typed_collection(raw["blocking_issues"], "blocking_issues", _issue),
        non_blocking_issues=_typed_collection(
            raw["non_blocking_issues"], "non_blocking_issues", _issue
        ),
        risk_hypotheses=_typed_collection(raw["risk_hypotheses"], "risk_hypotheses", _risk),
        verification_gaps=_typed_collection(raw["verification_gaps"], "verification_gaps", _gap),
        release_conditions=_typed_collection(
            raw["release_conditions"], "release_conditions", _release_condition
        ),
        nits=_typed_collection(raw["nits"], "nits", _nit),
    )
    seen: set[str] = set()
    for collection in (
        judgment.blocking_issues,
        judgment.non_blocking_issues,
        judgment.risk_hypotheses,
        judgment.verification_gaps,
        judgment.release_conditions,
        judgment.nits,
    ):
        for observation in collection:
            canonical = _canonical(observation)
            if canonical in seen:
                raise ValidationError("duplicate observation")
            seen.add(canonical)
    return judgment


def canonicalize_judgment(judgment: ModelJudgment) -> str:
    if not isinstance(judgment, ModelJudgment):
        raise ValidationError("judgment is not validated")
    return _canonical(judgment)


def load_gate_registry(path: Path) -> GateRegistry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValidationError(f"invalid gate registry: {error}") from error
    raw = _mapping(
        payload,
        "gate_registry",
        ("version", "security_checks", "ordinary_checks"),
    )
    version = raw["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValidationError("gate_registry.version must be a positive integer")

    def names(field: str) -> frozenset[str]:
        values = _collection(raw[field], f"gate_registry.{field}")
        parsed = frozenset(_string(item, f"gate_registry.{field}", 256) for item in values)
        if len(parsed) != len(values):
            raise ValidationError(f"gate_registry.{field} contains a duplicate")
        return parsed

    security = names("security_checks")
    ordinary = names("ordinary_checks")
    if security & ordinary:
        raise ValidationError("gate registry check classes overlap")
    return GateRegistry(version, security, ordinary)


def build_gate_snapshot(
    rows: Sequence[Mapping[str, object]],
    captured_at: str,
    registry: GateRegistry,
) -> GateSnapshot:
    _string(captured_at, "captured_at", 64)
    checks: list[GateCheck] = []
    for index, value in enumerate(rows):
        raw = _mapping(value, f"checks[{index}]", ("name", "state", "link"), ("required",))
        name = _string(raw["name"], f"checks[{index}].name", 256)
        state = _string(raw["state"], f"checks[{index}].state", 64).upper()
        required_value = raw.get("required", False)
        required = _boolean(required_value, f"checks[{index}].required")
        status = "pending" if state in _PENDING_STATES else "completed"
        checks.append(
            GateCheck(
                check_name=name,
                check_class=registry.classify(name, required),
                status=status,
                conclusion=state.lower(),
                url=_string(raw["link"], f"checks[{index}].link"),
                required=required,
            )
        )
    ordered = tuple(sorted(checks, key=lambda check: (check.check_name.casefold(), check.url)))
    fingerprint_payload = [check.to_dict() for check in ordered]
    digest = hashlib.sha256(
        json.dumps(fingerprint_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return GateSnapshot(f"sha256:{digest}", captured_at, ordered)


def apply_gate_policy(snapshot: GateSnapshot, policy: ReviewPolicy) -> GateAssessment:
    blocking: list[GateCondition] = []
    waived: list[GateCondition] = []
    pending: list[GateCheck] = []
    for check in snapshot.checks:
        applicable = check.required or check.check_class == "security"
        if not applicable:
            continue
        if check.status != "completed":
            pending.append(check)
            continue
        if check.conclusion == "success":
            continue
        kind = {
            "ordinary": "required_ci",
            "security": "security",
            "unclassified_required": "unclassified_required",
        }.get(check.check_class, "unclassified_required")
        condition = GateCondition(
            kind=kind,
            check_name=check.check_name,
            conclusion=check.conclusion,
            requirement=f"{check.check_name} must complete successfully.",
            url=check.url,
            action=f"Satisfy or rerun {check.check_name}.",
        )
        if kind == "required_ci" and policy.ignore_ci:
            waived.append(condition)
        else:
            blocking.append(condition)
    key = lambda condition: (condition.check_name.casefold(), condition.url)
    return GateAssessment(
        tuple(sorted(blocking, key=key)),
        tuple(sorted(waived, key=key)),
        tuple(sorted(pending, key=lambda check: (check.check_name.casefold(), check.url))),
    )


def _model_incomplete(gaps: tuple[VerificationGap, ...]) -> Incomplete | None:
    core = tuple(gap for gap in gaps if gap.prevents_core_assessment)
    if not core:
        return None
    reason_codes = tuple(sorted({gap.reason_code for gap in core if gap.reason_code}))
    details = tuple(
        sorted({f"{gap.unassessed_claim} Missing evidence: {gap.missing_evidence}" for gap in core})
    )
    guidance = tuple(sorted({gap.next_step for gap in core}))
    return Incomplete(
        origin="model",
        reason_codes=reason_codes,
        detail=" ".join(details)[:_MAX_SUMMARY],
        retry_guidance=" ".join(guidance)[:_MAX_SUMMARY],
    )


def _publishable_findings(result: StructuredResult) -> bool:
    """Observations worth publishing that are not the gaps causing incompleteness."""
    return any((
        result.non_blocking_issues,
        result.risk_hypotheses,
        result.release_conditions,
        result.nits,
        result.blocking_gate_conditions,
        tuple(gap for gap in result.verification_gaps if not gap.prevents_core_assessment),
    ))


def derive_terminal(result: StructuredResult) -> DerivedResult:
    if result.incomplete is not None and result.incomplete.origin == "system":
        return DerivedResult("REVIEW_INCOMPLETE", "INCOMPLETE")
    if result.blocking_issues:
        return DerivedResult("ISSUES_FOUND", "ISSUES")
    if result.incomplete is not None:
        if _publishable_findings(result):
            return DerivedResult("REVIEW_INCOMPLETE", "INCOMPLETE_WITH_FINDINGS")
        return DerivedResult("REVIEW_INCOMPLETE", "INCOMPLETE")
    if result.blocking_gate_conditions:
        return DerivedResult("ISSUES_FOUND", "GATE_BLOCKED")
    comments = any((
        result.non_blocking_issues,
        result.risk_hypotheses,
        result.verification_gaps,
        result.release_conditions,
        result.nits,
        result.waived_ci_conditions,
    ))
    presentation = "READY_WITH_COMMENTS" if comments else "READY_CLEAN"
    return DerivedResult("READY_FOR_HUMAN_REVIEW", presentation)


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return _string(value, "now", 64)


def _result_payload(result: StructuredResult, *, include_hash: bool) -> dict[str, Any]:
    payload = {
        "schema_version": result.schema_version,
        "request_id": result.request_id,
        "target": _primitive(result.target),
        "policy": _primitive(result.policy),
        "summary": result.summary,
        "evidence_reviewed": _primitive(result.evidence_reviewed),
        "verification_limits": _primitive(result.verification_limits),
        "blocking_issues": _primitive(result.blocking_issues),
        "non_blocking_issues": _primitive(result.non_blocking_issues),
        "risk_hypotheses": _primitive(result.risk_hypotheses),
        "verification_gaps": _primitive(result.verification_gaps),
        "release_conditions": _primitive(result.release_conditions),
        "nits": _primitive(result.nits),
        "gate_snapshot": {
            "fingerprint": result.gate_snapshot.fingerprint,
            "captured_at": result.gate_snapshot.captured_at,
            "checks": [check.to_dict() for check in result.gate_snapshot.checks],
        },
        "blocking_gate_conditions": _primitive(result.blocking_gate_conditions),
        "waived_ci_conditions": _primitive(result.waived_ci_conditions),
        "incomplete": None if result.incomplete is None else _primitive(result.incomplete),
        "derived": _primitive(result.derived),
        "assembled_at": result.assembled_at,
    }
    if include_hash:
        payload["result_hash"] = result.result_hash
    return payload


def canonicalize_result(result: StructuredResult) -> str:
    if not isinstance(result, StructuredResult):
        raise ValidationError("result is not structured")
    return json.dumps(_result_payload(result, include_hash=True), separators=(",", ":"), sort_keys=True)


def assemble_result(
    request: LifecycleRequest,
    judgment: ModelJudgment | None,
    snapshot: GateSnapshot,
    system_failure: SystemFailure | None,
    now: datetime | str,
) -> StructuredResult:
    if (judgment is None) == (system_failure is None):
        raise ValidationError("provide exactly one judgment or system_failure")
    assessment = apply_gate_policy(snapshot, request.policy)
    if judgment is not None and assessment.pending_checks:
        raise ValidationError("required checks are still pending")

    if system_failure is not None:
        failure = SystemFailure(
            reason_code=_string(system_failure.reason_code, "system_failure.reason_code", 128),
            detail=_string(system_failure.detail, "system_failure.detail", _MAX_SUMMARY),
            retry_guidance=_string(
                system_failure.retry_guidance,
                "system_failure.retry_guidance",
                _MAX_SUMMARY,
            ),
        )
        summary = failure.detail
        evidence_reviewed: tuple[EvidenceAnchor, ...] = ()
        verification_limits: tuple[str, ...] = ()
        blocking_issues: tuple[Issue, ...] = ()
        non_blocking_issues: tuple[Issue, ...] = ()
        risk_hypotheses: tuple[RiskHypothesis, ...] = ()
        verification_gaps: tuple[VerificationGap, ...] = ()
        release_conditions: tuple[ReleaseCondition, ...] = ()
        nits: tuple[Nit, ...] = ()
        incomplete = Incomplete(
            "system", (failure.reason_code,), failure.detail, failure.retry_guidance
        )
    else:
        if not isinstance(judgment, ModelJudgment):
            raise ValidationError("judgment is not validated")
        summary = judgment.summary
        evidence_reviewed = judgment.evidence_reviewed
        verification_limits = judgment.verification_limits
        blocking_issues = judgment.blocking_issues
        non_blocking_issues = judgment.non_blocking_issues
        risk_hypotheses = judgment.risk_hypotheses
        verification_gaps = judgment.verification_gaps
        release_conditions = judgment.release_conditions
        nits = judgment.nits
        incomplete = _model_incomplete(verification_gaps)

    provisional = StructuredResult(
        schema_version=2,
        request_id=request.request_id,
        target=ReviewTarget(request.owner, request.repo, request.pr_number, request.head_sha),
        policy=request.policy,
        summary=summary,
        evidence_reviewed=evidence_reviewed,
        verification_limits=verification_limits,
        blocking_issues=blocking_issues,
        non_blocking_issues=non_blocking_issues,
        risk_hypotheses=risk_hypotheses,
        verification_gaps=verification_gaps,
        release_conditions=release_conditions,
        nits=nits,
        gate_snapshot=snapshot,
        blocking_gate_conditions=assessment.blocking_conditions,
        waived_ci_conditions=assessment.waived_conditions,
        incomplete=incomplete,
        derived=DerivedResult("", ""),
        assembled_at=_timestamp(now),
        result_hash="",
    )
    derived = derive_terminal(provisional)
    provisional = replace(provisional, derived=derived)
    digest = hashlib.sha256(
        json.dumps(
            _result_payload(provisional, include_hash=False),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return replace(provisional, result_hash=f"sha256:{digest}")


def _anchors_text(anchors: tuple[EvidenceAnchor, ...]) -> str:
    return "; ".join(
        f"{anchor.kind}: {anchor.reference}"
        + (f" ({anchor.detail})" if anchor.detail else "")
        for anchor in anchors
    )


def _render_issues(title: str, observations: tuple[Issue, ...]) -> list[str]:
    if not observations:
        return []
    lines = [f"## {title}"]
    for item in observations:
        lines.extend((
            f"### {item.title}",
            f"- Expected behavior: {item.expected_behavior}",
            f"- Changed mechanism: {item.changed_mechanism}",
            f"- Trigger: {item.trigger}",
            f"- Consequence: {item.consequence}",
            f"- Impact scope: {item.impact_scope}",
            f"- Evidence: {_anchors_text(item.evidence_anchors)}",
            f"- Action: {item.action}",
        ))
    return lines


def render_github(result: StructuredResult) -> str:
    incomplete = result.derived.outcome == "REVIEW_INCOMPLETE"
    if incomplete and result.derived.presentation != "INCOMPLETE_WITH_FINDINGS":
        raise ValidationError("incomplete results without a substantiated issue are audit-only")
    flags = "--ignore-ci" if result.policy.ignore_ci else "none"
    lines = [
        f"# {result.derived.outcome.replace('_', ' ').title()}",
        f"- Target: {result.target.owner}/{result.target.repo}#{result.target.pr_number} @ {result.target.head_sha}",
        f"- Rubric: {result.policy.rubric_version}",
        f"- Flags: {flags}",
        "",
        result.summary,
    ]
    if incomplete and result.incomplete is not None:
        lines.extend((
            "> **Core assessment was prevented.** The observations below are real but "
            "partial — treat this as an unfinished review, not a verdict.",
            f"> Missing evidence: {result.incomplete.detail}",
            f"> To finish: {result.incomplete.retry_guidance}",
        ))
    lines.extend(_render_issues("Blocking Issues", result.blocking_issues))
    if result.blocking_gate_conditions:
        lines.append("## Blocking Gate Conditions")
        lines.append("Check status alone does not assert a code defect.")
        for condition in result.blocking_gate_conditions:
            lines.extend((
                f"### {condition.check_name}",
                f"- Observed conclusion: {condition.conclusion}",
                f"- Requirement: {condition.requirement}",
                f"- Evidence: {condition.url}",
                f"- Action: {condition.action}",
            ))
    lines.extend(_render_issues("Non-Blocking Issues", result.non_blocking_issues))
    if result.risk_hypotheses:
        lines.append("## Risk Hypotheses — Unconfirmed")
        for item in result.risk_hypotheses:
            lines.extend((
                f"### {item.title}",
                f"- Observed facts: {item.observed_facts}",
                f"- Hypothesized link: {item.hypothesized_link}",
                f"- Unresolved link: {item.unresolved_link}",
                f"- Evidence: {_anchors_text(item.evidence_anchors)}",
                f"- Verification step: {item.verification_step}",
            ))
    if result.verification_gaps:
        lines.append("## Verification Gaps")
        for item in result.verification_gaps:
            lines.extend((
                f"### {item.title}",
                f"- Unassessed claim: {item.unassessed_claim}",
                f"- Evidence inspected: {_anchors_text(item.evidence_inspected)}",
                f"- Missing evidence: {item.missing_evidence}",
                f"- Next step: {item.next_step}",
            ))
    if result.release_conditions:
        lines.append("## Release Conditions")
        for item in result.release_conditions:
            lines.extend((
                f"### {item.title}",
                f"- Requirement: {item.requirement}",
                f"- Authoritative source: {item.authoritative_source}",
                f"- Phase: {item.phase}",
                f"- Completion criterion: {item.completion_criterion}",
            ))
    if result.nits:
        lines.append("## Nits")
        for item in result.nits:
            text = f"- **{item.title}** at {item.location}"
            if item.suggestion:
                text += f": {item.suggestion}"
            lines.append(text)
    if result.evidence_reviewed:
        lines.extend(("## Evidence Reviewed", _anchors_text(result.evidence_reviewed)))
    if result.verification_limits:
        lines.append("## Verification Limits")
        lines.extend(f"- {limit}" for limit in result.verification_limits)
    lines.append("## Gate Snapshot")
    if result.gate_snapshot.checks:
        lines.extend(
            f"- {check.check_name}: {check.status}/{check.conclusion} ({check.check_class}) — {check.url}"
            for check in result.gate_snapshot.checks
        )
    else:
        lines.append("No checks were reported for the exact head.")
    if result.waived_ci_conditions:
        lines.append("## Waived CI Conditions")
        lines.extend(
            f"- {condition.check_name}: {condition.conclusion} — {condition.url}"
            for condition in result.waived_ci_conditions
        )
    lines.append(f"<!-- boris-review request_id={result.request_id} result_hash={result.result_hash} -->")
    return "\n\n".join(line for line in lines if line != "") + "\n"


def render_slack(result: StructuredResult, github_url: str | None) -> str:
    short_sha = result.target.head_sha[:7]
    target = f"{result.target.owner}/{result.target.repo}#{result.target.pr_number} @ {short_sha}"
    if result.derived.outcome == "REVIEW_INCOMPLETE":
        if result.incomplete is None:
            raise ValidationError("incomplete result is missing its reason")
        lines = [
            f"REVIEW INCOMPLETE — {target}",
            result.incomplete.detail,
            f"Retry: {result.incomplete.retry_guidance}",
        ]
        if result.derived.presentation == "INCOMPLETE_WITH_FINDINGS":
            lines.append("Partial observations were published in the GitHub review.")
        elif any((
            result.non_blocking_issues,
            result.risk_hypotheses,
            result.verification_gaps,
            result.release_conditions,
            result.nits,
        )):
            lines.append("Partial observations were retained in the audit record and were not published.")
    elif result.derived.outcome == "ISSUES_FOUND":
        lines = [f"ISSUES FOUND — {target}"]
        for issue in result.blocking_issues[:3]:
            lines.append(f"• {issue.title}")
        if len(result.blocking_issues) > 3:
            lines.append(f"• +{len(result.blocking_issues) - 3} more")
        if result.blocking_gate_conditions:
            checks = ", ".join(item.check_name for item in result.blocking_gate_conditions)
            lines.append(f"Required checks blocking: {checks}")
    else:
        label = "READY WITH COMMENTS" if result.derived.presentation == "READY_WITH_COMMENTS" else "READY"
        lines = [f"{label} — {target}", "No blocking issues were found."]
        counts = (
            ("non-blocking issues", len(result.non_blocking_issues)),
            ("risk hypotheses", len(result.risk_hypotheses)),
            ("verification gaps", len(result.verification_gaps)),
            ("release conditions", len(result.release_conditions)),
            ("nits", len(result.nits)),
            ("waived CI conditions", len(result.waived_ci_conditions)),
        )
        lines.extend(f"{name}: {count}" for name, count in counts if count)
    if github_url:
        lines.append(f"GitHub review: {github_url}")
    return "\n".join(lines)
