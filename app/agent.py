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
from google.adk.workflow import FunctionNode, Workflow
from google.genai import types

from typing import Protocol

from .benchmark import load_scenario, scenario_case
from .case_data import RepairCase, inspect_case
from .execution import current_execution
from .model_armor import SafetyScreen, configured_safety_screen
from .repairs import CandidateCatalogue, build_candidate_catalogue
from .schemas import (
    CandidateOption,
    CandidatePrompt,
    CandidateSelection,
    Counterexample,
    DriftReport,
    IncidentInput,
    RepairProgram,
    ValidationResult,
    WorkerProposal,
)
from .synthesis import (
    minimal_counterexample,
    search_catalogue,
    verify_authoritative_program,
    verify_program,
)


MODEL = "gemini-3.5-flash"


def _structural_report(report: DriftReport) -> DriftReport:
    value = report.model_copy(deep=True)
    for profile in (value.before, value.after):
        for field in profile.fields:
            field.example_values = []
    return value


def _case_for_id(case_id: str) -> RepairCase:
    execution = current_execution()
    if execution is not None and execution.case is not None:
        if execution.case.id != case_id:
            raise RuntimeError("execution case identity mismatch")
        return execution.case
    return scenario_case(load_scenario(case_id))


def inspect_incident(node_input: IncidentInput) -> Event:
    """Inspect a case while keeping all source rows outside session state."""
    case = _case_for_id(node_input.resolved_scenario_id)
    report = _structural_report(inspect_case(case))
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Inspected {case.id}.")],
        ),
        output=report,
        actions=EventActions(
            state_delta={
                "case_id": case.id,
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
    generate_content_config=types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW
        ),
    ),
    instruction="""Select only from the opaque candidate IDs supplied by
DriftPatch. Return `repair` with one to six unique IDs when the structural
evidence and prior counterexamples justify that composition. Return
`escalate` with no IDs when evidence is insufficient. Never invent an ID,
operation, source field or success claim. A deterministic verifier—not this
selection—decides whether the repair is valid.""",
    input_schema=CandidatePrompt,
    output_schema=CandidateSelection,
    output_key="candidate_selection",
)


class CandidatePlanner(Protocol):
    async def select(self, prompt: CandidatePrompt) -> CandidateSelection: ...


def _terminal_program(decision: str, rationale: str) -> RepairProgram:
    return RepairProgram(
        decision=decision,
        steps=[],
        confidence=1,
        evidence=[rationale.replace("_", " ")],
        rationale=rationale,
    )


def _candidate_prompt(
    report: DriftReport,
    catalogue: CandidateCatalogue,
    feedback: list[Counterexample],
    round_number: int,
) -> CandidatePrompt:
    return CandidatePrompt(
        round=round_number,
        report=_structural_report(report),
        candidates=[
            CandidateOption(id=item.id, summary=item.summary) for item in catalogue
        ],
        counterexamples=feedback[-3:],
    )


def _selection_program(
    catalogue: CandidateCatalogue, selection: CandidateSelection
) -> RepairProgram:
    return RepairProgram(
        decision="repair",
        steps=catalogue.select(selection.candidate_ids),
        confidence=0,
        evidence=[f"selected candidate: {item}" for item in selection.candidate_ids],
        rationale="model_selected_candidates",
    )


def _safe_escalation(
    case: RepairCase, catalogue: CandidateCatalogue, rationale: str
) -> ValidationResult:
    return verify_program(case, _terminal_program("escalate", rationale), catalogue)


async def synthesize_case(
    report: DriftReport,
    case: RepairCase,
    planner: CandidatePlanner,
    safety: SafetyScreen,
) -> ValidationResult:
    """Run a bounded verifier-guided selection loop over authorized mutations."""
    catalogue = build_candidate_catalogue(case, report)
    unchanged = verify_program(
        case,
        _terminal_program("unchanged", "contracts_already_satisfied"),
        catalogue,
    )
    if unchanged.status == "unchanged":
        return unchanged

    canonical = search_catalogue(case, catalogue)
    feedback: list[Counterexample] = []
    for round_number in range(1, 4):
        prompt = _candidate_prompt(report, catalogue, feedback, round_number)
        if not (await safety.screen_prompt(prompt.model_dump_json())).allowed:
            return _safe_escalation(case, catalogue, "safety_screen_blocked")
        selection = await planner.select(prompt)
        if not (await safety.screen_response(selection.model_dump_json())).allowed:
            return _safe_escalation(case, catalogue, "safety_screen_blocked")
        if selection.decision == "escalate":
            feedback.append(
                Counterexample(
                    invariant="selection",
                    failing_count=1,
                    detail="selection did not identify a verifiable candidate",
                )
            )
            continue
        try:
            program = _selection_program(catalogue, selection)
        except ValueError:
            feedback.append(
                Counterexample(
                    invariant="catalogue_authorized",
                    failing_count=1,
                    detail="selection included an unknown candidate",
                )
            )
            continue
        result = verify_authoritative_program(
            case,
            program,
            catalogue,
            canonical_program=canonical,
        )
        if result.status != "failed":
            return result
        feedback.append(minimal_counterexample(result))
    return verify_program(case, canonical, catalogue)


class _ContextPlanner:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    async def select(self, prompt: CandidatePrompt) -> CandidateSelection:
        output = await self._ctx.run_node(
            repair_planner,
            prompt,
            run_id=f"proposal-r{prompt.round}",
        )
        return CandidateSelection.model_validate(output)


async def synthesize_program(node_input: DriftReport, ctx: Context) -> Event:
    case = _case_for_id(node_input.scenario_id)
    result = await synthesize_case(
        inspect_case(case),
        case,
        _ContextPlanner(ctx),
        configured_safety_screen(),
    )
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=result.program.model_dump_json())],
        ),
        output=result,
    )


synthesis_node = FunctionNode(
    func=synthesize_program,
    rerun_on_resume=True,
)


async def record_result(node_input: ValidationResult, ctx: Context) -> Event:
    """Publish a proposal while leaving the terminal decision to the result service."""
    del ctx
    execution = current_execution()
    if execution is not None:
        await execution.publisher.publish(
            WorkerProposal(
                case_kind=execution.case_kind,
                case_id=execution.case_id,
                event_id=execution.event_id,
                issued_day=execution.issued_day,
                attempt_id=execution.attempt_id,
                execution_token=execution.execution_token,
                bundle=execution.bundle,
                program=node_input.program,
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
        (inspect_incident, synthesis_node),
        (synthesis_node, record_result),
    ],
    timeout=300,
)

app = App(root_agent=root_agent, name="app")
