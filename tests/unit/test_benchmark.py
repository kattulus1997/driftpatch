from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent import apply_plan, inspect_incident, validate_plan
from app.benchmark import (
    load_scenarios,
    run_contracts,
    scenario_source,
)
from app.repairs import apply_repair_plan
from app.schemas import ApplyResult, Contract, IncidentInput, PipelineConfig, RepairPlan


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda item: item.id)
def test_benchmark_incident_and_expected_terminal_state(scenario) -> None:
    _, before_checks = run_contracts(
        scenario_source(scenario, "before"), scenario.pipeline, scenario.contract
    )
    assert all(check.passed for check in before_checks)

    _, broken_checks = run_contracts(
        scenario_source(scenario, "after"), scenario.pipeline, scenario.contract
    )
    if scenario.expected_status == "unchanged":
        assert all(check.passed for check in broken_checks)
    else:
        assert any(not check.passed for check in broken_checks)

    patched = apply_repair_plan(scenario.pipeline, scenario.expected_plan)
    _, repaired_checks = run_contracts(
        scenario_source(scenario, "after"), patched, scenario.contract
    )
    if scenario.expected_status == "unchanged":
        assert scenario.expected_plan.operation == "no_change"
        assert patched == scenario.pipeline
        assert all(check.passed for check in repaired_checks)
    elif scenario.expected_status == "repaired":
        assert patched != scenario.pipeline
        assert all(check.passed for check in repaired_checks)
    else:
        assert scenario.expected_plan.operation == "escalate"
        assert patched == scenario.pipeline
        assert any(not check.passed for check in repaired_checks)


@pytest.mark.parametrize(
    "scenario", load_scenarios(suite="external"), ids=lambda item: item.id
)
def test_external_corpus_has_a_predeclared_deterministic_terminal_state(scenario) -> None:
    _, before_checks = run_contracts(
        scenario_source(scenario, "before"), scenario.pipeline, scenario.contract
    )
    _, current_checks = run_contracts(
        scenario_source(scenario, "after"), scenario.pipeline, scenario.contract
    )
    patched = apply_repair_plan(scenario.pipeline, scenario.expected_plan)
    _, patched_checks = run_contracts(
        scenario_source(scenario, "after"), patched, scenario.contract
    )

    assert all(check.passed for check in before_checks)
    if scenario.expected_status == "unchanged":
        assert scenario.expected_plan.operation == "no_change"
        assert all(check.passed for check in current_checks)
        assert patched == scenario.pipeline
    elif scenario.expected_status == "repaired":
        assert any(not check.passed for check in current_checks)
        assert all(check.passed for check in patched_checks)
        assert patched != scenario.pipeline
    else:
        assert scenario.expected_plan.operation == "escalate"
        assert any(not check.passed for check in current_checks)
        assert any(not check.passed for check in patched_checks)


def test_repair_plan_cannot_add_an_output_field() -> None:
    config = PipelineConfig(format="csv", fields={"id": "id"})
    plan = RepairPlan(
        operation="update_field_sources",
        field_sources=[{"output_field": "secret", "source_field": "value"}],
        confidence=1,
        evidence=["test"],
        rationale="test",
    )
    with pytest.raises(ValueError, match="existing output fields"):
        apply_repair_plan(config, plan)


def test_incident_input_accepts_task_attributes() -> None:
    incident = IncidentInput.model_validate(
        {
            "scenario_id": "column-rename",
            "attributes": {"event_id": "evt-1", "trigger": "cloud-tasks"},
        }
    )
    assert incident.resolved_scenario_id == "column-rename"


def test_inspection_propagates_only_non_secret_execution_state() -> None:
    incident = IncidentInput.model_validate(
        {
            "scenario_id": "column-rename",
            "attributes": {
                "event_id": "event-1",
                "trigger": "cloud-tasks",
            },
        }
    )

    event = inspect_incident(incident)

    assert event.actions.state_delta["scenario_id"] == "column-rename"
    assert event.actions.state_delta["event_id"] == "event-1"
    assert event.actions.state_delta["trigger"] == "cloud-tasks"
    assert "claim_token" not in event.actions.state_delta
    assert event.actions.state_delta["drift_report"]["scenario_id"] == "column-rename"


@pytest.mark.parametrize(
    "scenario",
    [item for suite in ("demo", "external") for item in load_scenarios(suite=suite)],
    ids=lambda item: item.id,
)
def test_expected_decisions_pass_the_full_deterministic_gate(scenario) -> None:
    patched = apply_repair_plan(scenario.pipeline, scenario.expected_plan)
    result = validate_plan(
        ApplyResult(
            scenario_id=scenario.id,
            plan=scenario.expected_plan,
            patched_pipeline=patched,
            changed=patched != scenario.pipeline,
        )
    ).output

    assert result.status == scenario.expected_status
    assert result.evidence_complete
    if result.status in {"unchanged", "repaired"}:
        assert all(check.passed for check in result.checks)


def test_healthy_baseline_rejects_a_redundant_mutation() -> None:
    scenario = next(item for item in load_scenarios() if item.id == "compatible-addition")
    plan = RepairPlan(
        operation="set_cast",
        field="name",
        strategy="string",
        confidence=1,
        evidence=["proposal"],
        rationale="proposal",
    )
    patched = apply_repair_plan(scenario.pipeline, plan)
    result = validate_plan(
        ApplyResult(
            scenario_id=scenario.id,
            plan=plan,
            patched_pipeline=patched,
            changed=True,
        )
    ).output

    assert result.status == "failed"
    assert not next(check for check in result.checks if check.name == "baseline_failure").passed


def test_invalid_operation_parameters_fail_closed_with_terminal_evidence() -> None:
    plan = RepairPlan(
        operation="set_cast",
        field="unknown",
        strategy="integer",
        confidence=1,
        evidence=["proposal"],
        rationale="proposal",
    )
    context = type(
        "ContextStub", (), {"state": {"scenario_id": "integer-decimal"}}
    )()

    applied = apply_plan(plan, context).output
    result = validate_plan(applied).output

    assert applied.application_error.startswith("authorization rejected:")
    assert not applied.changed
    assert result.status == "failed"
    authorization = next(
        check for check in result.checks if check.name == "operation_authorized"
    )
    assert not authorization.passed
    assert authorization.detail.startswith("application rejected:")


def test_model_cannot_escalate_when_evidence_supports_a_bounded_repair() -> None:
    plan = RepairPlan(
        operation="escalate",
        confidence=1,
        evidence=["model declined"],
        rationale="model declined",
    )
    context = type("ContextStub", (), {"state": {"scenario_id": "column-rename"}})()

    applied = apply_plan(plan, context).output
    result = validate_plan(applied).output

    assert applied.application_error == (
        "authorization rejected: observations support a bounded repair "
        "that must be evaluated first"
    )
    assert result.status == "failed"
    assert not next(
        check for check in result.checks if check.name == "operation_authorized"
    ).passed


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("duplicate.csv", "id,id\n1,2\n", "duplicate CSV header"),
        ("duplicate.json", '{"items":[{"id":1,"id":2}]}', "duplicate JSON key"),
    ],
)
def test_ambiguous_source_keys_fail_closed(
    tmp_path: Path, name: str, content: str, message: str
) -> None:
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")
    config = PipelineConfig(
        format="csv" if name.endswith("csv") else "json",
        record_path=None if name.endswith("csv") else "items",
        fields={"id": "id"},
        casts={"id": "integer"},
    )
    contract = Contract(required=["id"], types={"id": "integer"}, unique_key="id")

    with pytest.raises(ValueError, match=message):
        run_contracts(source, config, contract)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_number_cast_rejects_non_finite_values(tmp_path: Path, value: str) -> None:
    source = tmp_path / "source.csv"
    source.write_text(f"id,value\n1,{value}\n", encoding="utf-8")
    config = PipelineConfig(
        format="csv",
        fields={"id": "id", "value": "value"},
        casts={"id": "integer", "value": "number"},
    )
    contract = Contract(
        required=["id", "value"],
        types={"id": "integer", "value": "number"},
        unique_key="id",
    )

    records, checks = run_contracts(source, config, contract)

    assert records == []
    assert checks[-1].name == "transform"
    assert "non-finite" in checks[-1].detail


def test_integer_from_float_rejects_non_integral_values(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("id,count\n1,3.5\n", encoding="utf-8")
    config = PipelineConfig(
        format="csv",
        fields={"id": "id", "count": "count"},
        casts={"id": "integer", "count": "integer_from_float"},
    )
    contract = Contract(
        required=["id", "count"],
        types={"id": "integer", "count": "integer"},
        unique_key="id",
    )

    records, checks = run_contracts(source, config, contract)

    assert records == []
    assert [check.model_dump() for check in checks] == [
        {
            "name": "transform",
            "passed": False,
            "detail": "count cannot losslessly convert '3.5' to integer",
        }
    ]


def test_contract_can_require_a_source_header_even_when_values_are_null(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text("id,optional\n1,\n2,\n", encoding="utf-8")
    config = PipelineConfig(format="csv", fields={"id": "id"}, casts={"id": "integer"})
    contract = Contract(
        required=["id"],
        source_fields=["optional"],
        types={"id": "integer"},
        unique_key="id",
    )

    _, present_checks = run_contracts(source, config, contract)
    source.write_text("id\n1\n2\n", encoding="utf-8")
    _, missing_checks = run_contracts(source, config, contract)

    assert next(check for check in present_checks if check.name == "source:optional").passed
    assert not next(
        check for check in missing_checks if check.name == "source:optional"
    ).passed


def test_grouped_integer_cast_rejects_masked_values(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text('id,count\n1,"27,424"\n', encoding="utf-8")
    config = PipelineConfig(
        format="csv",
        fields={"id": "id", "count": "count"},
        casts={"id": "integer", "count": "integer_grouped"},
    )
    contract = Contract(
        required=["id", "count"],
        types={"id": "integer", "count": "integer"},
        unique_key="id",
    )

    records, valid_checks = run_contracts(source, config, contract)
    source.write_text("id,count\n1,Masked\n", encoding="utf-8")
    masked_records, masked_checks = run_contracts(source, config, contract)

    assert records[0]["count"] == 27424
    assert all(check.passed for check in valid_checks)
    assert masked_records == []
    assert masked_checks[-1].name == "transform"
    assert masked_checks[-1].passed is False


def test_composite_unique_key_fails_closed_without_type_error(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text('{"items":[{"id":{"part":1}}]}', encoding="utf-8")
    config = PipelineConfig(format="json", record_path="items", fields={"id": "id"})
    contract = Contract(required=["id"], types={"id": "string"}, unique_key="id")

    _, checks = run_contracts(source, config, contract)

    unique = next(check for check in checks if check.name == "unique:id")
    assert unique.passed is False
    assert unique.detail == "rows=1 distinct=0 invalid=1"


@pytest.mark.parametrize(
    "payload",
    [
        {"evidence": ["e"] * 17},
        {"rationale": "r" * 1025},
        {"sources": [f"field-{index}" for index in range(17)]},
        {"split_fields": [{"output_field": "x" * 129, "index": 0}]},
        {"split_fields": [{"output_field": "latitude", "index": 16}]},
    ],
)
def test_repair_plan_rejects_unbounded_model_output(payload: dict) -> None:
    values = {
        "operation": "escalate",
        "confidence": 1,
        "evidence": ["observed failure"],
        "rationale": "bounded decision",
        **payload,
    }

    with pytest.raises(ValidationError):
        RepairPlan.model_validate(values)
