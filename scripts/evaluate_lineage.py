from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.lineage import (
    MIN_MARGIN,
    MIN_SIMILARITY,
    VertexTextEmbedder,
    cosine_similarity,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests/eval/datasets/lineage-holdout.json"


async def evaluate(dataset: dict[str, object]) -> dict[str, object]:
    cases = dataset["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("lineage holdout must contain cases")
    embedder = VertexTextEmbedder()
    results: list[dict[str, object]] = []
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError("lineage cases must be objects")
        legacy = str(item["legacy"])
        candidates = [str(value) for value in item["candidates"]]
        expected = str(item["expected"])
        query = await embedder.embed(
            f"output field {legacy}; legacy source field {legacy}"
        )
        scores = {
            candidate: cosine_similarity(
                query,
                await embedder.embed(f"observed replacement field {candidate}"),
            )
            for candidate in candidates
        }
        ranked = sorted(scores, key=lambda candidate: scores[candidate] or -1, reverse=True)
        margin = (scores[ranked[0]] or 0) - (scores[ranked[1]] or 0)
        hinted = (scores[ranked[0]] or 0) >= MIN_SIMILARITY and margin >= MIN_MARGIN
        results.append(
            {
                "id": item["id"],
                "expected": expected,
                "selected": ranked[0],
                "correct": ranked[0] == expected,
                "hinted": hinted,
                "hint_correct": hinted and ranked[0] == expected,
                "margin": round(margin, 6),
            }
        )
    correct = sum(bool(item["correct"]) for item in results)
    hinted = sum(bool(item["hinted"]) for item in results)
    hint_correct = sum(bool(item["hint_correct"]) for item in results)
    return {
        "model": "gemini-embedding-001",
        "cases": len(results),
        "top1_correct": correct,
        "top1_accuracy": round(correct / len(results), 6),
        "hints_emitted": hinted,
        "hint_correct": hint_correct,
        "hint_precision": round(hint_correct / hinted, 6) if hinted else 0,
        "hint_coverage": round(hinted / len(results), 6),
        "mean_margin": round(
            sum(float(item["margin"]) for item in results) / len(results), 6
        ),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(evaluate(json.loads(args.dataset.read_text())))
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    if float(result["hint_precision"]) != 1 or float(result["hint_coverage"]) < 0.5:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
