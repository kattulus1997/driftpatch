export type FieldProfile = {
  name: string;
  inferred_type: string;
  null_rate: number;
  distinct_count: number;
  example_values: string[];
};

export type SourceProfile = {
  format: string;
  delimiter: string | null;
  record_path: string | null;
  row_count: number;
  fields: FieldProfile[];
};

export type DriftReport = {
  scenario_id: string;
  title: string;
  before: SourceProfile;
  after: SourceProfile;
  added_fields: string[];
  removed_fields: string[];
  type_changes: Record<string, [string, string]>;
  current_failure: string;
  contract: {
    required: string[];
    types: Record<string, string>;
    unique_key: string;
    min_rows: number;
  };
};

export type RepairPlan = {
  operation: string;
  field_sources: {output_field: string; source_field: string}[];
  delimiter: string | null;
  field: string | null;
  strategy: string | null;
  input_format: string | null;
  true_values: string[];
  false_values: string[];
  path: string | null;
  sources: string[];
  source: string | null;
  split_fields: {output_field: string; index: number}[];
  separator: string | null;
  confidence: number;
  evidence: string[];
  rationale: string;
};

export type ValidationResult = {
  id?: string;
  trigger?: string;
  source_sha256?: string;
  scenario_id: string;
  status: "unchanged" | "repaired" | "escalated" | "failed";
  plan: RepairPlan;
  checks: {name: string; passed: boolean; detail: string}[];
  transformed_rows: number;
  evidence_complete: boolean;
  summary: string;
  application: {
    state: "applied" | "already_active";
    version: number;
    affected_outputs: string[];
    previous_sha256: string;
    applied_sha256: string;
    rollback_ready: boolean;
  } | null;
};

export type RunReceipt = {
  id: string;
  scenario_id: string;
  status: "queued";
};

export type RunNotStarted = {
  id: string;
  scenario_id: string;
  status: "not_started";
};

export type RunStatus = RunNotStarted | RunReceipt | ValidationResult;

export type ScenarioItem = {
  id: string;
  title: string;
  report: DriftReport;
};

export type ScenariosResponse = {
  items: ScenarioItem[];
};
