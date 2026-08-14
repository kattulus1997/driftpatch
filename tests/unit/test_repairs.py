from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.benchmark import load_scenarios
from app.case_data import inspect_case, parse_submission, run_case_contracts
from app.repairs import (
    CandidateCatalogue,
    affected_output_fields,
    apply_repair_program,
    build_candidate_catalogue,
    program_from_legacy_plan,
)
from app.schemas import (
    Candidate,
    CustomRunSubmission,
    RepairProgram,
    RepairStep,
    SourceDocument,
)


def _cross_format_case():
    return parse_submission(
        CustomRunSubmission(
            label="Nested orders",
            before=SourceDocument(format="csv", content="id,total\n1,10\n2,20\n"),
            after=SourceDocument(
                format="json",
                content='{"payload":{"rows":[{"id":1,"total":10},{"id":2,"total":20}]}}',
            ),
            pipeline_json=json.dumps(
                {
                    "format": "csv",
                    "fields": {"id": "id", "total": "total"},
                    "casts": {"id": "integer", "total": "integer"},
                }
            ),
            contract_json=json.dumps(
                {
                    "required": ["id", "total"],
                    "types": {"id": "integer", "total": "integer"},
                    "unique_key": "id",
                    "preserve_values": ["total"],
                }
            ),
        ),
        case_id="custom_nested",
    )


def _candidate(
    catalogue: CandidateCatalogue, operation: str, **parameters: object
) -> Candidate:
    matches = [
        item
        for item in catalogue
        if item.step.operation == operation
        and all(getattr(item.step, name) == value for name, value in parameters.items())
    ]
    assert len(matches) == 1
    return matches[0]


def test_csv_to_nested_json_requires_ordered_format_and_path_steps() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    format_candidate = _candidate(
        catalogue, "set_source_format", format="json"
    )
    path_candidate = _candidate(
        catalogue, "set_record_path", path="payload.rows"
    )
    program = RepairProgram(
        decision="repair",
        steps=[format_candidate.step, path_candidate.step],
        evidence=["format and record path changed"],
        rationale="Both observed structural changes are required.",
        confidence=0,
    )

    patched = apply_repair_program(case.pipeline, program)
    _, checks = run_case_contracts(case, patched)

    assert patched.format == "json"
    assert patched.record_path == "payload.rows"
    assert all(check.passed for check in checks)


def test_nested_json_to_root_json_can_clear_the_record_path() -> None:
    case = parse_submission(
        CustomRunSubmission(
            label="Root records",
            before=SourceDocument(
                format="json",
                content='{"payload":{"rows":[{"id":1,"total":10}]}}',
            ),
            after=SourceDocument(
                format="json", content='[{"id":1,"total":10}]'
            ),
            pipeline_json=json.dumps(
                {
                    "format": "json",
                    "record_path": "payload.rows",
                    "fields": {"id": "id", "total": "total"},
                    "casts": {"id": "integer", "total": "integer"},
                }
            ),
            contract_json=json.dumps(
                {
                    "required": ["id", "total"],
                    "types": {"id": "integer", "total": "integer"},
                    "unique_key": "id",
                    "preserve_values": ["total"],
                }
            ),
        )
    )
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    root = _candidate(catalogue, "set_record_path", path="$")
    program = RepairProgram(
        decision="repair",
        steps=[root.step],
        evidence=["record list moved to the root"],
        rationale="Clear the obsolete nested record path.",
        confidence=0,
    )

    patched = apply_repair_program(case.pipeline, program)
    _, checks = run_case_contracts(case, patched)

    assert patched.record_path is None
    assert all(check.passed for check in checks)


def test_program_rejects_duplicate_or_more_than_six_steps() -> None:
    step = RepairStep(operation="set_source_format", format="json")

    with pytest.raises(ValidationError, match="unique"):
        RepairProgram(
            decision="repair",
            steps=[step, step],
            evidence=["format changed"],
            rationale="Apply the observed format.",
            confidence=0,
        )


def test_semantically_equal_retargets_are_duplicates_regardless_of_json_order() -> None:
    first = RepairStep(
        operation="update_field_sources",
        field_sources=[
            {"output_field": "latitude", "source_field": "latitudine"},
            {"output_field": "longitude", "source_field": "longitudine"},
        ],
    )
    reversed_order = RepairStep(
        operation="update_field_sources",
        field_sources=[
            {"output_field": "longitude", "source_field": "longitudine"},
            {"output_field": "latitude", "source_field": "latitudine"},
        ],
    )

    with pytest.raises(ValidationError, match="unique"):
        RepairProgram(
            decision="repair",
            steps=[first, reversed_order],
            evidence=["documented aliases"],
            rationale="Each semantic mutation may appear once.",
            confidence=0,
        )
    with pytest.raises(ValidationError):
        RepairProgram(
            decision="repair",
            steps=[
                RepairStep(operation="set_cast", field=f"f{index}", strategy="integer")
                for index in range(7)
            ],
            evidence=["types changed"],
            rationale="Apply bounded casts.",
            confidence=0,
        )


@pytest.mark.parametrize("decision", ["unchanged", "escalate"])
def test_nonrepair_decisions_cannot_smuggle_mutations(decision: str) -> None:
    with pytest.raises(ValidationError, match="zero steps"):
        RepairProgram(
            decision=decision,
            steps=[RepairStep(operation="set_source_format", format="json")],
            evidence=["decision evidence"],
            rationale="No mutation is authorized.",
            confidence=0,
        )


def test_candidate_ids_are_stable_opaque_and_row_values_never_enter_summaries() -> None:
    case = _cross_format_case()

    first = build_candidate_catalogue(case, inspect_case(case))
    second = build_candidate_catalogue(case, inspect_case(case))

    assert [item.id for item in first] == [item.id for item in second]
    assert all(item.id.startswith("c_") and len(item.id) == 14 for item in first)
    assert "10" not in " ".join(item.summary for item in first)
    assert "20" not in " ".join(item.summary for item in first)


def test_catalogue_rejects_unknown_and_repeated_candidate_ids() -> None:
    case = _cross_format_case()
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    identifier = next(iter(catalogue)).id

    with pytest.raises(ValueError, match="unique"):
        catalogue.select([identifier, identifier])
    with pytest.raises(ValueError, match="unknown candidate"):
        catalogue.select(["c_000000000000"])


def test_two_value_preserving_renames_remain_distinct_candidates() -> None:
    case = parse_submission(
        CustomRunSubmission(
            label="Ambiguous names",
            before=SourceDocument(format="csv", content="id,name\n1,Ada\n2,Grace\n"),
            after=SourceDocument(
                format="csv",
                content="id,display_name,legal_name\n1,Ada,Ada\n2,Grace,Grace\n",
            ),
            pipeline_json=json.dumps(
                {
                    "format": "csv",
                    "fields": {"id": "id", "name": "name"},
                    "casts": {"id": "integer"},
                }
            ),
            contract_json=json.dumps(
                {
                    "required": ["id", "name"],
                    "types": {"id": "integer", "name": "string"},
                    "unique_key": "id",
                    "preserve_values": ["name"],
                }
            ),
        )
    )

    catalogue = build_candidate_catalogue(case, inspect_case(case))
    renames = [
        item
        for item in catalogue
        if item.step.operation == "update_field_sources"
    ]

    assert {item.step.field_sources[0].source_field for item in renames} == {
        "display_name",
        "legal_name",
    }


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for suite in ("demo", "external")
        for scenario in load_scenarios(suite=suite)
    ],
    ids=lambda scenario: scenario.id,
)
def test_legacy_fixture_decisions_have_an_equivalent_program(scenario) -> None:
    program = program_from_legacy_plan(scenario.expected_plan)

    patched = apply_repair_program(scenario.pipeline, program)

    assert program.decision == (
        "unchanged"
        if scenario.expected_plan.operation == "no_change"
        else "escalate"
        if scenario.expected_plan.operation == "escalate"
        else "repair"
    )
    assert len(program.steps) == (1 if program.decision == "repair" else 0)
    assert affected_output_fields(scenario.pipeline, program) == affected_output_fields(
        scenario.pipeline, scenario.expected_plan
    )
    if program.decision != "repair":
        assert patched == scenario.pipeline
