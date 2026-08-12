import runpy
from pathlib import Path


EVALUATE = runpy.run_path(
    Path(__file__).parents[1] / "eval" / "decision_accuracy.py"
)["evaluate"]


def _instance(operation: str, status: str, gate: str):
    return {
        "prompt": {"parts": [{"text": '{"scenario_id":"column-rename"}'}]},
        "response": {
            "parts": [
                {
                    "text": f"Title: {status} with {operation}; 6/6 validation checks passed; contract gate {gate}."
                }
            ]
        },
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": f'{{"operation":"{operation}","evidence":["observed"]}}'
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    }


def test_decision_accuracy_accepts_expected_terminal_state() -> None:
    assert EVALUATE(_instance("update_field_sources", "repaired", "passed"))["score"] == 1


def test_decision_accuracy_rejects_wrong_operation() -> None:
    assert EVALUATE(_instance("escalate", "repaired", "passed"))["score"] == 0
