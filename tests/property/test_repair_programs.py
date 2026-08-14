from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from app.case_data import RepairCase, inspect_case, parse_submission
from app.repairs import build_candidate_catalogue
from app.schemas import CustomRunSubmission, RepairProgram, RepairStep, SourceDocument
from app.synthesis import canonical_pipeline_hash, search_catalogue, verify_program

UNIQUE_IDS = st.lists(
    st.integers(min_value=1, max_value=1_000_000),
    min_size=1,
    max_size=18,
    unique=True,
)
TOTALS = st.lists(
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    min_size=1,
    max_size=18,
)
FIELD_NAMES = st.from_regex(r"aux_[a-z]{1,8}", fullmatch=True)


def _cross_format_case(ids: list[int], totals: list[int]) -> RepairCase:
    rows = [
        {"id": identifier, "total": totals[index % len(totals)]}
        for index, identifier in enumerate(ids)
    ]
    before = "id,total\n" + "".join(
        f"{row['id']},{row['total']}\n" for row in rows
    )
    return parse_submission(
        CustomRunSubmission(
            label="Generated cross-format chain",
            before=SourceDocument(format="csv", content=before),
            after=SourceDocument(format="json", content=json.dumps({"rows": rows})),
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
        case_id="custom_property",
    )


def _terminal_fingerprint(case: RepairCase, program: RepairProgram) -> tuple:
    result = verify_program(case, program)
    return (
        result.status,
        result.patched_pipeline_hash,
        tuple((check.name, check.passed, check.detail) for check in result.checks),
    )


@settings(max_examples=35, deadline=None)
@given(ids=UNIQUE_IDS, totals=TOTALS)
def test_verified_program_is_invariant_to_row_order(
    ids: list[int], totals: list[int]
) -> None:
    case = _cross_format_case(ids, totals)
    program = RepairProgram(
        decision="repair",
        steps=[
            RepairStep(operation="set_source_format", format="json"),
            RepairStep(operation="set_record_path", path="rows"),
        ],
        confidence=1,
        evidence=["property test"],
        rationale="property_test",
    )
    reversed_case = _cross_format_case(list(reversed(ids)), list(reversed(totals)))

    assert _terminal_fingerprint(case, program) == _terminal_fingerprint(
        reversed_case, program
    )


@settings(max_examples=30, deadline=None)
@given(ids=UNIQUE_IDS, totals=TOTALS, field=FIELD_NAMES)
def test_stable_irrelevant_field_never_expands_authorized_mutations(
    ids: list[int], totals: list[int], field: str
) -> None:
    case = _cross_format_case(ids, totals)
    baseline = [
        {"id": identifier, "total": totals[index % len(totals)], field: "stable"}
        for index, identifier in enumerate(ids)
    ]
    enriched = RepairCase(
        id=case.id,
        title=case.title,
        before=SourceDocument(format="json", content=json.dumps({"rows": baseline})),
        after=SourceDocument(format="json", content=json.dumps({"rows": baseline})),
        pipeline=case.pipeline.model_copy(
            update={"format": "json", "record_path": "rows"}
        ),
        contract=case.contract,
    )
    plain = RepairCase(
        id=case.id,
        title=case.title,
        before=SourceDocument(
            format="json",
            content=json.dumps(
                {"rows": [{"id": row["id"], "total": row["total"]} for row in baseline]}
            ),
        ),
        after=SourceDocument(
            format="json",
            content=json.dumps(
                {"rows": [{"id": row["id"], "total": row["total"]} for row in baseline]}
            ),
        ),
        pipeline=enriched.pipeline,
        contract=enriched.contract,
    )

    plain_steps = {item.step.model_dump_json() for item in build_candidate_catalogue(plain, inspect_case(plain))}
    enriched_steps = {item.step.model_dump_json() for item in build_candidate_catalogue(enriched, inspect_case(enriched))}

    assert enriched_steps <= plain_steps


@settings(max_examples=35, deadline=None)
@given(ids=UNIQUE_IDS, totals=TOTALS)
def test_deterministic_search_never_returns_an_unverified_terminal_program(
    ids: list[int], totals: list[int]
) -> None:
    case = _cross_format_case(ids, totals)
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    program = search_catalogue(case, catalogue)
    result = verify_program(case, program, catalogue)

    assert result.status in {"repaired", "unchanged", "escalated"}
    if result.status == "repaired":
        assert result.patched_pipeline is not None
        assert result.patched_pipeline_hash == canonical_pipeline_hash(
            result.patched_pipeline
        )
