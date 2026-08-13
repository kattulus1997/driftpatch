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
from google.adk.events.event_actions import EventActions
from google.adk.models import Gemini
from google.adk.workflow import Workflow
from google.genai import types

from .benchmark import inspect_scenario, load_scenario
from .execution import current_execution
from .gate import apply_plan_deterministically, validate_plan_deterministically
from .schemas import (
    ApplyResult,
    IncidentInput,
    RepairPlan,
    ValidationResult,
    WorkerProposal,
)


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
        actions=EventActions(
            state_delta={
                "scenario_id": scenario.id,
                "event_id": (
                    node_input.attributes.event_id if node_input.attributes else None
                ),
                "trigger": (
                    node_input.attributes.trigger if node_input.attributes else "api"
                ),
                "drift_report": report.model_dump(),
            }
        ),
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
- no_change: when the current pipeline still satisfies every contract
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
    result = apply_plan_deterministically(ctx.state["scenario_id"], node_input)
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Applied {node_input.operation} in memory.")],
        ),
        output=result,
        actions=EventActions(state_delta={"apply_result": result.model_dump()}),
    )


def validate_plan(node_input: ApplyResult) -> Event:
    """Run deterministic contracts and choose an evidence-backed terminal state."""
    result = validate_plan_deterministically(node_input)
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Deterministic contract gate completed.")],
        ),
        output=result,
    )


async def record_result(node_input: ValidationResult, ctx: Context) -> Event:
    """Publish a proposal while leaving the terminal decision to the result service."""
    del ctx
    execution = current_execution()
    if execution is not None:
        await execution.publisher.publish(
            WorkerProposal(
                scenario_id=node_input.scenario_id,
                event_id=execution.event_id,
                issued_day=execution.issued_day,
                attempt_id=execution.attempt_id,
                execution_token=execution.execution_token,
                plan=node_input.plan,
            )
        )
        execution.published = True
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=node_input.summary)],
        ),
        output=node_input,
        actions=EventActions(
            state_delta={"result_published": execution is not None}
        ),
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
