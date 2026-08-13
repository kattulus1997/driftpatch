"""Deterministic decision gate for the DriftPatch benchmark."""

import json

EXPECTED = {
    "compatible-addition": ("no_change", "unchanged"),
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
    "italy-compatible-notes": ("no_change", "unchanged"),
    "italy-documented-rename": ("update_field_sources", "repaired"),
    "jhu-granularity-shift": ("escalate", "escalated"),
    "swiss-null-column-removal": ("escalate", "escalated"),
    "nz-service-to-residence": ("escalate", "escalated"),
    "nz-masking-policy": ("escalate", "escalated"),
}


def _content_text(content):
    if not isinstance(content, dict):
        return ""
    return "\n".join(
        part.get("text", "")
        for part in content.get("parts", [])
        if isinstance(part, dict) and part.get("text")
    )


def _final_response_text(instance):
    direct = _content_text(instance.get("response"))
    if direct:
        return direct
    return "\n".join(
        _content_text(item.get("response"))
        for item in instance.get("responses", [])
        if isinstance(item, dict)
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

    response = _final_response_text(instance)
    expected_gate = (
        "contract gate passed"
        if expected_status in {"unchanged", "repaired"}
        else "contract gate failed"
    )
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
