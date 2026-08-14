import {useState} from "react";

import {getCustomRun, getExample, startCustomRun} from "./api";
import type {
  CustomRunSubmission,
  RepairStep,
  SourceFormat,
  ValidationResult,
} from "./types";

const POLL_INTERVAL_MS = 750;
const RUN_TIMEOUT_MS = 90_000;
const MAX_REQUEST_BYTES = 5 * 1024 * 1024;

type LoadedFile = {
  name: string;
  content: string;
  format?: SourceFormat;
};

type Files = {
  before: LoadedFile | null;
  after: LoadedFile | null;
  pipeline: LoadedFile | null;
  contract: LoadedFile | null;
};

type FileKey = keyof Files;

const EMPTY_FILES: Files = {
  before: null,
  after: null,
  pipeline: null,
  contract: null,
};

const wait = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

async function waitForTerminalResult(runId: string): Promise<ValidationResult> {
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const current = await getCustomRun(runId);
    if (current.status !== "queued") return current;
    await wait(POLL_INTERVAL_MS);
  }
  throw new Error("The repair did not finish within 90 seconds.");
}

function sourceFormat(fileName: string): SourceFormat | null {
  const extension = fileName.toLowerCase().split(".").pop();
  return extension === "csv" || extension === "json" ? extension : null;
}

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () =>
      typeof reader.result === "string"
        ? resolve(reader.result)
        : reject(new Error(`${file.name} could not be read as text.`));
    reader.onerror = () => reject(new Error(`${file.name} could not be read.`));
    reader.readAsText(file, "utf-8");
  });
}

function jsonObject(value: string, label: string): void {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${label} must contain valid JSON.`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must contain a JSON object.`);
  }
}

function words(value: string): string {
  return value.replaceAll("_", " ").replaceAll("-", " ");
}

function statusTitle(status: ValidationResult["status"]): string {
  return {
    unchanged: "No repair needed",
    repaired: "Repair verified",
    escalated: "Manual review required",
    failed: "Repair rejected",
  }[status];
}

function stepParameters(step: RepairStep): [string, string][] {
  const values: [string, string][] = [];
  if (step.format) values.push(["format", step.format]);
  if (step.delimiter) values.push(["delimiter", step.delimiter === "\t" ? "tab" : step.delimiter]);
  if (step.field_sources.length) {
    values.push([
      "field sources",
      step.field_sources
        .map((item) => `${item.output_field} ← ${item.source_field}`)
        .join(", "),
    ]);
  }
  if (step.field) values.push(["field", step.field]);
  if (step.strategy) values.push(["strategy", step.strategy]);
  if (step.input_format) values.push(["input format", step.input_format]);
  if (step.path) values.push(["record path", step.path]);
  if (step.sources.length) values.push(["sources", step.sources.join(" + ")]);
  if (step.source) values.push(["source", step.source]);
  if (step.split_fields.length) {
    values.push([
      "split fields",
      step.split_fields
        .map((item) => `${item.output_field}[${item.index}]`)
        .join(", "),
    ]);
  }
  if (step.true_values.length) values.push(["true values", step.true_values.join(", ")]);
  if (step.false_values.length) values.push(["false values", step.false_values.join(", ")]);
  if (step.separator) values.push(["separator", JSON.stringify(step.separator)]);
  return values;
}

function FileField({
  label,
  hint,
  accept,
  value,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  accept: string;
  value: LoadedFile | null;
  disabled: boolean;
  onChange: (file: File) => void;
}) {
  return (
    <label className="file-field">
      <span className="file-label">
        <strong>{label}</strong>
        <small>{hint}</small>
      </span>
      <span className={value ? "file-name selected" : "file-name"}>
        {value?.name ?? "Choose file"}
      </span>
      <input
        className="file-input"
        type="file"
        aria-label={label}
        accept={accept}
        disabled={disabled}
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) onChange(file);
        }}
      />
    </label>
  );
}

function RepairResult({result}: {result: ValidationResult}) {
  const downloadPipeline = () => {
    if (!result.patched_pipeline) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(result.patched_pipeline, null, 2) + "\n"], {
        type: "application/json",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `driftpatch-${result.id}-pipeline.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className={`result ${result.status}`} aria-live="polite">
      <header className="result-header">
        <div>
          <h2>{statusTitle(result.status)}</h2>
          <code>{result.id}</code>
        </div>
        {result.patched_pipeline ? (
          <button type="button" className="download" onClick={downloadPipeline}>
            Download patched pipeline
          </button>
        ) : null}
      </header>

      {result.program?.steps.length ? (
        <div className="program" aria-label="Authorized changes">
          {result.program.steps.map((step, index) => {
            const parameters = stepParameters(step);
            return (
              <div className="program-step" key={`${step.operation}-${index}`}>
                <strong>{words(step.operation)}</strong>
                {parameters.length ? (
                  <dl>
                    {parameters.map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      {result.application ? (
        <dl className="receipt" aria-label="Configuration receipt">
          <div>
            <dt>{result.application.state === "applied" ? "Applied" : "Already active"}</dt>
            <dd>v{result.application.version} · {result.application.affected_outputs.join(", ")}</dd>
          </div>
          <div>
            <dt>Configuration</dt>
            <dd title={`${result.application.previous_sha256} → ${result.application.applied_sha256}`}>
              <code>{result.application.previous_sha256.slice(0, 10)} → {result.application.applied_sha256.slice(0, 10)}</code>
            </dd>
          </div>
          <div>
            <dt>Rollback</dt>
            <dd>{result.application.rollback_ready ? "snapshot stored" : "unavailable"}</dd>
          </div>
        </dl>
      ) : null}

      <div className="verification">
        <table>
          <caption>Verification</caption>
          <thead>
            <tr><th>Contract</th><th>Evidence</th><th>State</th></tr>
          </thead>
          <tbody>
            {result.checks.map((check) => (
              <tr key={check.name} className={check.passed ? "pass" : "fail"}>
                <td data-label="Contract">{check.name}</td>
                <td data-label="Evidence">{check.detail}</td>
                <td data-label="State">{check.passed ? "pass" : "fail"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export default function App() {
  const [label, setLabel] = useState("");
  const [files, setFiles] = useState<Files>(EMPTY_FILES);
  const [running, setRunning] = useState(false);
  const [loadingExample, setLoadingExample] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ValidationResult | null>(null);

  const setFile = async (key: FileKey, file: File) => {
    const source = key === "before" || key === "after";
    const format = sourceFormat(file.name);
    if (source && !format) {
      setError(`${key === "before" ? "Baseline" : "Current"} source must be a CSV or JSON file.`);
      return;
    }
    if (!source && !file.name.toLowerCase().endsWith(".json")) {
      setError(`${key === "pipeline" ? "Pipeline" : "Contract"} must be a JSON file.`);
      return;
    }
    try {
      const content = await readText(file);
      setFiles((current) => ({
        ...current,
        [key]: {name: file.name, content, ...(format ? {format} : {})},
      }));
      setResult(null);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The selected file could not be read.");
    }
  };

  const loadExample = async () => {
    if (running || loadingExample) return;
    setLoadingExample(true);
    setError(null);
    try {
      const example = await getExample();
      setLabel(example.label);
      setFiles({
        before: {name: `baseline.${example.before.format}`, ...example.before},
        after: {name: `current.${example.after.format}`, ...example.after},
        pipeline: {name: "pipeline.json", content: example.pipeline_json},
        contract: {name: "contract.json", content: example.contract_json},
      });
      setResult(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The example could not be loaded.");
    } finally {
      setLoadingExample(false);
    }
  };

  const run = async (event: React.FormEvent) => {
    event.preventDefault();
    if (running) return;
    setError(null);
    setResult(null);
    try {
      const name = label.trim();
      if (!name) throw new Error("Enter a chain name.");
      if (!files.before || !files.after || !files.pipeline || !files.contract) {
        throw new Error("Select the baseline, current source, pipeline and contract files.");
      }
      jsonObject(files.pipeline.content, "Pipeline");
      jsonObject(files.contract.content, "Contract");
      const submission: CustomRunSubmission = {
        label: name,
        before: {format: files.before.format!, content: files.before.content},
        after: {format: files.after.format!, content: files.after.content},
        pipeline_json: files.pipeline.content,
        contract_json: files.contract.content,
      };
      const encoded = JSON.stringify(submission);
      if (new Blob([encoded]).size > MAX_REQUEST_BYTES) {
        throw new Error("The complete request exceeds the 5 MiB limit.");
      }
      setRunning(true);
      const receipt = await startCustomRun(submission);
      setResult(await waitForTerminalResult(receipt.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The chain could not be repaired.");
    } finally {
      setRunning(false);
    }
  };

  return (
    <main className="app-shell">
      <header className="system-header">
        <a href="/" className="wordmark" aria-label="DriftPatch home">
          <span>DRIFT</span><span>PATCH</span>
        </a>
        <button type="button" className="example" onClick={loadExample} disabled={running || loadingExample}>
          {loadingExample ? "Loading…" : "Load example"}
        </button>
      </header>

      <section className="workspace">
        <header className="workspace-intro">
          <h1>Repair your data chain</h1>
          <p>Upload baseline and current CSV or JSON, plus the pipeline and contract JSON.</p>
        </header>

        <form onSubmit={run}>
          <label className="chain-name">
            <span>Chain name</span>
            <input
              aria-label="Chain name"
              value={label}
              maxLength={80}
              disabled={running}
              onChange={(event) => {setLabel(event.currentTarget.value); setResult(null);}}
              placeholder="e.g. Public transport feed"
            />
          </label>

          <div className="file-grid">
            <FileField label="Baseline source" hint="CSV or JSON" accept=".csv,.json" value={files.before} disabled={running} onChange={(file) => setFile("before", file)} />
            <FileField label="Current source" hint="CSV or JSON" accept=".csv,.json" value={files.after} disabled={running} onChange={(file) => setFile("after", file)} />
            <FileField label="Pipeline" hint="JSON configuration" accept=".json" value={files.pipeline} disabled={running} onChange={(file) => setFile("pipeline", file)} />
            <FileField label="Contract" hint="JSON invariants" accept=".json" value={files.contract} disabled={running} onChange={(file) => setFile("contract", file)} />
          </div>

          {error ? <p className="error" role="alert">{error}</p> : null}
          {running ? <p className="running" role="status">Inspecting and verifying…</p> : null}

          <div className="form-action">
            <button type="submit" disabled={running || loadingExample}>
              {running ? "Repairing…" : "Repair this chain"}
            </button>
          </div>
        </form>

        {result ? <RepairResult result={result} /> : null}
      </section>
    </main>
  );
}
