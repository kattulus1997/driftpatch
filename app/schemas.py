from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

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


class IncidentData(BaseModel):
    scenario_id: str


class IncidentAttributes(BaseModel):
    event_id: str | None = None
    issued_day: str | None = None
    trigger: str | None = None


class IncidentInput(BaseModel):
    scenario_id: str | None = Field(
        default=None, description="Benchmark incident identifier"
    )
    data: IncidentData | None = None
    attributes: IncidentAttributes | None = None

    @model_validator(mode="after")
    def require_scenario_id(self):
        if not self.scenario_id and not self.data:
            raise ValueError("scenario_id is required")
        return self

    @property
    def resolved_scenario_id(self) -> str:
        return self.scenario_id or self.data.scenario_id


class JoinSpec(BaseModel):
    sources: list[str]
    separator: str = " "


class SplitSpec(BaseModel):
    source: str
    index: int
    separator: str = ","


class BooleanSpec(BaseModel):
    true_values: list[str]
    false_values: list[str]


class PipelineConfig(BaseModel):
    format: Literal["csv", "json"]
    delimiter: str = ","
    record_path: str | None = None
    fields: dict[str, str]
    casts: dict[
        str,
        Literal["string", "integer", "integer_grouped", "integer_from_float", "number"],
    ] = Field(default_factory=dict)
    date_formats: dict[str, str] = Field(default_factory=dict)
    booleans: dict[str, BooleanSpec] = Field(default_factory=dict)
    joins: dict[str, JoinSpec] = Field(default_factory=dict)
    splits: dict[str, SplitSpec] = Field(default_factory=dict)


class Contract(BaseModel):
    required: list[str]
    source_fields: list[str] = Field(default_factory=list)
    types: dict[str, Literal["string", "integer", "number", "boolean", "date"]]
    unique_key: str
    min_rows: int = 1
    source_aliases: dict[str, list[str]] = Field(default_factory=dict)
    preserve_values: list[str] = Field(default_factory=list)


class FieldProfile(BaseModel):
    name: str
    inferred_type: str
    null_rate: float
    distinct_count: int
    example_values: list[str]


class SourceProfile(BaseModel):
    format: str
    delimiter: str | None
    record_path: str | None
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


class ValidationResult(BaseModel):
    scenario_id: str
    status: Literal["unchanged", "repaired", "escalated", "failed"]
    plan: RepairPlan
    checks: list[CheckResult]
    transformed_rows: int
    evidence_complete: bool
    summary: str


class RunReceipt(BaseModel):
    id: str
    scenario_id: str
    status: Literal["queued"]


class TaskRequest(BaseModel):
    scenario_id: ShortText
    event_id: EventIdentifier
    issued_day: IssuedDay
    attempt_id: AttemptIdentifier
    attempt_token: AttemptIdentifier


class AttemptLease(BaseModel):
    disposition: Literal["run", "terminal", "busy", "stale"]
    execution_token: AttemptIdentifier | None = None


class WorkerProposal(BaseModel):
    scenario_id: ShortText
    event_id: EventIdentifier
    issued_day: IssuedDay
    attempt_id: AttemptIdentifier
    execution_token: AttemptIdentifier
    plan: RepairPlan


class Scenario(BaseModel):
    id: str
    title: str
    before: str
    after: str
    pipeline: PipelineConfig
    contract: Contract
    expected_status: Literal["unchanged", "repaired", "escalated"]
    expected_plan: RepairPlan
