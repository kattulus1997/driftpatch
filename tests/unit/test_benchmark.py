import pytest

from app.benchmark import (
    load_scenarios,
    run_contracts,
    scenario_source,
)
from app.repairs import apply_repair_plan
from app.schemas import IncidentInput, PipelineConfig, RepairPlan


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda item: item.id)
def test_benchmark_incident_and_expected_terminal_state(scenario) -> None:
    _, before_checks = run_contracts(
        scenario_source(scenario, "before"), scenario.pipeline, scenario.contract
    )
    assert all(check.passed for check in before_checks)

    _, broken_checks = run_contracts(
        scenario_source(scenario, "after"), scenario.pipeline, scenario.contract
    )
    assert any(not check.passed for check in broken_checks)

    patched = apply_repair_plan(scenario.pipeline, scenario.expected_plan)
    _, repaired_checks = run_contracts(
        scenario_source(scenario, "after"), patched, scenario.contract
    )
    if scenario.expected_status == "repaired":
        assert patched != scenario.pipeline
        assert all(check.passed for check in repaired_checks)
    else:
        assert scenario.expected_plan.operation == "escalate"
        assert patched == scenario.pipeline
        assert any(not check.passed for check in repaired_checks)


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


def test_incident_input_accepts_native_pubsub_envelope() -> None:
    incident = IncidentInput.model_validate(
        {
            "data": {"scenario_id": "column-rename"},
            "attributes": {"event_id": "evt-1", "trigger": "pubsub"},
        }
    )
    assert incident.resolved_scenario_id == "column-rename"
