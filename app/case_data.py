from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from .schemas import (
    CheckResult,
    Contract,
    CustomRunSubmission,
    DriftReport,
    FieldProfile,
    PipelineConfig,
    SourceDocument,
    SourceProfile,
)

MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_SOURCE_BYTES = MAX_REQUEST_BYTES
MAX_RECORDS = 20_000
MAX_FIELDS = 256
MAX_CELL_CHARS = 100_000
MAX_JSON_DEPTH = 32
CSV_DELIMITERS = (",", ";", "|", "\t")


class SubmissionRejected(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class RepairCase:
    id: str
    title: str
    before: SourceDocument
    after: SourceDocument
    pipeline: PipelineConfig
    contract: Contract


@dataclass(frozen=True)
class _ParsedDocument:
    records: list[dict[str, Any]]
    delimiter: str | None
    record_path: str | None


def _text_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _validate_text_size(value: str, label: str) -> None:
    if _text_size(value) > MAX_SOURCE_BYTES:
        raise SubmissionRejected(
            "source_too_large",
            f"{label} exceeds the {MAX_SOURCE_BYTES}-byte source limit",
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionRejected("duplicate_json_key", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise SubmissionRejected(
        "nonfinite_json_number", f"non-finite JSON number: {value}"
    )


def _validate_json_limits(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise SubmissionRejected(
            "json_too_deep", f"JSON exceeds the {MAX_JSON_DEPTH}-level depth limit"
        )
    if isinstance(value, dict):
        if len(value) > MAX_FIELDS:
            raise SubmissionRejected(
                "too_many_fields",
                f"JSON object exceeds the {MAX_FIELDS}-field limit",
            )
        for key, child in value.items():
            if any(ord(character) < 32 for character in key):
                raise SubmissionRejected(
                    "invalid_field_name", "JSON field name contains a control character"
                )
            if len(key) > MAX_CELL_CHARS:
                raise SubmissionRejected(
                    "field_name_too_large",
                    f"JSON field name exceeds the {MAX_CELL_CHARS}-character limit",
                )
            _validate_json_limits(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_RECORDS:
            raise SubmissionRejected(
                "too_many_records",
                f"JSON array exceeds the {MAX_RECORDS}-record limit",
            )
        for child in value:
            _validate_json_limits(child, depth + 1)
    elif isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        raise SubmissionRejected(
            "cell_too_large",
            f"JSON string exceeds the {MAX_CELL_CHARS}-character limit",
        )


def parse_json_value(content: str, label: str = "JSON") -> Any:
    _validate_text_size(content, label)
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except SubmissionRejected:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise SubmissionRejected("invalid_json", f"{label} is invalid: {detail}") from exc
    _validate_json_limits(value)
    return value


def _strict_json_object(content: str, label: str) -> dict[str, Any]:
    value = parse_json_value(content, label)
    if not isinstance(value, dict):
        raise SubmissionRejected("invalid_json_object", f"{label} must be an object")
    return value


def _csv_records(content: str, delimiter: str | None = None) -> _ParsedDocument:
    _validate_text_size(content, "CSV source")
    if "\x00" in content:
        raise SubmissionRejected("invalid_csv", "CSV source contains a NUL byte")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(
                content[:4096], delimiters="".join(CSV_DELIMITERS)
            ).delimiter
        except csv.Error:
            delimiter = ","
    if delimiter not in CSV_DELIMITERS:
        raise SubmissionRejected("unsupported_delimiter", "unsupported CSV delimiter")
    reader = csv.reader(
        io.StringIO(content, newline=""), delimiter=delimiter, strict=True
    )
    try:
        headers = next(reader)
    except StopIteration:
        raise SubmissionRejected("missing_csv_header", "CSV source has no header row") from None
    except csv.Error as exc:
        raise SubmissionRejected("invalid_csv", f"invalid CSV header: {exc}") from exc
    if not headers or any(not header for header in headers):
        raise SubmissionRejected("empty_csv_header", "CSV source has an empty CSV header")
    if any(any(ord(character) < 32 for character in header) for header in headers):
        raise SubmissionRejected(
            "invalid_field_name", "CSV header contains a control character"
        )
    if len(headers) > MAX_FIELDS:
        raise SubmissionRejected(
            "too_many_fields", f"CSV exceeds the {MAX_FIELDS}-field limit"
        )
    if any(len(header) > MAX_CELL_CHARS for header in headers):
        raise SubmissionRejected("field_name_too_large", "CSV header exceeds the field-size limit")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise SubmissionRejected(
            "duplicate_csv_header", f"duplicate CSV header: {', '.join(duplicates)}"
        )

    records: list[dict[str, str]] = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if len(records) >= MAX_RECORDS:
                raise SubmissionRejected(
                    "too_many_records",
                    f"CSV exceeds the {MAX_RECORDS}-record limit",
                )
            if len(row) != len(headers):
                raise SubmissionRejected(
                    "invalid_csv_shape",
                    f"CSV row {row_number} has {len(row)} cells; expected {len(headers)}",
                )
            if any(len(cell) > MAX_CELL_CHARS for cell in row):
                raise SubmissionRejected(
                    "cell_too_large", f"CSV row {row_number} exceeds the cell-size limit"
                )
            records.append(dict(zip(headers, row, strict=True)))
    except csv.Error as exc:
        raise SubmissionRejected("invalid_csv", f"invalid CSV source: {exc}") from exc
    return _ParsedDocument(records=records, delimiter=delimiter, record_path=None)


def _list_record_paths(
    value: Any, prefix: str = ""
) -> Iterable[tuple[str | None, list[dict[str, Any]]]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        yield prefix or None, value
    elif isinstance(value, dict):
        for key, child in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else key
            yield from _list_record_paths(child, next_prefix)


def _select_json_path(value: Any, record_path: str | None) -> list[dict[str, Any]]:
    current = value
    for part in record_path.split(".") if record_path else []:
        if not isinstance(current, dict) or part not in current:
            raise SubmissionRejected(
                "record_path_missing", f"JSON record path not found: {record_path}"
            )
        current = current[part]
    if not isinstance(current, list) or not all(
        isinstance(item, dict) for item in current
    ):
        path = record_path or "<root>"
        raise SubmissionRejected(
            "record_path_invalid", f"JSON record path is not an object list: {path}"
        )
    return current


def _json_records(content: str, record_path: str | object | None) -> _ParsedDocument:
    value = parse_json_value(content, "JSON source")
    if record_path is _DISCOVER_PATH:
        candidates = list(_list_record_paths(value))
        if not candidates:
            raise SubmissionRejected("records_missing", "no JSON record list found")
        largest = max(len(records) for _, records in candidates)
        winners = [item for item in candidates if len(item[1]) == largest]
        if len(winners) != 1:
            paths = ", ".join(path or "<root>" for path, _ in winners)
            raise SubmissionRejected(
                "ambiguous_record_path", f"ambiguous record paths: {paths}"
            )
        selected_path, records = winners[0]
    else:
        selected_path = record_path
        records = _select_json_path(value, selected_path)
    return _ParsedDocument(records=records, delimiter=None, record_path=selected_path)


_DISCOVER_PATH = object()


def _parse_document(
    document: SourceDocument,
    *,
    delimiter: str | None = None,
    record_path: str | object | None = _DISCOVER_PATH,
) -> _ParsedDocument:
    if document.format == "csv":
        return _csv_records(document.content, delimiter)
    return _json_records(document.content, record_path)


def profile_document(document: SourceDocument) -> SourceProfile:
    parsed = _parse_document(document)
    names = sorted({key for record in parsed.records for key in record})
    fields: list[FieldProfile] = []
    for name in names:
        values = [record.get(name) for record in parsed.records]
        present = [value for value in values if value not in (None, "")]
        if not present:
            inferred_type = "null"
        elif all(isinstance(value, bool) for value in present):
            inferred_type = "boolean"
        elif all(
            isinstance(value, int) and not isinstance(value, bool) for value in present
        ):
            inferred_type = "integer"
        elif all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in present
        ):
            inferred_type = "number"
        else:
            inferred_type = "string"
        examples = sorted({str(value) for value in present})[:3]
        fields.append(
            FieldProfile(
                name=name,
                inferred_type=inferred_type,
                null_rate=(
                    round(sum(value in (None, "") for value in values) / len(values), 4)
                    if values
                    else 1.0
                ),
                distinct_count=len(
                    {json.dumps(value, sort_keys=True) for value in values}
                ),
                example_values=examples,
            )
        )
    return SourceProfile(
        format=document.format,
        delimiter=parsed.delimiter,
        record_path=parsed.record_path,
        row_count=len(parsed.records),
        fields=fields,
    )


def read_document_records(
    document: SourceDocument, config: PipelineConfig
) -> list[dict[str, Any]]:
    if document.format != config.format:
        raise SubmissionRejected(
            "source_format_mismatch",
            f"source format {document.format} does not match pipeline {config.format}",
        )
    if config.format == "csv":
        return _parse_document(document, delimiter=config.delimiter).records
    return _parse_document(document, record_path=config.record_path).records


def _source_value(record: dict[str, Any], source: str) -> Any:
    current: Any = record
    for part in source.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _transform_value(
    field: str, record: dict[str, Any], config: PipelineConfig
) -> Any:
    if field in config.joins:
        spec = config.joins[field]
        value = spec.separator.join(
            str(_source_value(record, source) or "").strip()
            for source in spec.sources
        ).strip()
    elif field in config.splits:
        spec = config.splits[field]
        parts = str(_source_value(record, spec.source) or "").split(spec.separator)
        value = parts[spec.index].strip() if len(parts) > spec.index else None
    else:
        value = _source_value(record, config.fields[field])

    if field in config.booleans and value not in (None, ""):
        spec = config.booleans[field]
        normalized = str(value).strip().casefold()
        true_values = {item.casefold() for item in spec.true_values}
        false_values = {item.casefold() for item in spec.false_values}
        if normalized in true_values:
            value = True
        elif normalized in false_values:
            value = False
        else:
            raise ValueError(f"{field} has unknown boolean value {value!r}")

    if field in config.date_formats and value not in (None, ""):
        value = datetime.strptime(str(value), config.date_formats[field]).date().isoformat()

    strategy = config.casts.get(field)
    if strategy == "string" and value is not None:
        value = str(value)
    elif strategy == "integer" and value not in (None, ""):
        value = int(str(value))
    elif strategy == "integer_grouped" and value not in (None, ""):
        normalized = str(value).replace(",", "")
        if not normalized.isascii() or not normalized.isdecimal():
            raise ValueError(f"{field} has invalid grouped integer value {value!r}")
        value = int(normalized)
    elif strategy == "integer_from_float" and value not in (None, ""):
        original = value
        try:
            decimal = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"{field} has invalid numeric value {original!r}") from exc
        if not decimal.is_finite() or decimal != decimal.to_integral_value():
            raise ValueError(
                f"{field} cannot losslessly convert {original!r} to integer"
            )
        value = int(decimal)
    elif strategy == "number" and value not in (None, ""):
        try:
            decimal = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"{field} has invalid numeric value {value!r}") from exc
        if not decimal.is_finite():
            raise ValueError(f"{field} has non-finite numeric value {value!r}")
        value = float(decimal)
    return value


def transform_document(
    document: SourceDocument, config: PipelineConfig
) -> list[dict[str, Any]]:
    records = read_document_records(document, config)
    return [
        {field: _transform_value(field, record, config) for field in config.fields}
        for record in records
    ]


def _transform_records(
    records: list[dict[str, Any]], config: PipelineConfig
) -> tuple[list[dict[str, Any]], list[str]]:
    transformed: list[dict[str, Any]] = []
    failed_fields: set[str] = set()
    for record in records:
        row: dict[str, Any] = {}
        for field in config.fields:
            try:
                row[field] = _transform_value(field, record, config)
            except (TypeError, ValueError, KeyError, IndexError):
                failed_fields.add(field)
        transformed.append(row)
    return transformed, sorted(failed_fields)


def transform_failure_fields(
    document: SourceDocument, config: PipelineConfig
) -> list[str]:
    try:
        _, failed_fields = _transform_records(read_document_records(document, config), config)
    except (SubmissionRejected, TypeError, ValueError, KeyError, IndexError):
        return []
    return failed_fields


def run_document_contracts(
    document: SourceDocument,
    config: PipelineConfig,
    contract: Contract,
) -> tuple[list[dict[str, Any]], list[CheckResult]]:
    raw = _parse_document(document)
    source_names = {key for record in raw.records for key in record}
    checks = [
        CheckResult(
            name=f"source:{field}",
            passed=field in source_names,
            detail=f"present={str(field in source_names).lower()}",
        )
        for field in contract.source_fields
    ]
    try:
        records = transform_document(document, config)
    except (SubmissionRejected, TypeError, ValueError, KeyError, IndexError) as exc:
        return [], [*checks, CheckResult(name="transform", passed=False, detail=str(exc))]

    checks.append(
        CheckResult(
            name="minimum_rows",
            passed=len(records) >= contract.min_rows,
            detail=f"observed={len(records)} expected>={contract.min_rows}",
        )
    )
    for field in contract.required:
        missing = sum(record.get(field) in (None, "") for record in records)
        checks.append(
            CheckResult(
                name=f"required:{field}",
                passed=missing == 0 and bool(records),
                detail=f"missing={missing} rows={len(records)}",
            )
        )

    def is_date(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    validators = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int)
        and not isinstance(value, bool),
        "number": lambda value: (
            isinstance(value, int) and not isinstance(value, bool)
        )
        or (isinstance(value, float) and math.isfinite(value)),
        "boolean": lambda value: isinstance(value, bool),
        "date": is_date,
    }
    for field, expected_type in contract.types.items():
        invalid = sum(
            not validators[expected_type](record.get(field)) for record in records
        )
        checks.append(
            CheckResult(
                name=f"type:{field}",
                passed=invalid == 0 and bool(records),
                detail=f"invalid={invalid} expected={expected_type}",
            )
        )

    keys = [record.get(contract.unique_key) for record in records]
    invalid_keys = sum(
        key is None or not isinstance(key, (str, int, float, bool)) for key in keys
    )
    distinct_keys = len(set(keys)) if invalid_keys == 0 else 0
    checks.append(
        CheckResult(
            name=f"unique:{contract.unique_key}",
            passed=invalid_keys == 0 and len(keys) == distinct_keys and bool(records),
            detail=f"rows={len(keys)} distinct={distinct_keys} invalid={invalid_keys}",
        )
    )
    return records, checks


def _records_by_key(
    records: list[dict[str, Any]], unique_key: str
) -> dict[str | int | float | bool, dict[str, Any]]:
    return {record[unique_key]: record for record in records}


def run_case_contracts(
    case: RepairCase, config: PipelineConfig
) -> tuple[list[dict[str, Any]], list[CheckResult]]:
    after_records, checks = run_document_contracts(case.after, config, case.contract)
    if not after_records or any(check.name == "transform" for check in checks):
        return after_records, checks
    try:
        before_records = transform_document(case.before, case.pipeline)
        before_by_key = _records_by_key(before_records, case.contract.unique_key)
        after_by_key = _records_by_key(after_records, case.contract.unique_key)
    except (KeyError, TypeError, ValueError, SubmissionRejected) as exc:
        return after_records, [
            *checks,
            CheckResult(name="row_policy", passed=False, detail=str(exc)),
        ]

    before_keys = set(before_by_key)
    after_keys = set(after_by_key)
    added = after_keys - before_keys
    removed = before_keys - after_keys
    row_policy_passed = (
        not added and not removed
        if case.contract.row_policy == "same_keys"
        else not removed
    )
    checks.append(
        CheckResult(
            name="row_policy",
            passed=row_policy_passed,
            detail=(
                f"before={len(before_keys)} after={len(after_keys)} "
                f"added={len(added)} removed={len(removed)}"
            ),
        )
    )
    common = before_keys & after_keys
    changed = sum(
        before_by_key[key].get(field) != after_by_key[key].get(field)
        for key in common
        for field in case.contract.preserve_values
    )
    checks.append(
        CheckResult(
            name="preserve_values",
            passed=changed == 0,
            detail=(
                f"keys={len(common)} fields={len(case.contract.preserve_values)} "
                f"changed={changed}"
            ),
        )
    )
    return after_records, checks


def _safe_label(value: str) -> str:
    label = value.strip()
    if not label or any(ord(character) < 32 for character in label):
        raise SubmissionRejected("label_invalid", "label contains no usable text")
    return label


def parse_submission(
    value: CustomRunSubmission, *, case_id: str = "unassigned"
) -> RepairCase:
    if _text_size(value.model_dump_json()) > MAX_REQUEST_BYTES:
        raise SubmissionRejected("body_too_large", "submission exceeds 5 MiB")
    try:
        pipeline = PipelineConfig.model_validate(
            _strict_json_object(value.pipeline_json, "pipeline")
        )
    except SubmissionRejected:
        raise
    except ValidationError as exc:
        raise SubmissionRejected("pipeline_invalid", str(exc)) from exc
    try:
        contract = Contract.model_validate(
            _strict_json_object(value.contract_json, "contract")
        )
    except SubmissionRejected:
        raise
    except ValidationError as exc:
        raise SubmissionRejected("contract_invalid", str(exc)) from exc

    case = RepairCase(
        id=case_id,
        title=_safe_label(value.label),
        before=value.before,
        after=value.after,
        pipeline=pipeline,
        contract=contract,
    )
    profile_document(case.before)
    profile_document(case.after)
    _, baseline_checks = run_document_contracts(
        case.before, case.pipeline, case.contract
    )
    failed = [check for check in baseline_checks if not check.passed]
    if failed:
        first = failed[0]
        raise SubmissionRejected(
            "invalid_baseline", f"{first.name}: {first.detail}"
        )
    return case


def inspect_case(case: RepairCase) -> DriftReport:
    before = profile_document(case.before)
    after = profile_document(case.after)
    before_fields = {field.name: field for field in before.fields}
    after_fields = {field.name: field for field in after.fields}
    _, checks = run_case_contracts(case, case.pipeline)
    failure = "; ".join(check.name for check in checks if not check.passed)
    return DriftReport(
        scenario_id=case.id,
        title=case.title,
        current_pipeline=case.pipeline,
        contract=case.contract,
        before=before,
        after=after,
        added_fields=sorted(after_fields.keys() - before_fields.keys()),
        removed_fields=sorted(before_fields.keys() - after_fields.keys()),
        type_changes={
            name: [
                before_fields[name].inferred_type,
                after_fields[name].inferred_type,
            ]
            for name in before_fields.keys() & after_fields.keys()
            if before_fields[name].inferred_type != after_fields[name].inferred_type
        },
        current_failure=failure or "No contract failure observed",
    )
