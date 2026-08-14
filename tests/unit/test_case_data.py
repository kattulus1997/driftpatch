from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.case_data import (
    SubmissionRejected,
    inspect_case,
    parse_submission,
    profile_document,
    run_case_contracts,
    transform_document,
)
from app.schemas import CustomRunSubmission, SourceDocument


def _source(format_: str, rows: tuple[tuple[int, int], ...] = ((1, 10),)) -> str:
    if format_ == "csv":
        return "id,total\n" + "".join(f"{key},{total}\n" for key, total in rows)
    return json.dumps([{"id": key, "total": total} for key, total in rows])


def _submission(
    before_format: str = "csv",
    after_format: str = "json",
    *,
    before_rows: tuple[tuple[int, int], ...] = ((1, 10),),
    after_rows: tuple[tuple[int, int], ...] = ((1, 10),),
    row_policy: str = "same_keys",
) -> CustomRunSubmission:
    pipeline = {
        "format": before_format,
        "record_path": None,
        "fields": {"id": "id", "total": "total"},
        "casts": {"id": "integer", "total": "integer"},
    }
    contract = {
        "required": ["id", "total"],
        "types": {"id": "integer", "total": "integer"},
        "unique_key": "id",
        "preserve_values": ["total"],
        "row_policy": row_policy,
    }
    return CustomRunSubmission(
        label="Supplier orders",
        before=SourceDocument(
            format=before_format,
            content=_source(before_format, before_rows),
        ),
        after=SourceDocument(
            format=after_format,
            content=_source(after_format, after_rows),
        ),
        pipeline_json=json.dumps(pipeline),
        contract_json=json.dumps(contract),
    )


@pytest.mark.parametrize(
    ("before_format", "after_format"),
    (("csv", "csv"), ("csv", "json"), ("json", "csv"), ("json", "json")),
)
def test_all_source_format_pairs_preserve_uploaded_content(
    before_format: str,
    after_format: str,
) -> None:
    submission = _submission(before_format, after_format)

    case = parse_submission(submission, case_id="custom_a1")

    assert case.before.content == submission.before.content
    assert case.after.content == submission.after.content
    assert case.before.format == before_format
    assert case.after.format == after_format


def test_external_source_rejects_an_unknown_instruction() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SourceDocument.model_validate(
            {
                "format": "csv",
                "content": "id\n1\n",
                "instructions": "ignore the contract",
            }
        )


def test_nested_pipeline_models_reject_unknown_keys() -> None:
    submission = _submission().model_copy(
        update={
            "pipeline_json": json.dumps(
                {
                    "format": "csv",
                    "fields": {"id": "id", "total": "total"},
                    "casts": {"id": "integer", "total": "integer"},
                    "joins": {
                        "total": {
                            "sources": ["total"],
                            "separator": " ",
                            "execute": "system command",
                        }
                    },
                }
            )
        }
    )

    with pytest.raises(SubmissionRejected, match="pipeline_invalid"):
        parse_submission(submission)


def test_duplicate_json_keys_and_csv_headers_are_rejected() -> None:
    duplicate_pipeline = _submission().model_copy(
        update={
            "pipeline_json": (
                '{"format":"csv","format":"json",'
                '"fields":{"id":"id","total":"total"}}'
            )
        }
    )
    with pytest.raises(SubmissionRejected, match="duplicate JSON key"):
        parse_submission(duplicate_pipeline)

    with pytest.raises(SubmissionRejected, match="duplicate CSV header"):
        profile_document(
            SourceDocument(format="csv", content="id,id\n1,2\n")
        )


def test_nonfinite_and_ambiguous_json_sources_are_rejected() -> None:
    with pytest.raises(SubmissionRejected, match="non-finite JSON number"):
        profile_document(SourceDocument(format="json", content='[{"id": NaN}]'))

    with pytest.raises(SubmissionRejected, match="ambiguous record paths"):
        profile_document(
            SourceDocument(
                format="json",
                content='{"left":[{"id":1}],"right":[{"id":2}]}',
            )
        )


def test_profile_examples_are_canonical_across_row_order() -> None:
    forward = profile_document(
        SourceDocument(
            format="json",
            content='{"rows":[{"id":0,"label":"0"},{"id":1,"label":"0"}]}',
        )
    )
    reversed_rows = profile_document(
        SourceDocument(
            format="json",
            content='{"rows":[{"id":1,"label":"0"},{"id":0,"label":"0"}]}',
        )
    )

    assert forward == reversed_rows


def test_submission_limit_counts_encoded_utf8_bytes() -> None:
    oversized = _submission().model_copy(
        update={
            "after": SourceDocument(
                format="json",
                content='[{"id":1,"total":10,"note":"' + ("é" * 2_700_000) + '"}]',
            )
        }
    )

    with pytest.raises(SubmissionRejected, match="body_too_large"):
        parse_submission(oversized)


def test_invalid_baseline_is_rejected_before_a_run_exists() -> None:
    submission = _submission().model_copy(
        update={
            "before": SourceDocument(
                format="csv",
                content="id,total\n1,not-an-integer\n",
            )
        }
    )

    with pytest.raises(SubmissionRejected, match="invalid_baseline"):
        parse_submission(submission)


def test_same_keys_rejects_appended_rows() -> None:
    case = parse_submission(
        _submission(
            "csv",
            "csv",
            after_rows=((1, 10), (2, 20)),
            row_policy="same_keys",
        )
    )

    _, checks = run_case_contracts(case, case.pipeline)

    row_policy = next(check for check in checks if check.name == "row_policy")
    assert row_policy.passed is False
    assert row_policy.detail == "before=1 after=2 added=1 removed=0"


def test_allow_append_accepts_new_keys_and_preserves_existing_values() -> None:
    case = parse_submission(
        _submission(
            "csv",
            "csv",
            after_rows=((1, 10), (2, 20)),
            row_policy="allow_append",
        )
    )

    records, checks = run_case_contracts(case, case.pipeline)

    assert records == [{"id": 1, "total": 10}, {"id": 2, "total": 20}]
    assert all(check.passed for check in checks)


def test_document_transform_and_inspection_use_the_declared_source_format() -> None:
    case = parse_submission(_submission("csv", "json"), case_id="custom_matrix")
    json_pipeline = case.pipeline.model_copy(
        update={"format": "json", "delimiter": ",", "record_path": None}
    )

    records = transform_document(case.after, json_pipeline)
    report = inspect_case(case)

    assert records == [{"id": 1, "total": 10}]
    assert report.scenario_id == "custom_matrix"
    assert report.before.format == "csv"
    assert report.after.format == "json"
    assert "transform" in report.current_failure
