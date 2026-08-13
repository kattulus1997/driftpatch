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
      <div className="run-control">
        <button
          type="button"
          onClick={onRun}
          disabled={running}
          title="One execution per incident and UTC day"
        >
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
      <header>Incidents</header>
      <ol>
        {items.map((item) => {
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
                <span className="incident-name">
                  <strong>{words(item.id)}</strong>
                </span>
                {result ? (
                  <span className={`incident-state ${result.status}`}>{result.status}</span>
                ) : null}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function CaseHeader({report}: {report: DriftReport}) {
  return (
    <header className="case-header">
      <h1>{report.title}</h1>
    </header>
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
  profile,
  report,
  side,
}: {
  profile: SourceProfile;
  report: DriftReport;
  side: "before" | "after";
}) {
  const name = side === "before" ? "Baseline" : "Observed";
  return (
    <section className={`source-snapshot ${side}`} aria-label={`${name} source profile`}>
      <header>
        <strong>{name}</strong>
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
      <span aria-hidden="true">→</span>
    </div>
  );
}

function EvidencePlane({report}: {report: DriftReport}) {
  return (
    <section className="evidence-plane">
      <header className="section-title">
        <h2>Source change</h2>
      </header>
      <div className="schema-comparison">
        <SourceSnapshot profile={report.before} report={report} side="before" />
        <DeltaSpine report={report} />
        <SourceSnapshot profile={report.after} report={report} side="after" />
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
        <h2>Repair</h2>
      </header>
      {!plan ? (
        <div className="decision-pending" aria-live="polite">
          <p>{running ? "Evaluating evidence…" : "Awaiting proof."}</p>
          {running && <i className="activity-line" aria-hidden="true" />}
        </div>
      ) : (
        <div className="decision-result enter" aria-live="polite">
          <div className="operation-lockup">
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
              <span>Evidence</span>
              <ul>{plan.evidence.map((evidence) => <li key={evidence}>{evidence}</li>)}</ul>
            </div>
          </div>
          {result.application ? (
            <dl className="application-receipt">
              <div>
                <dt>{result.application.state === "applied" ? "Applied" : "Already active"}</dt>
                <dd>v{result.application.version} · {result.application.affected_outputs.join(", ")}</dd>
              </div>
              <div>
                <dt>Config</dt>
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
        </div>
      )}
    </section>
  );
}

function GateStage({result}: {result?: ValidationResult}) {
  if (!result) return null;
  const outcomeCopy = result.status === "unchanged"
      ? "The source remains compatible. No repair is justified."
      : result.status === "repaired"
        ? null
        : "No repair was authorized. Human review is required.";
  return (
    <section className="gate-stage">
      <header className="section-title">
        <h2>Contract checks</h2>
      </header>
      <div className="gate-result enter" aria-live="polite">
        <table className="check-table">
          <thead><tr><th>Contract</th><th>Evidence</th><th>State</th></tr></thead>
          <tbody>
            {result.checks.map((check) => (
              <tr key={check.name} className={check.passed ? "pass" : "fail"}>
                <td>{check.name.replaceAll("_", " ")}</td><td>{check.detail}</td><td>{check.passed ? "pass" : "fail"}</td>
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
        {outcomeCopy ? <p className={`gate-outcome ${result.status}`}>{outcomeCopy}</p> : null}
      </div>
    </section>
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
          {error && <div className="error-banner" role="alert"><strong>Run failed</strong><span>{error}</span></div>}
          <EvidencePlane report={selected.report} />
          {running || result ? (
            <div className={`proof-resolution ${result ? "resolved" : ""}`}>
              <DecisionStage result={result} running={running} />
              <GateStage result={result} />
            </div>
          ) : null}
        </article>
      </div>
    </main>
  );
}
