"""Deterministic decision gate for the DriftPatch benchmark."""

import json


EXPECTED = {
    "column-rename": ("update_field_sources", "repaired"),
    "delimiter-change": ("set_delimiter", "repaired"),
    "integer-decimal": ("set_cast", "repaired"),
    "date-format": ("set_date_format", "repaired"),
    "boolean-vocabulary": ("set_boolean_values", "repaired"),
    "json-record-path": ("set_record_path", "repaired"),
    "join-name": ("set_join_source", "repaired"),
    "split-coordinates": ("set_split_source", "repaired"),
    "missing-identifier": ("escalate", "escalated"),
    "duplicate-identifier": ("escalate", "escalated"),
}


def _content_text(content):
    if not isinstance(content, dict):
        return ""
    return "\n".join(
        part.get("text", "")
        for part in content.get("parts", [])
        if isinstance(part, dict) and part.get("text")
    )


def _planner_decision(agent_data):
    for turn in (agent_data or {}).get("turns", []):
        for event in turn.get("events", []):
            for part in (event.get("content") or {}).get("parts", []):
                text = part.get("text") if isinstance(part, dict) else None
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and "operation" in payload:
                    return payload
    return None


def evaluate(instance):
    try:
        scenario_id = json.loads(_content_text(instance.get("prompt")))["scenario_id"]
        expected_operation, expected_status = EXPECTED[scenario_id]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {"score": 0, "explanation": "Unknown or malformed benchmark prompt."}

    decision = _planner_decision(instance.get("agent_data"))
    if not decision:
        return {"score": 0, "explanation": "Planner decision missing from trace."}

    response = _content_text(instance.get("response"))
    expected_gate = "contract gate passed" if expected_status == "repaired" else "contract gate failed"
    checks = {
        "operation": decision.get("operation") == expected_operation,
        "evidence": bool(decision.get("evidence")),
        "status": f": {expected_status} with {expected_operation};" in response,
        "gate": expected_gate in response,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "score": 0 if failed else 1,
        "explanation": "Passed deterministic decision gate." if not failed else f"Failed: {', '.join(failed)}.",
    }
