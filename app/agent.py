# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.models import Gemini
from google.adk.workflow import Workflow
from google.genai import types

from .benchmark import inspect_scenario, load_scenario, run_contracts, scenario_source
from .ledger import save_run
from .repairs import apply_repair_plan
from .schemas import ApplyResult, IncidentInput, RepairPlan, ValidationResult


MODEL = "gemini-3.5-flash"


def inspect_incident(node_input: IncidentInput) -> Event:
    """Load one allowlisted incident and produce an evidence-rich drift report."""
    scenario = load_scenario(node_input.resolved_scenario_id)
    report = inspect_scenario(scenario)
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Inspected {scenario.id}.")],
        ),
        output=report,
        state={
            "scenario_id": scenario.id,
            "event_id": (
                node_input.attributes.event_id if node_input.attributes else None
            ),
            "trigger": (
                node_input.attributes.trigger if node_input.attributes else "api"
            ),
            "drift_report": report.model_dump(),
        },
    )


repair_planner = LlmAgent(
    name="repair_planner",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    mode="single_turn",
    generate_content_config=types.GenerateContentConfig(temperature=0),
    instruction="""You are DriftPatch's bounded repair planner. You receive a
structured comparison of a public data source before and after an upstream
change, the current pipeline configuration, its deterministic contract, and
the exact failure produced by the new source.

Choose exactly one operation. Never invent source fields or claim that a patch
works; deterministic checks run after you. Use `escalate` when evidence does
not justify a lossless repair.

Allowed operations and arguments:
- update_field_sources: field_sources [{"output_field": "name", "source_field": "full_name"}]
- set_delimiter: delimiter, one of comma, semicolon, pipe, or tab
- set_cast: field and strategy
- set_date_format: field and input_format, using Python strptime syntax
- set_boolean_values: field, true_values, and false_values
- set_record_path: path, using dot-separated keys
- set_join_source: field, sources, and separator
- set_split_source: source, split_fields [{"output_field": "latitude", "index": 0}], and separator
- escalate: no operation parameters

Leave every parameter unrelated to the chosen operation empty or null.

Evidence must cite concrete observed fields, formats, values or failed
contracts from the input. Confidence reflects only whether the proposed
operation is supported by those observations.""",
    input_schema=None,
    output_schema=RepairPlan,
    output_key="repair_plan",
)


def apply_plan(node_input: RepairPlan, ctx: Context) -> Event:
    """Apply one validated operation to an in-memory pipeline configuration."""
    scenario = load_scenario(ctx.state["scenario_id"])
    patched = apply_repair_plan(scenario.pipeline, node_input)
    result = ApplyResult(
        scenario_id=scenario.id,
        plan=node_input,
        patched_pipeline=patched,
        changed=patched != scenario.pipeline,
    )
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Applied {node_input.operation} in memory.")],
        ),
        output=result,
        state={"apply_result": result.model_dump()},
    )


def validate_plan(node_input: ApplyResult) -> Event:
    """Run deterministic contracts and choose an evidence-backed terminal state."""
    scenario = load_scenario(node_input.scenario_id)
    records, checks = run_contracts(
        scenario_source(scenario, "after"),
        node_input.patched_pipeline,
        scenario.contract,
    )
    passed = bool(checks) and all(check.passed for check in checks)
    if node_input.plan.operation == "escalate":
        status = "escalated"
    elif passed and node_input.changed:
        status = "repaired"
    else:
        status = "failed"
    evidence_complete = bool(all(
        (
            node_input.plan.evidence,
            scenario.before,
            scenario.after,
            checks,
            node_input.plan.rationale,
        )
    ))
    summary = (
        f"{scenario.title}: {status} with {node_input.plan.operation}; "
        f"{sum(check.passed for check in checks)}/{len(checks)} validation checks passed; "
        f"contract gate {'passed' if passed else 'failed'}."
    )
    if status == "escalated":
        summary += f" Reason: {node_input.plan.rationale}"
    result = ValidationResult(
        scenario_id=scenario.id,
        status=status,
        plan=node_input.plan,
        checks=checks,
        transformed_rows=len(records),
        evidence_complete=evidence_complete,
        summary=summary,
    )
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Deterministic contract gate completed.")],
        ),
        output=result,
    )


async def record_result(node_input: ValidationResult, ctx: Context) -> Event:
    """Write the terminal evidence record through an idempotent ledger adapter."""
    stored = await save_run(
        node_input,
        event_id=ctx.state.get("event_id"),
        trigger=ctx.state.get("trigger"),
    )
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=node_input.summary)],
        ),
        output=node_input,
        state={"ledger_record_id": stored["id"]},
    )


root_agent = Workflow(
    name="driftpatch",
    description="Detects and repairs bounded drift in public data pipelines.",
    input_schema=IncidentInput,
    output_schema=ValidationResult,
    edges=[
        ("START", inspect_incident),
        (inspect_incident, repair_planner),
        (repair_planner, apply_plan),
        (apply_plan, validate_plan),
        (validate_plan, record_result),
    ],
    timeout=300,
)

app = App(root_agent=root_agent, name="app")
