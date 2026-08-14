from __future__ import annotations

from .benchmark import (
    inspect_scenario,
    load_scenario,
    run_contracts,
    scenario_case,
    scenario_source,
    transform,
)
from .case_data import inspect_case
from .repairs import (
    affected_output_fields,
    apply_repair_plan,
    authorize_repair_plan,
    build_candidate_catalogue,
)
from .schemas import (
    ApplyResult,
    CheckResult,
    RepairPlan,
    RepairProgram,
    ValidationResult,
)
from .synthesis import search_catalogue, verify_program


def apply_plan_deterministically(scenario_id: str, plan: RepairPlan) -> ApplyResult:
    scenario = load_scenario(scenario_id)
    report = inspect_scenario(scenario)
    authorized, authorization_detail = authorize_repair_plan(report, plan)
    application_error = None
    if not authorized:
        patched = scenario.pipeline
        application_error = f"authorization rejected: {authorization_detail}"
    else:
        try:
            patched = apply_repair_plan(scenario.pipeline, plan)
        except ValueError as exc:
            patched = scenario.pipeline
            application_error = str(exc)
    return ApplyResult(
        scenario_id=scenario.id,
        plan=plan,
        patched_pipeline=patched,
        changed=patched != scenario.pipeline,
        application_error=application_error,
    )


def validate_plan_deterministically(applied: ApplyResult) -> ValidationResult:
    scenario = load_scenario(applied.scenario_id)
    report = inspect_scenario(scenario)
    _, baseline_checks = run_contracts(
        scenario_source(scenario, "after"),
        scenario.pipeline,
        scenario.contract,
    )
    baseline_passed = bool(baseline_checks) and all(
        check.passed for check in baseline_checks
    )
    records, contract_checks = run_contracts(
        scenario_source(scenario, "after"),
        applied.patched_pipeline,
        scenario.contract,
    )
    authorized, authorization_detail = authorize_repair_plan(report, applied.plan)
    if applied.application_error:
        authorized = False
        authorization_detail = f"application rejected: {applied.application_error}"

    preservation_passed = True
    preservation_detail = "not required for a non-mutating decision"
    if applied.changed:
        try:
            before_records = transform(
                scenario_source(scenario, "before"), scenario.pipeline
            )
            after_records = transform(
                scenario_source(scenario, "after"), applied.patched_pipeline
            )
            unique_key = scenario.contract.unique_key
            before_by_key = {record[unique_key]: record for record in before_records}
            after_by_key = {record[unique_key]: record for record in after_records}
            common_keys = before_by_key.keys() & after_by_key.keys()
            affected = affected_output_fields(scenario.pipeline, applied.plan) - {
                unique_key
            }
            preserved_fields = set(scenario.contract.preserve_values)
            aliases = scenario.contract.source_aliases
            values_preserved = all(
                before_by_key[key].get(field) == after_by_key[key].get(field)
                for key in common_keys
                for field in preserved_fields
            )
            aliased_fields = aliases.keys() & affected
            affected_covered = affected <= preserved_fields | aliased_fields
            preservation_contract = bool(preserved_fields or aliased_fields)
            preservation_passed = (
                preservation_contract
                and bool(common_keys)
                and set(before_by_key) == set(after_by_key)
                and values_preserved
                and affected_covered
            )
            preservation_detail = (
                f"matched={len(common_keys)} preserved_fields={len(preserved_fields)} "
                f"aliased_fields={len(aliased_fields)}"
            )
        except (KeyError, TypeError, ValueError) as exc:
            preservation_passed = False
            preservation_detail = str(exc)

    decision_checks = [
        CheckResult(
            name="baseline_failure",
            passed=(
                baseline_passed
                if applied.plan.operation == "no_change"
                else not baseline_passed
            ),
            detail=(
                "baseline passed; no mutation is justified"
                if baseline_passed
                else "baseline failed; intervention or escalation is justified"
            ),
        ),
        CheckResult(
            name="operation_authorized",
            passed=authorized,
            detail=authorization_detail,
        ),
        CheckResult(
            name="semantic_preservation",
            passed=preservation_passed,
            detail=preservation_detail,
        ),
    ]
    checks = [*decision_checks, *contract_checks]
    passed = all(check.passed for check in checks)
    if applied.plan.operation == "no_change":
        terminal_status = "unchanged" if passed and not applied.changed else "failed"
    elif applied.plan.operation == "escalate":
        terminal_status = (
            "escalated"
            if (
                not baseline_passed
                and authorized
                and not applied.application_error
                and not applied.changed
            )
            else "failed"
        )
    elif passed and applied.changed:
        terminal_status = "repaired"
    else:
        terminal_status = "failed"

    observed_evidence = [
        *(f"removed field: {field}" for field in report.removed_fields),
        *(f"added field: {field}" for field in report.added_fields),
        *(
            f"baseline failure: {check.name} ({check.detail})"
            for check in baseline_checks
            if not check.passed
        ),
    ]
    if not observed_evidence:
        observed_evidence = ["baseline contracts remained satisfied"]
    deterministic_confidence = (
        sum(check.passed for check in checks) / len(checks) if checks else 0.0
    )
    verified_plan = applied.plan.model_copy(
        update={
            "confidence": deterministic_confidence,
            "evidence": observed_evidence,
            "rationale": authorization_detail,
        }
    )
    evidence_complete = bool(checks and observed_evidence and authorization_detail)
    summary = (
        f"{scenario.title}: {terminal_status} with {applied.plan.operation}; "
        f"{sum(check.passed for check in checks)}/{len(checks)} validation checks passed; "
        f"contract gate {'passed' if passed else 'failed'}."
    )
    if terminal_status == "escalated":
        summary += f" Reason: {authorization_detail}"
    return ValidationResult(
        scenario_id=scenario.id,
        status=terminal_status,
        plan=verified_plan,
        checks=checks,
        transformed_rows=len(records),
        evidence_complete=evidence_complete,
        summary=summary,
    )


def decide_plan(scenario_id: str, plan: RepairPlan) -> ValidationResult:
    return validate_plan_deterministically(
        apply_plan_deterministically(scenario_id, plan)
    )


def verify_scenario_program(
    scenario_id: str, program: RepairProgram
) -> ValidationResult:
    case = scenario_case(load_scenario(scenario_id))
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    return verify_program(case, program, catalogue)


def search_scenario(scenario_id: str) -> ValidationResult:
    case = scenario_case(load_scenario(scenario_id))
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    return verify_program(case, search_catalogue(case, catalogue), catalogue)
