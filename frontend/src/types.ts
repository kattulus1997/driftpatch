export type SourceFormat = "csv" | "json";

export type SourceDocument = {
  format: SourceFormat;
  content: string;
};

export type CustomRunSubmission = {
  label: string;
  before: SourceDocument;
  after: SourceDocument;
  pipeline_json: string;
  contract_json: string;
};

export type FieldSourceUpdate = {
  output_field: string;
  source_field: string;
};

export type RepairStep = {
  operation: string;
  field_sources: FieldSourceUpdate[];
  delimiter?: string | null;
  format?: SourceFormat | null;
  field?: string | null;
  strategy?: string | null;
  input_format?: string | null;
  true_values: string[];
  false_values: string[];
  path?: string | null;
  sources: string[];
  source?: string | null;
  split_fields: {output_field: string; index: number}[];
  separator?: string | null;
};

export type RepairProgram = {
  decision: "unchanged" | "repair" | "escalate";
  steps: RepairStep[];
  confidence: number;
  evidence: string[];
  rationale: string;
};

export type PipelineConfig = Record<string, unknown>;

export type ValidationResult = {
  id: string;
  status: "unchanged" | "repaired" | "escalated" | "failed";
  program: RepairProgram | null;
  checks: {name: string; passed: boolean; detail: string}[];
  transformed_rows: number;
  evidence_complete: boolean;
  summary: string;
  patched_pipeline: PipelineConfig | null;
  patched_pipeline_hash: string | null;
  application: {
    state: "applied" | "already_active";
    version: number;
    affected_outputs: string[];
    previous_sha256: string;
    applied_sha256: string;
    rollback_ready: boolean;
  } | null;
};

export type CustomRunReceipt = {
  id: string;
  status: "queued";
  status_url: string;
};

export type CustomRunStatus = CustomRunReceipt | ValidationResult;
