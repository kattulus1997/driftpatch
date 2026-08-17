from __future__ import annotations

import json
from pathlib import Path


def test_lineage_holdout_is_closed_and_nontrivial() -> None:
    path = Path(__file__).parents[1] / "eval/datasets/lineage-holdout.json"
    cases = json.loads(path.read_text())["cases"]

    assert len(cases) >= 8
    assert len({item["id"] for item in cases}) == len(cases)
    assert all(len(item["candidates"]) >= 3 for item in cases)
    assert all(item["expected"] in item["candidates"] for item in cases)
