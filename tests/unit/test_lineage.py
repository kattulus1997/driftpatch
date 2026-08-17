from __future__ import annotations

import pytest

from app.benchmark import load_scenario, scenario_case
from app.case_data import inspect_case
from app.lineage import SemanticLineageRanker
from app.repairs import build_candidate_catalogue


class FakeEmbedder:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.texts: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        if self.fails:
            raise RuntimeError("embedding service unavailable")
        return [1.0, 0.0] if "legal" not in text else [0.0, 1.0]


class IndifferentEmbedder:
    async def embed(self, _text: str) -> list[float]:
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_lineage_scores_only_structural_rename_labels() -> None:
    case = scenario_case(load_scenario("custom-ambiguous-alias"))
    catalogue = build_candidate_catalogue(case, inspect_case(case))
    embedder = FakeEmbedder()

    scores = await SemanticLineageRanker(embedder).score(case, catalogue)

    rename_ids = {
        item.id
        for item in catalogue
        if item.step.operation == "update_field_sources"
    }
    assert scores.keys() <= rename_ids
    assert scores
    assert len(scores) == 1
    serialized = " ".join(embedder.texts)
    assert "output field" in serialized
    assert "observed replacement field" in serialized
    assert "Ada" not in serialized
    assert "Grace" not in serialized


@pytest.mark.asyncio
async def test_lineage_failure_is_advisory() -> None:
    case = scenario_case(load_scenario("custom-ambiguous-alias"))
    catalogue = build_candidate_catalogue(case, inspect_case(case))

    scores = await SemanticLineageRanker(FakeEmbedder(fails=True)).score(
        case, catalogue
    )

    assert scores == {}


@pytest.mark.asyncio
async def test_lineage_abstains_when_candidates_are_indistinguishable() -> None:
    case = scenario_case(load_scenario("custom-ambiguous-alias"))
    catalogue = build_candidate_catalogue(case, inspect_case(case))

    scores = await SemanticLineageRanker(IndifferentEmbedder()).score(
        case, catalogue
    )

    assert scores == {}
