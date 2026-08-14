from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .case_data import (
    RepairCase,
    inspect_case,
    run_case_contracts,
    run_document_contracts,
)
from .repairs import (
    CandidateCatalogue,
    affected_output_fields,
    apply_repair_program,
    apply_repair_step,
    build_candidate_catalogue,
)
from .schemas import (
    CheckResult,
    Counterexample,
    PipelineConfig,
    RepairProgram,
    RepairStep,
    ValidationResult,
)

MAX_SEARCH_STATES = 4_096
MAX_SEARCH_SECONDS = 0.750
MAX_REPAIR_STEPS = 6


def canonical_pipeline_hash(config: PipelineConfig) -> str:
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_step(step: RepairStep) -> str:
    return json.dumps(
        step.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        sort_keys=True,
        separators=(",", ":"),
    )


def _catalogue_index(catalogue: CandidateCatalogue) -> dict[str, str]:
    return {_canonical_step(item.step): item.id for item in catalogue}


def _changed_output_fields(
    before: PipelineConfig, after: PipelineConfig
) -> frozenset[str]:
    changed: set[str] = set()
    if (
        before.format != after.format
        or before.delimiter != after.delimiter
        or before.record_path != after.record_path
    ):
        changed.update(before.fields)
    for field in before.fields:
        if before.fields.get(field) != after.fields.get(field):
            changed.add(field)
        for attribute in ("casts", "date_formats", "booleans", "joins", "splits"):
            if getattr(before, attribute).get(field) != getattr(after, attribute).get(
                field
            ):
                changed.add(field)
    return frozenset(changed)


def _observed_evidence(
    case: RepairCase,
    program: RepairProgram,
    current_checks: list[CheckResult],
    selected_ids: Iterable[str],
) -> list[str]:
    report = inspect_case(case)
    evidence: list[str] = []
    if report.before.format != report.after.format:
        evidence.append(
            f"source format: {report.before.format} to {report.after.format}"
        )
    evidence.extend(f"removed field: {name}" for name in report.removed_fields[:4])
    evidence.extend(f"added field: {name}" for name in report.added_fields[:4])
    evidence.extend(
        f"current failure: {check.name}"
        for check in current_checks
        if not check.passed
    )
    evidence.extend(
        f"authorized candidate: {identifier}" for identifier in selected_ids
    )
    if not evidence:
        evidence = ["current contracts satisfied"]
    return evidence[:16]


def verify_program(
    case: RepairCase,
    program: RepairProgram,
    catalogue: CandidateCatalogue | None = None,
) -> ValidationResult:
    catalogue = catalogue or build_candidate_catalogue(case, inspect_case(case))
    index = _catalogue_index(catalogue)
    selected = [_canonical_step(step) for step in program.steps]
    selected_ids = [index[payload] for payload in selected if payload in index]
    authorized = len(selected_ids) == len(program.steps)

    _, baseline_checks = run_document_contracts(
        case.before, case.pipeline, case.contract
    )
    baseline_valid = bool(baseline_checks) and all(
        check.passed for check in baseline_checks
    )
    current_records, current_checks = run_case_contracts(case, case.pipeline)
    current_valid = bool(current_checks) and all(check.passed for check in current_checks)

    application_error: str | None = None
    try:
        proposed = apply_repair_program(case.pipeline, program)
    except (TypeError, ValueError) as exc:
        proposed = case.pipeline
        application_error = str(exc)
    changed = proposed != case.pipeline
    intervention_justified = (
        current_valid if program.decision == "unchanged" else not current_valid
    )
    change_shape_valid = (
        changed if program.decision == "repair" else not changed
    )
    expected_affected = affected_output_fields(case.pipeline, program)
    actual_affected = _changed_output_fields(case.pipeline, proposed)
    coverage_valid = (
        bool(actual_affected)
        and actual_affected <= expected_affected
        and expected_affected <= set(case.pipeline.fields)
        if program.decision == "repair"
        else not actual_affected
    )
    decision_checks = [
        CheckResult(
            name="baseline_valid",
            passed=baseline_valid,
            detail="submitted baseline satisfies its declared contract",
        ),
        CheckResult(
            name="intervention_justified",
            passed=intervention_justified,
            detail=(
                "current contracts pass"
                if current_valid
                else "current contracts fail"
            ),
        ),
        CheckResult(
            name="catalogue_authorized",
            passed=authorized and application_error is None,
            detail=(
                f"selected={len(program.steps)} authorized={len(selected_ids)}"
                if application_error is None
                else f"application rejected: {application_error}"
            ),
        ),
        CheckResult(
            name="configuration_change",
            passed=change_shape_valid,
            detail=f"decision={program.decision} changed={str(changed).lower()}",
        ),
        CheckResult(
            name="affected_output_coverage",
            passed=coverage_valid,
            detail=(
                f"declared={len(expected_affected)} observed={len(actual_affected)}"
            ),
        ),
    ]

    if program.decision == "escalate":
        records: list[dict[str, object]] = current_records
        contract_checks = [
            CheckResult(
                name="safe_escalation",
                passed=not current_valid and not changed,
                detail="no configuration is applied while contracts fail",
            )
        ]
    else:
        records, contract_checks = run_case_contracts(case, proposed)
    checks = [*decision_checks, *contract_checks]
    passed = bool(checks) and all(check.passed for check in checks)
    if program.decision == "unchanged" and passed:
        status = "unchanged"
    elif program.decision == "repair" and passed:
        status = "repaired"
    elif program.decision == "escalate" and passed:
        status = "escalated"
    else:
        status = "failed"

    deterministic_rationale = {
        "unchanged": "contracts_already_satisfied",
        "repaired": "verified_repair",
        "failed": "verification_failed",
    }.get(status, program.rationale)
    if status == "escalated" and deterministic_rationale not in {
        "ambiguous_repair",
        "search_exhausted",
        "safety_screen_blocked",
        "delivery_exhausted",
        "execution_exhausted",
    }:
        deterministic_rationale = "deterministic_escalation"
    evidence = _observed_evidence(case, program, current_checks, selected_ids)
    verified_program = program.model_copy(
        deep=True,
        update={
            "confidence": sum(check.passed for check in checks) / len(checks),
            "evidence": evidence,
            "rationale": deterministic_rationale,
        },
    )
    patched_pipeline = proposed if status == "repaired" else None
    operations = ", ".join(step.operation for step in program.steps) or program.decision
    summary = (
        f"{case.title}: {status}; program {operations}; "
        f"{sum(check.passed for check in checks)}/{len(checks)} deterministic checks passed."
    )
    return ValidationResult(
        scenario_id=case.id,
        status=status,
        program=verified_program,
        checks=checks,
        transformed_rows=len(records),
        evidence_complete=bool(evidence and checks),
        summary=summary,
        patched_pipeline=patched_pipeline,
        patched_pipeline_hash=(
            canonical_pipeline_hash(proposed) if patched_pipeline is not None else None
        ),
    )


def minimal_counterexample(result: ValidationResult) -> Counterexample:
    failed = next((check for check in result.checks if not check.passed), None)
    if failed is None:
        raise ValueError("a passing result has no counterexample")
    prefix, separator, field = failed.name.partition(":")
    counts = [
        int(value)
        for value in re.findall(
            r"(?:missing|invalid|changed|added|removed|observed)=(\d+)",
            failed.detail,
        )
    ]
    failing_count = max([value for value in counts if value > 0], default=1)
    invariant = prefix[:128] or "verification"
    output_field = field[:128] if separator and field else None
    return Counterexample(
        invariant=invariant,
        output_field=output_field,
        failing_count=failing_count,
        detail=f"{failed.name[:128]} failed; affected={failing_count}",
    )


@dataclass(frozen=True)
class _SearchState:
    config: PipelineConfig
    candidate_ids: tuple[str, ...]
    score: int


def _search_program(
    catalogue: CandidateCatalogue, candidate_ids: tuple[str, ...], rationale: str
) -> RepairProgram:
    return RepairProgram(
        decision="repair",
        steps=catalogue.select(list(candidate_ids)),
        confidence=1,
        evidence=[f"deterministic search depth {len(candidate_ids)}"],
        rationale=rationale,
    )


def _terminal_program(decision: str, rationale: str) -> RepairProgram:
    return RepairProgram(
        decision=decision,
        steps=[],
        confidence=1,
        evidence=[rationale.replace("_", " ")],
        rationale=rationale,
    )


def _step_is_applicable(config: PipelineConfig, step: RepairStep) -> bool:
    if step.operation == "set_record_path":
        return config.format == "json"
    if step.operation == "set_delimiter":
        return config.format == "csv"
    return True


def search_catalogue(
    case: RepairCase,
    catalogue: CandidateCatalogue,
    *,
    max_states: int = MAX_SEARCH_STATES,
    max_seconds: float = MAX_SEARCH_SECONDS,
) -> RepairProgram:
    unchanged = _terminal_program("unchanged", "contracts_already_satisfied")
    if verify_program(case, unchanged, catalogue).status == "unchanged":
        return unchanged

    ordered = sorted(catalogue, key=lambda item: item.id)
    _, initial_checks = run_case_contracts(case, case.pipeline)
    frontier = [
        _SearchState(
            config=case.pipeline,
            candidate_ids=(),
            score=sum(check.passed for check in initial_checks),
        )
    ]
    visited = {canonical_pipeline_hash(case.pipeline)}
    started = time.perf_counter()
    states = 0

    for _depth in range(1, MAX_REPAIR_STEPS + 1):
        next_frontier: list[_SearchState] = []
        passing: dict[str, tuple[str, ...]] = {}
        exhausted = False
        for state in frontier:
            for item in ordered:
                if item.id in state.candidate_ids or not _step_is_applicable(
                    state.config, item.step
                ):
                    continue
                if states >= max_states or time.perf_counter() - started >= max_seconds:
                    exhausted = True
                    break
                states += 1
                try:
                    patched = apply_repair_step(state.config, item.step)
                except (TypeError, ValueError):
                    continue
                digest = canonical_pipeline_hash(patched)
                if digest in visited:
                    continue
                visited.add(digest)
                candidate_ids = (*state.candidate_ids, item.id)
                _, checks = run_case_contracts(case, patched)
                score = sum(check.passed for check in checks)
                if score < state.score:
                    continue
                child = _SearchState(patched, candidate_ids, score)
                if checks and all(check.passed for check in checks):
                    passing[digest] = candidate_ids
                else:
                    next_frontier.append(child)
            if exhausted:
                break
        if exhausted:
            return _terminal_program("escalate", "search_exhausted")
        if len(passing) > 1:
            return _terminal_program("escalate", "ambiguous_repair")
        if len(passing) == 1:
            candidate_ids = next(iter(passing.values()))
            return _search_program(catalogue, candidate_ids, "shortest_verified_repair")
        frontier = next_frontier
        if not frontier:
            break
    return _terminal_program("escalate", "search_exhausted")


def verify_authoritative_program(
    case: RepairCase,
    program: RepairProgram,
    catalogue: CandidateCatalogue,
    *,
    canonical_program: RepairProgram | None = None,
) -> ValidationResult:
    """Accept fail-closed escalation or the unique shortest configuration."""
    proposed = verify_program(case, program, catalogue)
    if program.decision == "escalate" and proposed.status == "escalated":
        return proposed

    canonical = canonical_program or search_catalogue(case, catalogue)
    expected = verify_program(case, canonical, catalogue)
    equivalent = program.decision == canonical.decision
    if program.decision == "repair":
        equivalent = (
            equivalent
            and proposed.status == "repaired"
            and expected.status == "repaired"
            and len(program.steps) == len(canonical.steps)
            and proposed.patched_pipeline_hash == expected.patched_pipeline_hash
        )
    if equivalent:
        return expected

    authority = CheckResult(
        name="unique_shortest_program",
        passed=False,
        detail="proposal differs from the unique shortest verified terminal",
    )
    return proposed.model_copy(
        deep=True,
        update={
            "status": "failed",
            "checks": [*proposed.checks, authority],
            "summary": f"{case.title}: failed authoritative program check.",
            "patched_pipeline": None,
            "patched_pipeline_hash": None,
            "application": None,
        },
    )
