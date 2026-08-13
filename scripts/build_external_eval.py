from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmark" / "external" / "manifest.json"
OUTPUT = ROOT / "tests" / "eval" / "datasets" / "external-holdout.json"


def build() -> None:
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    selected = [case for case in cases if case["split"] == "holdout"]
    payload = {
        "eval_cases": [
            {
                "eval_case_id": case["id"],
                "prompt": {
                    "role": "user",
                    "parts": [{"text": json.dumps({"scenario_id": case["id"]})}],
                },
                "reference": {
                    "response": {
                        "role": "model",
                        "parts": [
                            {
                                "text": (
                                    f"{case['publisher']}: {case['dataset']}: "
                                    f"{case['expected_status']} with "
                                    f"{case['expected_plan']['operation']}."
                                )
                            }
                        ],
                    }
                },
            }
            for case in selected
        ]
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
