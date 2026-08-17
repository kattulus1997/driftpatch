from __future__ import annotations

import json
from pathlib import Path


def test_risk_preflight_holdout_matches_the_frozen_planner_trace() -> None:
    path = Path(__file__).parents[1] / "eval/datasets/risk-preflight-holdout.json"
    cases = json.loads(path.read_text())["cases"]

    assert len(cases) == 8
    assert len({item["scenario_id"] for item in cases}) == len(cases)
    assert sum(item["canonical_escalation"] for item in cases) == 4
    assert sum(item["planner_calls"] for item in cases) == 13
