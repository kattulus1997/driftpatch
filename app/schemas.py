from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class IncidentData(BaseModel):
    scenario_id: str


class IncidentAttributes(BaseModel):
    event_id: str | None = None
    trigger: str | None = None


class IncidentInput(BaseModel):
    scenario_id: str | None = Field(
        default=None, description="Benchmark incident identifier"
    )
    data: IncidentData | None = Field(
        default=None, description="Pub/Sub-compatible incident payload"
    )
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
    casts: dict[str, Literal["string", "integer", "integer_from_float", "number"]] = Field(default_factory=dict)
    date_formats: dict[str, str] = Field(default_factory=dict)
    booleans: dict[str, BooleanSpec] = Field(default_factory=dict)
    joins: dict[str, JoinSpec] = Field(default_factory=dict)
    splits: dict[str, SplitSpec] = Field(default_factory=dict)


class Contract(BaseModel):
    required: list[str]
    types: dict[str, Literal["string", "integer", "number", "boolean", "date"]]
    unique_key: str
    min_rows: int = 1


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
    output_field: str
    source_field: str


class SplitField(BaseModel):
    output_field: str
    index: int = Field(ge=0)


class RepairPlan(BaseModel):
    operation: RepairOperation
    field_sources: list[FieldSourceUpdate] = Field(default_factory=list)
    delimiter: Literal[",", ";", "|", "\t"] | None = None
    field: str | None = None
    strategy: Literal["string", "integer", "integer_from_float", "number"] | None = None
    input_format: str | None = None
    true_values: list[str] = Field(default_factory=list)
    false_values: list[str] = Field(default_factory=list)
    path: str | None = None
    sources: list[str] = Field(default_factory=list)
    source: str | None = None
    split_fields: list[SplitField] = Field(default_factory=list)
    separator: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1)
    rationale: str


class ApplyResult(BaseModel):
    scenario_id: str
    plan: RepairPlan
    patched_pipeline: PipelineConfig
    changed: bool


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


class ValidationResult(BaseModel):
    scenario_id: str
    status: Literal["repaired", "escalated", "failed"]
    plan: RepairPlan
    checks: list[CheckResult]
    transformed_rows: int
    evidence_complete: bool
    summary: str


class Scenario(BaseModel):
    id: str
    title: str
    before: str
    after: str
    pipeline: PipelineConfig
    contract: Contract
    expected_status: Literal["repaired", "escalated"]
    expected_plan: RepairPlan
