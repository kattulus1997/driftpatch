from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=128)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=512)]
EventIdentifier = Annotated[
    str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
]
AttemptIdentifier = Annotated[
    str,
    Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    ),
]
IssuedDay = Annotated[
    str, Field(min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
]


class ExternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class SourceDocument(ExternalModel):
    format: Literal["csv", "json"]
    content: str


class CustomRunSubmission(ExternalModel):
    label: Annotated[str, Field(min_length=1, max_length=80)]
    before: SourceDocument
    after: SourceDocument
    pipeline_json: Annotated[str, Field(min_length=2)]
    contract_json: Annotated[str, Field(min_length=2)]


class StoredBundle(ExternalModel):
    object_name: Annotated[
        str, Field(pattern=r"^custom/[a-z0-9_]+\.json$", max_length=160)
    ]
    generation: int = Field(gt=0)
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: int = Field(gt=0, le=5 * 1024 * 1024)


class IncidentData(BaseModel):
    scenario_id: str


class IncidentAttributes(BaseModel):
    event_id: str | None = None
    issued_day: str | None = None
    trigger: str | None = None


class IncidentInput(BaseModel):
    case_kind: Literal["fixture", "custom"] | None = None
    case_id: str | None = None
    scenario_id: str | None = Field(
        default=None, description="Benchmark incident identifier"
    )
    data: IncidentData | None = None
    attributes: IncidentAttributes | None = None

    @model_validator(mode="after")
    def require_scenario_id(self):
        if not self.case_id and not self.scenario_id and not self.data:
            raise ValueError("case_id is required")
        return self

    @property
    def resolved_scenario_id(self) -> str:
        return self.case_id or self.scenario_id or self.data.scenario_id


class JoinSpec(ExternalModel):
    sources: list[str]
    separator: str = " "


class SplitSpec(ExternalModel):
    source: str
    index: int
    separator: str = ","


class BooleanSpec(ExternalModel):
    true_values: list[str]
    false_values: list[str]


class PipelineConfig(ExternalModel):
    format: Literal["csv", "json"]
    delimiter: Literal[",", ";", "|", "\t"] = ","
    record_path: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    fields: dict[str, str] = Field(min_length=1, max_length=256)
    casts: dict[
        str,
        Literal["string", "integer", "integer_grouped", "integer_from_float", "number"],
    ] = Field(default_factory=dict)
    date_formats: dict[str, str] = Field(default_factory=dict)
    booleans: dict[str, BooleanSpec] = Field(default_factory=dict)
    joins: dict[str, JoinSpec] = Field(default_factory=dict)
    splits: dict[str, SplitSpec] = Field(default_factory=dict)


class Contract(ExternalModel):
    required: list[ShortText] = Field(min_length=1, max_length=256)
    source_fields: list[ShortText] = Field(default_factory=list, max_length=256)
    types: dict[
        ShortText, Literal["string", "integer", "number", "boolean", "date"]
    ] = Field(min_length=1, max_length=256)
    unique_key: ShortText
    min_rows: int = Field(default=1, ge=1, le=20_000)
    source_aliases: dict[ShortText, list[ShortText]] = Field(
        default_factory=dict, max_length=256
    )
    preserve_values: list[ShortText] = Field(default_factory=list, max_length=256)
    row_policy: Literal["same_keys", "allow_append"] = "same_keys"


class FieldProfile(BaseModel):
    name: str
    inferred_type: str
    null_rate: float
    distinct_count: int
    example_values: list[str]


class SourceProfile(BaseModel):
    format: str
    delimiter: str | None = None
    record_path: str | None = None
    row_count: int
    fields: list[FieldProfile]


class DriftReport(BaseModel):
    scenario_id: str
    title: str
    current_pipeline: PipelineConfig
    contract: Contract
    before: SourceProfile
    after: SourceProfile
    added_fields: list[str]
    removed_fields: list[str]
    type_changes: dict[str, list[str]]
    current_failure: str


RepairOperation = Literal[
    "no_change",
    "update_field_sources",
    "set_delimiter",
    "set_cast",
    "set_date_format",
    "set_boolean_values",
    "set_record_path",
    "set_join_source",
    "set_split_source",
    "escalate",
]


class FieldSourceUpdate(BaseModel):
    output_field: ShortText
    source_field: ShortText


class SplitField(BaseModel):
    output_field: ShortText
    index: int = Field(ge=0, le=15)


class RepairPlan(BaseModel):
    operation: RepairOperation
    field_sources: list[FieldSourceUpdate] = Field(default_factory=list, max_length=16)
    delimiter: Literal[",", ";", "|", "\t"] | None = None
    field: ShortText | None = None
    strategy: Literal[
        "string", "integer", "integer_grouped", "integer_from_float", "number"
    ] | None = None
    input_format: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    true_values: list[ShortText] = Field(default_factory=list, max_length=32)
    false_values: list[ShortText] = Field(default_factory=list, max_length=32)
    path: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    sources: list[ShortText] = Field(default_factory=list, max_length=16)
    source: ShortText | None = None
    split_fields: list[SplitField] = Field(default_factory=list, max_length=16)
    separator: Annotated[str, Field(min_length=1, max_length=8)] | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceText] = Field(min_length=1, max_length=16)
    rationale: Annotated[str, Field(min_length=1, max_length=1024)]


RepairStepOperation = Literal[
    "set_source_format",
    "update_field_sources",
    "set_delimiter",
    "set_cast",
    "set_date_format",
    "set_boolean_values",
    "set_record_path",
    "set_join_source",
    "set_split_source",
]


class RepairStep(ExternalModel):
    operation: RepairStepOperation
    field_sources: list[FieldSourceUpdate] = Field(default_factory=list, max_length=16)
    delimiter: Literal[",", ";", "|", "\t"] | None = None
    format: Literal["csv", "json"] | None = None
    field: ShortText | None = None
    strategy: Literal[
        "string", "integer", "integer_grouped", "integer_from_float", "number"
    ] | None = None
    input_format: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    true_values: list[ShortText] = Field(default_factory=list, max_length=32)
    false_values: list[ShortText] = Field(default_factory=list, max_length=32)
    path: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    sources: list[ShortText] = Field(default_factory=list, max_length=16)
    source: ShortText | None = None
    split_fields: list[SplitField] = Field(default_factory=list, max_length=16)
    separator: Annotated[str, Field(min_length=1, max_length=8)] | None = None

    @model_validator(mode="after")
    def canonicalize_commutative_updates(self):
        self.field_sources.sort(
            key=lambda item: (item.output_field, item.source_field)
        )
        return self


class RepairProgram(ExternalModel):
    decision: Literal["unchanged", "repair", "escalate"]
    steps: list[RepairStep] = Field(default_factory=list, max_length=6)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceText] = Field(min_length=1, max_length=16)
    rationale: Annotated[str, Field(min_length=1, max_length=1024)]

    @model_validator(mode="after")
    def enforce_decision_shape(self):
        if self.decision == "repair" and not self.steps:
            raise ValueError("repair decisions require one to six steps")
        if self.decision != "repair" and self.steps:
            raise ValueError("unchanged and escalate decisions require zero steps")
        canonical_steps = [
            json.dumps(
                step.model_dump(
                    mode="json", exclude_none=True, exclude_defaults=True
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
            for step in self.steps
        ]
        if len(canonical_steps) != len(set(canonical_steps)):
            raise ValueError("repair steps must be unique")
        return self


class Candidate(ExternalModel):
    id: Annotated[str, Field(pattern=r"^c_[0-9a-f]{12}$")]
    step: RepairStep
    summary: EvidenceText


class ApplyResult(BaseModel):
    scenario_id: str
    plan: RepairPlan
    patched_pipeline: PipelineConfig
    changed: bool
    application_error: str | None = None


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


class ConfigurationReceipt(BaseModel):
    state: Literal["applied", "already_active"]
    version: int = Field(ge=1)
    affected_outputs: list[ShortText] = Field(min_length=1, max_length=16)
    previous_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    applied_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    rollback_ready: Literal[True] = True


class ValidationResult(BaseModel):
    scenario_id: str
    status: Literal["unchanged", "repaired", "escalated", "failed"]
    plan: RepairPlan | None = None
    program: RepairProgram | None = None
    checks: list[CheckResult]
    transformed_rows: int
    evidence_complete: bool
    summary: str
    patched_pipeline: PipelineConfig | None = None
    patched_pipeline_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    application: ConfigurationReceipt | None = None


class Counterexample(ExternalModel):
    invariant: ShortText
    output_field: ShortText | None = None
    failing_count: int = Field(ge=1)
    detail: EvidenceText


CandidateIdentifier = Annotated[str, Field(pattern=r"^c_[0-9a-f]{12}$")]


class CandidateOption(ExternalModel):
    id: CandidateIdentifier
    summary: EvidenceText


class CandidatePrompt(ExternalModel):
    round: int = Field(ge=1, le=3)
    report: DriftReport
    candidates: list[CandidateOption] = Field(max_length=256)
    counterexamples: list[Counterexample] = Field(default_factory=list, max_length=3)


class CandidateSelection(ExternalModel):
    decision: Literal["repair", "escalate"]
    candidate_ids: list[CandidateIdentifier] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def enforce_selection_shape(self):
        if self.decision == "repair" and not self.candidate_ids:
            raise ValueError("repair selections require at least one candidate")
        if self.decision == "escalate" and self.candidate_ids:
            raise ValueError("escalation selections cannot include candidates")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidate identifiers must be unique")
        return self


class RunReceipt(BaseModel):
    id: str
    scenario_id: str
    status: Literal["queued"]


class CustomRunReceipt(ExternalModel):
    id: EventIdentifier
    status: Literal["queued"]
    status_url: Annotated[str, Field(pattern=r"^/api/runs/custom_[0-9a-f]{32}$")]


class CustomClaim(ExternalModel):
    disposition: Literal["acquired", "existing", "exhausted"]
    run_id: EventIdentifier
    attempt_id: AttemptIdentifier | None = None
    token: AttemptIdentifier | None = None
    exhausted_budget: Literal["inference", "total"] | None = None


class TaskRequest(ExternalModel):
    case_kind: Literal["fixture", "custom"]
    case_id: EventIdentifier
    event_id: EventIdentifier
    issued_day: IssuedDay
    attempt_id: AttemptIdentifier
    attempt_token: AttemptIdentifier
    bundle: StoredBundle | None = None

    @model_validator(mode="after")
    def enforce_bundle_reference(self):
        if self.case_kind == "custom" and self.bundle is None:
            raise ValueError("custom tasks require a bundle reference")
        if self.case_kind == "fixture" and self.bundle is not None:
            raise ValueError("fixture tasks cannot include a bundle reference")
        if (
            self.bundle is not None
            and self.bundle.object_name != f"custom/{self.case_id}.json"
        ):
            raise ValueError("bundle object must match the custom case")
        return self


class AttemptLease(BaseModel):
    disposition: Literal["run", "terminal", "busy", "stale"]
    execution_token: AttemptIdentifier | None = None


class WorkerProposal(ExternalModel):
    case_kind: Literal["fixture", "custom"]
    case_id: EventIdentifier
    event_id: EventIdentifier
    issued_day: IssuedDay
    attempt_id: AttemptIdentifier
    execution_token: AttemptIdentifier
    bundle: StoredBundle | None = None
    program: RepairProgram

    @model_validator(mode="after")
    def enforce_proposal_bundle_reference(self):
        if self.case_kind == "custom" and self.bundle is None:
            raise ValueError("custom proposals require a bundle reference")
        if self.case_kind == "fixture" and self.bundle is not None:
            raise ValueError("fixture proposals cannot include a bundle reference")
        if (
            self.bundle is not None
            and self.bundle.object_name != f"custom/{self.case_id}.json"
        ):
            raise ValueError("bundle object must match the custom case")
        return self


class Scenario(BaseModel):
    id: str
    title: str
    before: str
    after: str
    pipeline: PipelineConfig
    contract: Contract
    expected_status: Literal["unchanged", "repaired", "escalated"]
    expected_plan: RepairPlan | None = None
    expected_pipeline_sha256: Annotated[
        str, Field(pattern=r"^[0-9a-f]{64}$")
    ] | None = None
    split: Literal["calibration", "holdout"] | None = None
    transition: Literal[
        "csv_to_csv", "csv_to_json", "json_to_csv", "json_to_json"
    ] | None = None
