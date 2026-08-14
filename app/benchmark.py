from __future__ import annotations

from pathlib import Path
from typing import Any

from .case_data import (
    MAX_SOURCE_BYTES,
    RepairCase,
    SubmissionRejected,
    inspect_case,
    parse_json_value,
    profile_document,
    read_document_records,
    run_document_contracts,
    transform_document,
)
from .schemas import (
    CheckResult,
    Contract,
    DriftReport,
    PipelineConfig,
    Scenario,
    SourceDocument,
    SourceProfile,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = PROJECT_ROOT / "benchmark"


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise SubmissionRejected(
            "source_too_large",
            f"{path.name} exceeds the {MAX_SOURCE_BYTES}-byte source limit",
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SubmissionRejected(
            "source_encoding", f"{path.name} is not valid UTF-8"
        ) from exc


def _json_payload(path: Path) -> Any:
    return parse_json_value(_read_text(path), path.name)


def load_scenarios(*, suite: str = "demo") -> list[Scenario]:
    if suite == "demo":
        path = BENCHMARK_ROOT / "scenarios.json"
    elif suite == "external":
        path = BENCHMARK_ROOT / "external" / "manifest.json"
    elif suite == "custom":
        path = BENCHMARK_ROOT / "custom" / "manifest.json"
    else:
        raise ValueError("suite must be demo, external or custom")
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
    for suite in ("demo", "external", "custom"):
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


def source_document(path: Path) -> SourceDocument:
    suffix = path.suffix.removeprefix(".")
    if suffix not in {"csv", "json"}:
        raise ValueError(f"Unsupported source format: {path.suffix}")
    return SourceDocument(format=suffix, content=_read_text(path))


def scenario_case(scenario: Scenario) -> RepairCase:
    return RepairCase(
        id=scenario.id,
        title=scenario.title,
        before=source_document(scenario_source(scenario, "before")),
        after=source_document(scenario_source(scenario, "after")),
        pipeline=scenario.pipeline,
        contract=scenario.contract,
    )


def profile_source(path: Path) -> SourceProfile:
    return profile_document(source_document(path))


def read_records(path: Path, config: PipelineConfig) -> list[dict[str, Any]]:
    return read_document_records(source_document(path), config)


def transform(path: Path, config: PipelineConfig) -> list[dict[str, Any]]:
    return transform_document(source_document(path), config)


def run_contracts(
    path: Path, config: PipelineConfig, contract: Contract
) -> tuple[list[dict[str, Any]], list[CheckResult]]:
    return run_document_contracts(source_document(path), config, contract)


def inspect_scenario(scenario: Scenario) -> DriftReport:
    return inspect_case(scenario_case(scenario))
