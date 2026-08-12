import pytest

from app.ledger import list_runs, save_run
from app.schemas import RepairPlan, ValidationResult


@pytest.mark.asyncio
async def test_local_ledger_is_idempotent_by_event_id() -> None:
    result = ValidationResult(
        scenario_id="column-rename",
        status="repaired",
        plan=RepairPlan(
            operation="update_field_sources",
            field_sources=[
                {"output_field": "name", "source_field": "full_name"}
            ],
            confidence=1,
            evidence=["name became full_name"],
            rationale="Observed rename",
        ),
        checks=[],
        transformed_rows=3,
        evidence_complete=True,
        summary="repaired",
    )
    await save_run(result, event_id="same-event", trigger="test")
    await save_run(result, event_id="same-event", trigger="test")
    matching = [item for item in await list_runs() if item["id"] == "same-event"]
    assert len(matching) == 1
