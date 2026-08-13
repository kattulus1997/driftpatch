from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .schemas import (
    CheckResult,
    Contract,
    DriftReport,
    FieldProfile,
    PipelineConfig,
    Scenario,
    SourceProfile,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = PROJECT_ROOT / "benchmark"
MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_RECORDS = 20_000
MAX_FIELDS = 256
MAX_CELL_CHARS = 100_000
MAX_JSON_DEPTH = 32


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"{path.name} exceeds the {MAX_SOURCE_BYTES}-byte source limit")
    return path.read_text(encoding="utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _json_payload(path: Path) -> Any:
    payload = json.loads(
        _read_text(path),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_json,
    )

    def validate(value: Any, depth: int = 0) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"JSON exceeds the {MAX_JSON_DEPTH}-level depth limit")
        if isinstance(value, dict):
            if len(value) > MAX_FIELDS:
                raise ValueError(f"JSON object exceeds the {MAX_FIELDS}-field limit")
            for child in value.values():
                validate(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > MAX_RECORDS:
                raise ValueError(f"JSON array exceeds the {MAX_RECORDS}-record limit")
            for child in value:
                validate(child, depth + 1)
        elif isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            raise ValueError(f"JSON string exceeds the {MAX_CELL_CHARS}-character limit")

    validate(payload)
    return payload


def _csv_records(path: Path, delimiter: str | None = None) -> tuple[list[dict[str, str]], str]:
    text = _read_text(path)
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;|\t").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        raise ValueError(f"{path.name} has no header row") from None
    if not headers or any(not header for header in headers):
        raise ValueError(f"{path.name} has an empty CSV header")
    if len(headers) > MAX_FIELDS:
        raise ValueError(f"CSV exceeds the {MAX_FIELDS}-field limit")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise ValueError(f"duplicate CSV header: {', '.join(duplicates)}")

    records = []
    for index, row in enumerate(reader, start=2):
        if index - 1 > MAX_RECORDS:
            raise ValueError(f"CSV exceeds the {MAX_RECORDS}-record limit")
        if len(row) != len(headers):
            raise ValueError(f"CSV row {index} has {len(row)} cells; expected {len(headers)}")
        if any(len(cell) > MAX_CELL_CHARS for cell in row):
            raise ValueError(f"CSV row {index} exceeds the cell-size limit")
        records.append(dict(zip(headers, row, strict=True)))
    return records, delimiter


def load_scenarios(*, suite: str = "demo") -> list[Scenario]:
    if suite == "demo":
        path = BENCHMARK_ROOT / "scenarios.json"
    elif suite == "external":
        path = BENCHMARK_ROOT / "external" / "manifest.json"
    else:
        raise ValueError("suite must be demo or external")
    payload = _json_payload(path)
    key = "scenarios" if suite == "demo" else "cases"
    items = payload[key]
    if suite == "external":
        items = [
            {
                "id": item["id"],
                "title": f"{item['publisher']}: {item['dataset']}",
                "before": f"external/{item['before']}",
                "after": f"external/{item['after']}",
                "pipeline": item["pipeline"],
                "contract": item["contract"],
                "expected_status": item["expected_status"],
                "expected_plan": item["expected_plan"],
            }
            for item in items
        ]
    return [Scenario.model_validate(item) for item in items]


def load_scenario(scenario_id: str) -> Scenario:
    for suite in ("demo", "external"):
        for item in load_scenarios(suite=suite):
            if item.id == scenario_id:
                return item
    raise ValueError(f"Unknown scenario: {scenario_id}")


def scenario_source(scenario: Scenario, version: str) -> Path:
    if version not in {"before", "after"}:
        raise ValueError("version must be before or after")
    relative = Path(getattr(scenario, version))
    path = (BENCHMARK_ROOT / relative).resolve()
    if BENCHMARK_ROOT.resolve() not in path.parents:
        raise ValueError("Scenario source escaped the benchmark root")
    return path


def _list_paths(value: Any, prefix: str = "") -> Iterable[tuple[str, list[dict[str, Any]]]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        yield prefix, value
    if isinstance(value, dict):
        for key, child in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else key
            yield from _list_paths(child, next_prefix)


def _raw_records(path: Path) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if path.suffix == ".csv":
        records, delimiter = _csv_records(path)
        return records, delimiter, None
    if path.suffix == ".json":
        payload = _json_payload(path)
        candidates = list(_list_paths(payload))
        if not candidates:
            raise ValueError(f"No record list found in {path.name}")
        record_path, records = max(candidates, key=lambda item: len(item[1]))
        return records, None, record_path or None
    raise ValueError(f"Unsupported source format: {path.suffix}")


def _infer_type(values: list[Any]) -> str:
    present = [value for value in values if value not in (None, "")]
    if not present:
        return "null"
    if all(isinstance(value, bool) for value in present):
        return "boolean"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return "integer"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return "number"
    return "string"


def profile_source(path: Path) -> SourceProfile:
    records, delimiter, record_path = _raw_records(path)
    names = sorted({key for record in records for key in record})
    fields = []
    for name in names:
        values = [record.get(name) for record in records]
        nulls = sum(value in (None, "") for value in values)
        fields.append(
            FieldProfile(
                name=name,
                inferred_type=_infer_type(values),
                null_rate=round(nulls / len(records), 4) if records else 1.0,
                distinct_count=len({json.dumps(value, sort_keys=True) for value in values}),
                example_values=list(dict.fromkeys(str(value) for value in values if value not in (None, "")))[:3],
            )
        )
    return SourceProfile(
        format=path.suffix.removeprefix("."),
        delimiter=delimiter,
        record_path=record_path,
        row_count=len(records),
        fields=fields,
    )


def _select_path(payload: Any, record_path: str | None) -> list[dict[str, Any]]:
    current = payload
    for part in record_path.split(".") if record_path else []:
        if not isinstance(current, dict) or part not in current:
            return []
        current = current[part]
    return current if isinstance(current, list) else []


def read_records(path: Path, config: PipelineConfig) -> list[dict[str, Any]]:
    if config.format == "csv":
        records, _ = _csv_records(path, config.delimiter)
        return records
    return _select_path(_json_payload(path), config.record_path)


def _source_value(record: dict[str, Any], source: str) -> Any:
    current: Any = record
    for part in source.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _transform_value(field: str, record: dict[str, Any], config: PipelineConfig) -> Any:
    if field in config.joins:
        spec = config.joins[field]
        value = spec.separator.join(
            str(_source_value(record, source) or "").strip() for source in spec.sources
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
        parsed = datetime.strptime(str(value), config.date_formats[field])
        value = parsed.date().isoformat()

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


def transform(path: Path, config: PipelineConfig) -> list[dict[str, Any]]:
    records = read_records(path, config)
    return [
        {field: _transform_value(field, record, config) for field in config.fields}
        for record in records
    ]


def run_contracts(
    path: Path, config: PipelineConfig, contract: Contract
) -> tuple[list[dict[str, Any]], list[CheckResult]]:
    source_records, _, _ = _raw_records(path)
    source_names = {key for record in source_records for key in record}
    checks = [
        CheckResult(
            name=f"source:{field}",
            passed=field in source_names,
            detail=f"present={str(field in source_names).lower()}",
        )
        for field in contract.source_fields
    ]
    try:
        records = transform(path, config)
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        return [], [
            *checks,
            CheckResult(name="transform", passed=False, detail=str(exc))
        ]

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
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
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
            passed=(
                invalid_keys == 0 and len(keys) == distinct_keys and bool(records)
            ),
            detail=(
                f"rows={len(keys)} distinct={distinct_keys} invalid={invalid_keys}"
            ),
        )
    )
    return records, checks


def inspect_scenario(scenario: Scenario) -> DriftReport:
    before_path = scenario_source(scenario, "before")
    after_path = scenario_source(scenario, "after")
    before = profile_source(before_path)
    after = profile_source(after_path)
    before_fields = {field.name: field for field in before.fields}
    after_fields = {field.name: field for field in after.fields}
    _, failed_checks = run_contracts(after_path, scenario.pipeline, scenario.contract)
    failure = "; ".join(
        f"{check.name}: {check.detail}" for check in failed_checks if not check.passed
    )
    return DriftReport(
        scenario_id=scenario.id,
        title=scenario.title,
        current_pipeline=scenario.pipeline,
        contract=scenario.contract,
        before=before,
        after=after,
        added_fields=sorted(after_fields.keys() - before_fields.keys()),
        removed_fields=sorted(before_fields.keys() - after_fields.keys()),
        type_changes={
            name: [before_fields[name].inferred_type, after_fields[name].inferred_type]
            for name in before_fields.keys() & after_fields.keys()
            if before_fields[name].inferred_type != after_fields[name].inferred_type
        },
        current_failure=failure or "No contract failure observed",
    )
