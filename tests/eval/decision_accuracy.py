"""Deterministic decision gate for the DriftPatch benchmark."""

import json

EXPECTED = {
    "compatible-addition": ("unchanged", [], "unchanged"),
    "column-rename": ("repair", ["update_field_sources"], "repaired"),
    "delimiter-change": ("repair", ["set_delimiter"], "repaired"),
    "integer-decimal": ("repair", ["set_cast"], "repaired"),
    "date-format": ("repair", ["set_date_format"], "repaired"),
    "boolean-vocabulary": ("repair", ["set_boolean_values"], "repaired"),
    "json-record-path": ("repair", ["set_record_path"], "repaired"),
    "join-name": ("repair", ["set_join_source"], "repaired"),
    "split-coordinates": ("repair", ["set_split_source"], "repaired"),
    "missing-identifier": ("escalate", [], "escalated"),
    "duplicate-identifier": ("escalate", [], "escalated"),
    "italy-compatible-notes": ("unchanged", [], "unchanged"),
    "italy-documented-rename": ("repair", ["update_field_sources"], "repaired"),
    "jhu-granularity-shift": ("escalate", [], "escalated"),
    "swiss-null-column-removal": ("escalate", [], "escalated"),
    "nz-service-to-residence": ("escalate", [], "escalated"),
    "nz-masking-policy": ("escalate", [], "escalated"),
    "custom-program-two": (
        "repair",
        ["update_field_sources", "set_source_format"],
        "repaired",
    ),
    "custom-program-three": (
        "repair",
        ["set_record_path", "update_field_sources", "set_cast"],
        "repaired",
    ),
    "custom-program-four": (
        "repair",
        ["set_date_format", "update_field_sources", "set_cast", "set_delimiter"],
        "repaired",
    ),
    "custom-program-five": (
        "repair",
        [
            "set_date_format",
            "set_source_format",
            "update_field_sources",
            "set_cast",
            "set_delimiter",
        ],
        "repaired",
    ),
    "custom-program-six": (
        "repair",
        [
            "set_boolean_values",
            "set_date_format",
            "update_field_sources",
            "set_source_format",
            "set_record_path",
            "set_cast",
        ],
        "repaired",
    ),
    "custom-join-source": ("repair", ["set_join_source"], "repaired"),
    "custom-split-source": ("repair", ["set_split_source"], "repaired"),
    "custom-unchanged-json": ("unchanged", [], "unchanged"),
    "custom-ambiguous-alias": ("escalate", [], "escalated"),
    "custom-ambiguous-date": ("escalate", [], "escalated"),
    "custom-ambiguous-boolean": ("escalate", [], "escalated"),
    "custom-missing-key": ("escalate", [], "escalated"),
    "custom-duplicate-key": ("escalate", [], "escalated"),
    "custom-out-of-language": ("escalate", [], "escalated"),
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


def _json_payloads(agent_data):
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
                if isinstance(payload, dict):
                    yield payload


def _program(agent_data):
    payloads = list(_json_payloads(agent_data))
    programs = [payload for payload in payloads if "decision" in payload]
    if programs:
        value = programs[-1]
        operations = [
            step.get("operation")
            for step in value.get("steps", [])
            if isinstance(step, dict)
        ]
        return value.get("decision"), operations
    legacy = next(
        (payload for payload in payloads if "operation" in payload), None
    )
    if legacy is None:
        return None
    operation = legacy.get("operation")
    if operation == "no_change":
        return "unchanged", []
    if operation == "escalate":
        return "escalate", []
    return "repair", [operation]


def evaluate(instance):
    try:
        scenario_id = json.loads(_content_text(instance.get("prompt")))["scenario_id"]
        expected_decision, expected_operations, expected_status = EXPECTED[scenario_id]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {"score": 0, "explanation": "Unknown or malformed benchmark prompt."}

    program = _program(instance.get("agent_data"))
    if not program:
        return {"score": 0, "explanation": "Repair program missing from trace."}
    decision, operations = program

    response = _final_response_text(instance)
    checks = {
        "decision": decision == expected_decision,
        "operations": operations == expected_operations,
        "status": f": {expected_status};" in response
        or f": {expected_status} with " in response,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "score": 0 if failed else 1,
        "explanation": "Passed deterministic decision gate." if not failed else f"Failed: {', '.join(failed)}.",
    }
