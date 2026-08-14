from __future__ import annotations

import json

from app.benchmark import load_scenario, scenario_case
from app.case_data import inspect_case, parse_submission
from app.repairs import build_candidate_catalogue
from app.schemas import (
    CustomRunSubmission,
    RepairProgram,
    RepairStep,
    SourceDocument,
)
from app.synthesis import (
    canonical_pipeline_hash,
    minimal_counterexample,
    search_catalogue,
    verify_authoritative_program,
    verify_program,
)


def _submission(
    before: SourceDocument,
    after: SourceDocument,
    pipeline: dict,
    contract: dict,
    *,
    label: str = "Judge chain",
):
    return parse_submission(
        CustomRunSubmission(
            label=label,
            before=before,
            after=after,
            pipeline_json=json.dumps(pipeline),
            contract_json=json.dumps(contract),
        ),
        case_id="custom_verifier",
    )


def _cross_format_case():
    return _submission(
        SourceDocument(format="csv", content="id,total\n1,10\n2,20\n"),
        SourceDocument(
            format="json",
            content='{"payload":{"rows":[{"id":1,"total":10},{"id":2,"total":20}]}}',
        ),
        {
            "format": "csv",
            "fields": {"id": "id", "total": "total"},
            "casts": {"id": "integer", "total": "integer"},
        },
        {
            "required": ["id", "total"],
            "types": {"id": "integer", "total": "integer"},
            "unique_key": "id",
            "preserve_values": ["total"],
        },
    )


def _program_for(case, *operations: str) -> RepairProgram:
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    steps = []
    for operation in operations:
        matches = [item.step for item in catalogue if item.step.operation == operation]
        assert len(matches) == 1
        steps.append(matches[0])
    return RepairProgram(
        decision="repair",
        steps=steps,
        confidence=0.2,
        evidence=["untrusted proposal"],
        rationale="untrusted rationale",
    )


def test_verifier_recomputes_full_evidence_and_returns_usable_pipeline() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    program = _program_for(case, "set_source_format", "set_record_path")

    result = verify_program(case, program, catalogue)

    assert result.status == "repaired"
    assert result.transformed_rows == 2
    assert result.program is not None
    assert result.program.confidence == 1
    assert result.program.evidence != ["untrusted proposal"]
    assert result.patched_pipeline is not None
    assert result.patched_pipeline.format == "json"
    assert result.patched_pipeline.record_path == "payload.rows"
    assert result.patched_pipeline_hash == canonical_pipeline_hash(
        result.patched_pipeline
    )
    assert all(check.passed for check in result.checks)


def test_step_outside_the_evidence_catalogue_fails_authorization() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    forged = RepairProgram(
        decision="repair",
        steps=[RepairStep(operation="set_record_path", path="attacker.rows")],
        confidence=1,
        evidence=["claimed"],
        rationale="claimed",
    )

    result = verify_program(case, forged, catalogue)

    assert result.status == "failed"
    authorization = next(
        check for check in result.checks if check.name == "catalogue_authorized"
    )
    assert authorization.passed is False
    assert result.patched_pipeline is None


def test_deterministic_search_finds_the_shortest_composed_program() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))

    program = search_catalogue(case, catalogue)
    result = verify_program(case, program, catalogue)

    assert program.decision == "repair"
    assert [step.operation for step in program.steps] == [
        "set_source_format",
        "set_record_path",
    ]
    assert result.status == "repaired"


def test_two_distinct_minimal_passing_configurations_escalate() -> None:
    case = _submission(
        SourceDocument(format="csv", content="id,name\n1,Ada\n2,Grace\n"),
        SourceDocument(
            format="csv",
            content="id,display_name,legal_name\n1,Ada,Ada\n2,Grace,Grace\n",
        ),
        {
            "format": "csv",
            "fields": {"id": "id", "name": "name"},
            "casts": {"id": "integer"},
        },
        {
            "required": ["id", "name"],
            "types": {"id": "integer", "name": "string"},
            "unique_key": "id",
            "preserve_values": ["name"],
        },
    )
    catalogue = build_candidate_catalogue(case, inspect_case(case))

    program = search_catalogue(case, catalogue)

    assert program.decision == "escalate"
    assert program.steps == []
    assert program.rationale == "ambiguous_repair"


def test_minimal_counterexample_contains_no_source_values() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    incomplete = _program_for(case, "set_source_format")

    result = verify_program(case, incomplete, catalogue)
    counterexample = minimal_counterexample(result)

    assert result.status == "failed"
    assert counterexample.failing_count >= 1
    assert counterexample.invariant
    assert "10" not in counterexample.detail
    assert "20" not in counterexample.detail


def test_healthy_chain_search_returns_unchanged_without_a_patch() -> None:
    case = _submission(
        SourceDocument(format="csv", content="id,total\n1,10\n"),
        SourceDocument(format="csv", content="id,total\n1,10\n"),
        {
            "format": "csv",
            "fields": {"id": "id", "total": "total"},
            "casts": {"id": "integer", "total": "integer"},
        },
        {
            "required": ["id", "total"],
            "types": {"id": "integer", "total": "integer"},
            "unique_key": "id",
            "preserve_values": ["total"],
        },
    )
    catalogue = build_candidate_catalogue(case, inspect_case(case))

    program = search_catalogue(case, catalogue)
    result = verify_program(case, program, catalogue)

    assert program.decision == "unchanged"
    assert result.status == "unchanged"
    assert result.patched_pipeline is None
    assert result.patched_pipeline_hash is None


def test_authority_canonicalizes_an_equivalent_commuting_selection() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    canonical = search_catalogue(case, catalogue)
    proposed = canonical.model_copy(
        update={"steps": list(reversed(canonical.steps))}
    )

    result = verify_authoritative_program(case, proposed, catalogue)

    assert result.status == "repaired"
    assert result.program.steps == canonical.steps


def test_authority_rejects_a_valid_but_nonminimal_program() -> None:
    case = scenario_case(load_scenario("custom-program-four"))
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    canonical = search_catalogue(case, catalogue)
    extra = next(
        item.step
        for item in catalogue
        if item.step.operation == "set_cast"
        and item.step.field == "id"
        and item.step.strategy == "integer_from_float"
    )
    proposed = canonical.model_copy(update={"steps": [*canonical.steps, extra]})

    assert verify_program(case, proposed, catalogue).status == "repaired"
    result = verify_authoritative_program(case, proposed, catalogue)

    assert result.status == "failed"
    assert any(
        check.name == "unique_shortest_program" and not check.passed
        for check in result.checks
    )


def test_authority_rejects_chained_ambiguous_aliases() -> None:
    case = scenario_case(load_scenario("custom-ambiguous-alias"))
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    aliases = [
        item.step
        for item in catalogue
        if item.step.operation == "update_field_sources"
    ]
    proposed = RepairProgram(
        decision="repair",
        steps=aliases,
        confidence=1,
        evidence=["claimed"],
        rationale="claimed",
    )

    assert verify_program(case, proposed, catalogue).status == "repaired"
    result = verify_authoritative_program(case, proposed, catalogue)

    assert result.status == "failed"
    assert search_catalogue(case, catalogue).decision == "escalate"
