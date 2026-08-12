from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import datetime
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


def load_scenarios() -> list[Scenario]:
    payload = json.loads((BENCHMARK_ROOT / "scenarios.json").read_text())
    return [Scenario.model_validate(item) for item in payload["scenarios"]]


def load_scenario(scenario_id: str) -> Scenario:
    try:
        return next(item for item in load_scenarios() if item.id == scenario_id)
    except StopIteration as exc:
        raise ValueError(f"Unknown scenario: {scenario_id}") from exc


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
        sample = path.read_text()[:4096]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter
        except csv.Error:
            delimiter = ","
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle, delimiter=delimiter)), delimiter, None
    if path.suffix == ".json":
        payload = json.loads(path.read_text())
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
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle, delimiter=config.delimiter))
    return _select_path(json.loads(path.read_text()), config.record_path)


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
    elif strategy == "integer_from_float" and value not in (None, ""):
        value = int(float(str(value)))
    elif strategy == "number" and value not in (None, ""):
        value = float(str(value))
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
    try:
        records = transform(path, config)
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        return [], [CheckResult(name="transform", passed=False, detail=str(exc))]

    checks = [
        CheckResult(
            name="minimum_rows",
            passed=len(records) >= contract.min_rows,
            detail=f"observed={len(records)} expected>={contract.min_rows}",
        )
    ]
    for field in contract.required:
        missing = sum(record.get(field) in (None, "") for record in records)
        checks.append(
            CheckResult(
                name=f"required:{field}",
                passed=missing == 0 and bool(records),
                detail=f"missing={missing} rows={len(records)}",
            )
        )

    validators = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float))
        and not isinstance(value, bool),
        "boolean": lambda value: isinstance(value, bool),
        "date": lambda value: isinstance(value, str)
        and len(value) == 10
        and value[4] == "-"
        and value[7] == "-",
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
    checks.append(
        CheckResult(
            name=f"unique:{contract.unique_key}",
            passed=len(keys) == len(set(keys)) and None not in keys and bool(records),
            detail=f"rows={len(keys)} distinct={len(set(keys))}",
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
