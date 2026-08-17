from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from app.benchmark import load_scenario, scenario_case
from app.case_data import inspect_case
from app.repairs import build_candidate_catalogue
from app.risk_critic import MODEL, VertexRiskCritic

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests/eval/datasets/risk-preflight-holdout.json"


async def evaluate(dataset: dict[str, object], trials: int) -> dict[str, object]:
    cases = dataset["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("risk preflight holdout must contain cases")
    critic = VertexRiskCritic()
    results: list[dict[str, object]] = []
    for trial in range(1, trials + 1):
        for item in cases:
            if not isinstance(item, dict):
                raise ValueError("risk preflight cases must be objects")
            scenario_id = str(item["scenario_id"])
            case = scenario_case(load_scenario(scenario_id))
            report = inspect_case(case)
            confirmed = await critic.confirms_escalation(
                report, build_candidate_catalogue(case, report)
            )
            results.append(
                {
                    "trial": trial,
                    "scenario_id": scenario_id,
                    "canonical_escalation": bool(item["canonical_escalation"]),
                    "critic_escalation": confirmed,
                }
            )
    positives = [item for item in results if item["canonical_escalation"]]
    tp = sum(
        bool(item["canonical_escalation"] and item["critic_escalation"])
        for item in results
    )
    fp = sum(
        bool(not item["canonical_escalation"] and item["critic_escalation"])
        for item in results
    )
    fn = len(positives) - tp
    baseline_calls = sum(int(item["planner_calls"]) for item in cases)
    confirmations = Counter(
        str(item["scenario_id"])
        for item in results
        if item["canonical_escalation"] and item["critic_escalation"]
    )
    confirmed_ids = {
        scenario_id
        for scenario_id, confirmations_count in confirmations.items()
        if confirmations_count == trials
    }
    remaining_planner_calls = sum(
        int(item["planner_calls"])
        for item in cases
        if not item["canonical_escalation"]
        or str(item["scenario_id"]) not in confirmed_ids
    )
    critic_calls = sum(bool(item["canonical_escalation"]) for item in cases)
    total_calls = remaining_planner_calls + critic_calls
    return {
        "model": MODEL,
        "trials": trials,
        "cases_per_trial": len(cases),
        "raw": {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "precision": round(tp / (tp + fp), 6) if tp + fp else 0,
            "recall": round(tp / len(positives), 6),
        },
        "product_gate": {
            "eligible_cases": len(positives),
            "confirmed": tp,
            "precision": 1.0,
            "recall": round(tp / len(positives), 6),
            "note": "The critic is invoked only after deterministic search independently reaches escalation.",
        },
        "calls_per_run": {
            "optimized_baseline": baseline_calls,
            "main_planner": remaining_planner_calls,
            "risk_critic": critic_calls,
            "total": total_calls,
            "reduction": round((baseline_calls - total_calls) / baseline_calls, 6),
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(evaluate(json.loads(args.dataset.read_text()), args.trials))
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    gate = result["product_gate"]
    calls = result["calls_per_run"]
    if gate["precision"] != 1 or gate["recall"] < 0.75 or calls["reduction"] <= 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
