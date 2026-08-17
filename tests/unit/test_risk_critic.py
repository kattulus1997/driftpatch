from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.benchmark import load_scenario, scenario_case
from app.case_data import inspect_case
from app.repairs import build_candidate_catalogue
from app.risk_critic import VertexRiskCritic


class FakeModels:
    def __init__(self, text: str | None = None, fails: bool = False) -> None:
        self.text = text
        self.fails = fails
        self.contents = ""

    async def generate_content(self, **kwargs):
        self.contents = kwargs["contents"]
        if self.fails:
            raise RuntimeError("unavailable")
        return SimpleNamespace(text=self.text)


def _critic(models: FakeModels) -> VertexRiskCritic:
    critic = object.__new__(VertexRiskCritic)
    critic._client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return critic


@pytest.mark.asyncio
async def test_risk_critic_sees_structure_without_row_values() -> None:
    case = scenario_case(load_scenario("custom-ambiguous-date"))
    report = inspect_case(case)
    catalogue = build_candidate_catalogue(case, report)
    models = FakeModels('{"decision":"escalate"}')

    assert await _critic(models).confirms_escalation(report, catalogue)
    assert "example_values\":[]" in models.contents
    assert "candidate" in models.contents


@pytest.mark.asyncio
async def test_risk_critic_fails_open_to_the_main_planner() -> None:
    case = scenario_case(load_scenario("custom-ambiguous-date"))
    report = inspect_case(case)
    catalogue = build_candidate_catalogue(case, report)

    assert not await _critic(FakeModels(fails=True)).confirms_escalation(
        report, catalogue
    )
    assert not await _critic(FakeModels("not-json")).confirms_escalation(
        report, catalogue
    )
