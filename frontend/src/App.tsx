import {useEffect, useState} from "react";

import {getRun, getScenarios, runScenario} from "./api";
import type {
  DriftReport,
  RepairPlan,
  ScenarioItem,
  ScenariosResponse,
  SourceProfile,
  ValidationResult,
} from "./types";

const POLL_INTERVAL_MS = 750;
const RUN_TIMEOUT_MS = 90_000;

const wait = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

async function waitForTerminalResult(scenarioId: string): Promise<ValidationResult> {
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const current = await getRun(scenarioId);
    if (current.status === "not_started") {
      throw new Error("This proof run has not been admitted.");
    }
    if (current.status !== "queued") return current;
    await wait(POLL_INTERVAL_MS);
  }
  throw new Error("The worker did not return terminal evidence within 90 seconds.");
}

const words = (value: string) => value.replaceAll("-", " ");
const delimiterName = (value: string | null) => value === "\t" ? "tab" : value ?? "n/a";

function SystemHeader({running, onRun}: {running: boolean; onRun: () => void}) {
  return (
    <header className="system-header">
      <a className="skip-link" href="#incident-proof">Skip to incident proof</a>
      <a href="/" className="wordmark" aria-label="DriftPatch home">
        <span>DRIFT</span><span>PATCH</span>
      </a>
      <span className="product-class">Public-source repair agent</span>
      <div className="runtime-stack" aria-label="Execution stack">
        <span>Gemini 3.5 Flash</span>
        <span>Google ADK 2</span>
      </div>
      <div className="run-control">
        <small>One execution per incident / UTC day</small>
        <button type="button" onClick={onRun} disabled={running}>
          {running ? "Running proof…" : "Run today’s proof"}
        </button>
      </div>
    </header>
  );
}

function IncidentIndex({
  items,
  selectedId,
  results,
  running,
  onSelect,
}: {
  items: ScenarioItem[];
  selectedId: string;
  results: Record<string, ValidationResult>;
  running: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="incident-index" aria-label="Benchmark incidents">
      <header>
        <span>Incident index</span>
        <strong>{String(items.length).padStart(2, "0")}</strong>
      </header>
      <ol>
        {items.map((item, index) => {
          const result = results[item.id];
          const selected = item.id === selectedId;
          return (
            <li key={item.id} className={selected ? "selected" : ""}>
              <button
                type="button"
                onClick={() => onSelect(item.id)}
                aria-current={selected ? "true" : undefined}
                disabled={running}
              >
                <span className="incident-sequence">{String(index + 1).padStart(2, "0")}</span>
                <span className="incident-name">
                  <strong>{words(item.id)}</strong>
                  <small>{item.title}</small>
                </span>
                <span className={`incident-state ${result?.status ?? "ready"}`}>
                  {result?.status ?? "ready"}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function CaseHeader({report}: {report: DriftReport}) {
  const delta = report.added_fields.length + report.removed_fields.length + Object.keys(report.type_changes).length;
  return (
    <header className="case-header">
      <div className="case-title">
        <span className="case-id">INCIDENT / {report.scenario_id}</span>
        <h1>{report.title}</h1>
      </div>
      <dl className="case-facts">
        <div><dt>Schema delta</dt><dd>{delta}</dd></div>
        <div><dt>Observed rows</dt><dd>{report.after.row_count}</dd></div>
        <div><dt>Required fields</dt><dd>{report.contract.required.length}</dd></div>
        <div><dt>Minimum rows</dt><dd>{report.contract.min_rows}</dd></div>
      </dl>
    </header>
  );
}

type PathState = "complete" | "active" | "pending" | "passed" | "review";

function ProofPath({running, result}: {running: boolean; result?: ValidationResult}) {
  const decisionState: PathState = result ? "complete" : running ? "active" : "pending";
  const gateState: PathState = result
    ? result.status === "repaired" || result.status === "unchanged" ? "passed" : "review"
    : "pending";
  const steps: {name: string; detail: string; state: PathState}[] = [
    {name: "Evidence captured", detail: "Two source profiles", state: "complete"},
    {name: "Decision bounded", detail: "One typed operation", state: decisionState},
    {name: "Contract gate", detail: "Every check required", state: gateState},
  ];
  return (
    <ol className="proof-path" aria-label="Repair proof path">
      {steps.map((step, index) => (
        <li key={step.name} className={step.state}>
          <span className="path-index">{index + 1}</span>
          <span><strong>{step.name}</strong><small>{step.detail}</small></span>
          <em>{step.state}</em>
        </li>
      ))}
    </ol>
  );
}

type FieldChange = "added" | "removed" | "changed" | "stable";

function fieldChange(report: DriftReport, field: string, side: "before" | "after"): FieldChange {
  if (side === "before" && report.removed_fields.includes(field)) return "removed";
  if (side === "after" && report.added_fields.includes(field)) return "added";
  if (field in report.type_changes) return "changed";
  return "stable";
}

function SourceSnapshot({
  label,
  profile,
  report,
  side,
}: {
  label: string;
  profile: SourceProfile;
  report: DriftReport;
  side: "before" | "after";
}) {
  return (
    <section className={`source-snapshot ${side}`} aria-label={`${label} source profile`}>
      <header>
        <div><span>{label}</span><strong>{side === "before" ? "Baseline" : "Observed"}</strong></div>
        <dl>
          <div><dt>Format</dt><dd>{profile.format}</dd></div>
          <div><dt>Delimiter</dt><dd>{delimiterName(profile.delimiter)}</dd></div>
          <div><dt>Rows</dt><dd>{profile.row_count}</dd></div>
        </dl>
      </header>
      <div className="source-table-wrap">
        <table>
          <thead><tr><th>Field</th><th>Type</th><th>Signal</th></tr></thead>
          <tbody>
            {profile.fields.length === 0 ? (
              <tr className="empty-row"><td colSpan={3}>No fields observed</td></tr>
            ) : profile.fields.map((field) => {
              const change = fieldChange(report, field.name, side);
              return (
                <tr key={field.name} className={change}>
                  <td>
                    <strong>{field.name}</strong>
                    <small>{field.example_values.slice(0, 2).join(" · ") || "no sample"}</small>
                  </td>
                  <td>{field.inferred_type}</td>
                  <td>
                    {change === "stable"
                      ? `${field.distinct_count} distinct`
                      : change}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <footer>{profile.record_path ? `Record path ${profile.record_path}` : "Root records"}</footer>
    </section>
  );
}

function DeltaSpine({report}: {report: DriftReport}) {
  const changes = report.added_fields.length + report.removed_fields.length + Object.keys(report.type_changes).length;
  return (
    <div className="delta-spine" aria-label={`${changes} observed schema changes`}>
      <span className="delta-minus">−{report.removed_fields.length}</span>
      <span className="delta-rule" aria-hidden="true"><i /></span>
      <strong>{changes}<small>schema<br />changes</small></strong>
      <span className="delta-rule" aria-hidden="true"><i /></span>
      <span className="delta-plus">+{report.added_fields.length}</span>
    </div>
  );
}

function EvidencePlane({report}: {report: DriftReport}) {
  return (
    <section className="evidence-plane">
      <header className="section-title">
        <span>Evidence</span>
        <h2>Observed source change</h2>
        <small>Profiles are measured before any decision is permitted.</small>
      </header>
      <div className="schema-comparison">
        <SourceSnapshot label="Source A" profile={report.before} report={report} side="before" />
        <DeltaSpine report={report} />
        <SourceSnapshot label="Source B" profile={report.after} report={report} side="after" />
      </div>
      <div className="failure-trace">
        <span>Failed contract</span>
        <code>{report.current_failure}</code>
      </div>
    </section>
  );
}

function planParameters(plan: RepairPlan): [string, string][] {
  const rows: [string, string][] = [];
  if (plan.field_sources.length) rows.push(["field sources", plan.field_sources.map((item) => `${item.output_field} ← ${item.source_field}`).join(", ")]);
  if (plan.delimiter) rows.push(["delimiter", delimiterName(plan.delimiter)]);
  if (plan.field) rows.push(["field", plan.field]);
  if (plan.strategy) rows.push(["strategy", plan.strategy]);
  if (plan.input_format) rows.push(["input format", plan.input_format]);
  if (plan.true_values.length) rows.push(["true values", plan.true_values.join(", ")]);
  if (plan.false_values.length) rows.push(["false values", plan.false_values.join(", ")]);
  if (plan.path) rows.push(["record path", plan.path]);
  if (plan.sources.length) rows.push(["sources", plan.sources.join(" + ")]);
  if (plan.source) rows.push(["source", plan.source]);
  if (plan.split_fields.length) rows.push(["split fields", plan.split_fields.map((item) => `${item.output_field}[${item.index}]`).join(", ")]);
  if (plan.separator) rows.push(["separator", JSON.stringify(plan.separator)]);
  return rows;
}

function DecisionStage({result, running}: {result?: ValidationResult; running: boolean}) {
  const plan = result?.plan;
  const parameters = plan ? planParameters(plan) : [];
  return (
    <section className={`decision-stage ${running ? "running" : ""}`} aria-busy={running}>
      <header className="section-title">
        <span>Decision</span>
        <h2>Bounded repair candidate</h2>
        <small>The agent can choose one allowlisted operation or escalate.</small>
      </header>
      {!plan ? (
        <div className="decision-pending" aria-live="polite">
          <div className="decision-aperture">
            <span>{running ? "Evaluating evidence" : "No decision yet"}</span>
            <strong>{running ? "A typed candidate is being resolved." : "Run today’s proof to resolve this incident."}</strong>
            {running && <i className="activity-line" aria-hidden="true" />}
          </div>
          <ul className="boundary-list" aria-label="Enforced boundaries">
            <li><strong>1</strong><span>operation maximum</span></li>
            <li><strong>0</strong><span>arbitrary commands</span></li>
            <li><strong>0</strong><span>automatic merges</span></li>
          </ul>
        </div>
      ) : (
        <div className="decision-result enter" aria-live="polite">
          <div className="operation-lockup">
            <span>Selected operation</span>
            <strong>{plan.operation}</strong>
          </div>
          <div className="decision-detail">
            <div className="rationale-block">
              <span>Rationale</span>
              <p>{plan.rationale}</p>
            </div>
            {parameters.length > 0 ? (
              <dl className="parameter-table">
                {parameters.map(([label, value]) => (
                  <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                ))}
              </dl>
            ) : null}
            <div className="evidence-block">
              <span>Decision evidence</span>
              <ul>{plan.evidence.map((evidence) => <li key={evidence}>{evidence}</li>)}</ul>
            </div>
          </div>
          <div className="boundary-stamp">Allowlist enforced · write path closed · merge disabled</div>
        </div>
      )}
    </section>
  );
}

function ContractSpec({report}: {report: DriftReport}) {
  return (
    <dl className="contract-spec">
      <div><dt>Required</dt><dd>{report.contract.required.join(", ")}</dd></div>
      <div><dt>Unique key</dt><dd>{report.contract.unique_key}</dd></div>
      <div><dt>Minimum rows</dt><dd>{report.contract.min_rows}</dd></div>
      <div><dt>Typed fields</dt><dd>{Object.keys(report.contract.types).length}</dd></div>
    </dl>
  );
}

function GateStage({result, report}: {result?: ValidationResult; report: DriftReport}) {
  const safe = result?.status === "repaired" || result?.status === "unchanged";
  const outcomeCopy = result?.status === "repaired"
    ? "Every contract passed. The proposal is safe to review."
    : result?.status === "unchanged"
      ? "The source remains compatible. No repair is justified."
      : result
        ? "No repair was authorized. Human review is required."
        : "Success is unavailable until every contract passes.";
  return (
    <section className="gate-stage">
      <header className="section-title">
        <span>Verify</span>
        <h2>Deterministic contract gate</h2>
        <small>A fluent decision cannot override a failed check.</small>
      </header>
      <ContractSpec report={report} />
      {result ? (
        <div className="gate-result enter" aria-live="polite">
          <table className="check-table">
            <thead><tr><th>Contract</th><th>Evidence</th><th>State</th></tr></thead>
            <tbody>
              {result.checks.map((check) => (
                <tr key={check.name} className={check.passed ? "pass" : "fail"}>
                  <td>{check.name}</td><td>{check.detail}</td><td>{check.passed ? "pass" : "fail"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.trigger === "cloud-scheduler" && result.source_sha256 ? (
            <div className="source-receipt">
              <span>Ambient receipt</span>
              <strong>Cloud Scheduler</strong>
              <code>{result.source_sha256}</code>
            </div>
          ) : null}
          <div className={`gate-outcome ${result.status}`}>
            <span>{safe ? "Gate open" : "Gate closed"}</span>
            <strong>{result.status}</strong>
            <p>{outcomeCopy}</p>
          </div>
        </div>
      ) : (
        <div className="gate-locked">
          <span>Gate locked</span><p>{outcomeCopy}</p>
        </div>
      )}
    </section>
  );
}

function ProofFooter() {
  return (
    <footer className="proof-footer">
      <span><strong>≤1</strong> operation candidate</span>
      <span><strong>All</strong> checks required</span>
      <span><strong>0</strong> automatic merges</span>
      <span><strong>90s</strong> execution budget</span>
    </footer>
  );
}

export default function App() {
  const [data, setData] = useState<ScenariosResponse | null>(null);
  const [selectedId, setSelectedId] = useState("column-rename");
  const [results, setResults] = useState<Record<string, ValidationResult>>({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setError(null);
    getScenarios()
      .then((scenarios) => {
        if (!active) return;
        setData(scenarios);
        if (!scenarios.items.some((item) => item.id === selectedId)) {
          setSelectedId(scenarios.items[0]?.id ?? "");
        }
      })
      .catch((reason: Error) => active && setError(reason.message));
    return () => { active = false; };
  }, [loadAttempt]);

  const selected = data?.items.find((item) => item.id === selectedId);

  const handleRun = async () => {
    if (!selected || running) return;
    setRunning(true);
    setError(null);
    try {
      await runScenario(selected.id);
      const result = await waitForTerminalResult(selected.id);
      setResults((current) => ({...current, [selected.id]: result}));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The proof run failed.");
    } finally {
      setRunning(false);
    }
  };

  if (!data || !selected) {
    return (
      <main className={`loading-screen ${error ? "load-error" : ""}`} aria-busy={!error}>
        <span className="loading-mark">DRIFT<br />PATCH</span>
        <div className="loading-message" role={error ? "alert" : "status"} aria-live={error ? "assertive" : "polite"}>
          <strong>{error ?? "Loading incident evidence"}</strong>
          {error ? (
            <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry incident index</button>
          ) : <i aria-hidden="true" />}
        </div>
      </main>
    );
  }

  const result = results[selected.id];
  return (
    <main className="app-shell">
      <SystemHeader running={running} onRun={handleRun} />
      <div className="workbench">
        <IncidentIndex
          items={data.items}
          selectedId={selected.id}
          results={results}
          running={running}
          onSelect={(id) => {setSelectedId(id); setError(null);}}
        />
        <article className="proof-canvas" id="incident-proof" tabIndex={-1} key={selected.id}>
          <CaseHeader report={selected.report} />
          <ProofPath running={running} result={result} />
          {error && <div className="error-banner" role="alert"><strong>Run failed</strong><span>{error}</span></div>}
          <EvidencePlane report={selected.report} />
          <div className="proof-resolution">
            <DecisionStage result={result} running={running} />
            <GateStage result={result} report={selected.report} />
          </div>
          <ProofFooter />
        </article>
      </div>
    </main>
  );
}
