import {useEffect, useMemo, useState} from "react";

import {getRuns, getScenarios, runScenario} from "./api";
import type {
  DriftReport,
  RepairPlan,
  ScenarioItem,
  ScenariosResponse,
  SourceProfile,
  ValidationResult,
} from "./types";

const emptySummary: ScenariosResponse["summary"] = {
  decisions: 10,
  repaired: 8,
  escalated: 2,
  auto_merges: 0,
};

function Arrow() {
  return (
    <svg viewBox="0 0 32 16" aria-hidden="true" className="arrow">
      <path d="M1 8h28M23 2l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function StageHeader({number, title, detail}: {number: string; title: string; detail: string}) {
  return (
    <header className="stage-header">
      <span className="stage-number">{number}</span>
      <span>
        <strong>{title}</strong>
        <small>{detail}</small>
      </span>
      {number !== "03" && <Arrow />}
    </header>
  );
}

function IncidentRail({
  items,
  selectedId,
  onSelect,
}: {
  items: ScenarioItem[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <aside className="incident-rail" aria-label="Benchmark incidents">
      <h2>INCIDENTS</h2>
      <ol>
        {items.map((item, index) => (
          <li key={item.id} className={item.id === selectedId ? "selected" : ""}>
            <button type="button" onClick={() => onSelect(item.id)} aria-current={item.id === selectedId}>
              <span className="incident-number">{String(index + 1).padStart(2, "0")}</span>
              <span className="incident-copy">
                <strong>{item.expected_status}</strong>
                <small>{item.id.replaceAll("-", " ")}</small>
              </span>
              <span className="rail-arrow" aria-hidden="true">›</span>
            </button>
          </li>
        ))}
      </ol>
    </aside>
  );
}

function SourceFields({label, profile, changed}: {label: string; profile: SourceProfile; changed: Set<string>}) {
  return (
    <section className="source-fields">
      <header>
        <strong>{label}</strong>
        <span>{profile.format}{profile.delimiter ? ` · ${profile.delimiter === "\t" ? "TAB" : profile.delimiter}` : ""}</span>
      </header>
      <ol>
        {profile.fields.map((field) => (
          <li key={field.name} className={changed.has(field.name) ? "changed" : ""}>
            <span className="field-name">{field.name}</span>
            <span className="field-type">{field.inferred_type}</span>
            <small>{field.example_values.slice(0, 2).join(" · ")}</small>
          </li>
        ))}
      </ol>
      <footer>{profile.row_count} observed rows{profile.record_path ? ` · ${profile.record_path}` : ""}</footer>
    </section>
  );
}

function EvidenceStage({report}: {report: DriftReport}) {
  return (
    <section className="stage evidence-stage">
      <StageHeader number="01" title="EVIDENCE" detail="DETECTED DRIFT & FAILURE" />
      <div className="stage-body">
        <div className="incident-heading">
          <span>SOURCE INCIDENT</span>
          <strong>{report.title}</strong>
        </div>
        <div className="schema-diff">
          <SourceFields label="BEFORE" profile={report.before} changed={new Set(report.removed_fields)} />
          <SourceFields label="AFTER" profile={report.after} changed={new Set(report.added_fields)} />
        </div>
        <div className="failure-block">
          <span>CONTRACT FAILURE</span>
          <p>{report.current_failure}</p>
        </div>
      </div>
    </section>
  );
}

function planParameters(plan: RepairPlan): [string, string][] {
  const rows: [string, string][] = [];
  if (plan.field_sources.length) rows.push(["field sources", plan.field_sources.map((item) => `${item.output_field} ← ${item.source_field}`).join(", ")]);
  if (plan.delimiter) rows.push(["delimiter", plan.delimiter === "\t" ? "TAB" : plan.delimiter]);
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

function PatchStage({result, running}: {result?: ValidationResult; running: boolean}) {
  const plan = result?.plan;
  return (
    <section className={`stage patch-stage ${running ? "is-running" : ""}`} aria-busy={running}>
      <StageHeader number="02" title="PATCH" detail="ONE BOUNDED REPAIR CHOICE" />
      <div className="stage-body">
        {!plan ? (
          <div className="pending-state">
            <strong>{running ? "GEMINI IS PLANNING" : "AWAITING LIVE DECISION"}</strong>
            <p>{running ? "The source profile and failed contract are being evaluated." : "Run the selected incident. Gemini may choose one typed operation or escalate."}</p>
          </div>
        ) : (
          <div className="result-content enter">
            <div className="chosen-operation">
              <span>CHOSEN OPERATION</span>
              <strong>{plan.operation}</strong>
              <small>{Math.round(plan.confidence * 100)}% EVIDENCE CONFIDENCE</small>
            </div>
            {planParameters(plan).length > 0 && (
              <dl className="parameter-list">
                {planParameters(plan).map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            )}
            <div className="rationale">
              <span>RATIONALE</span>
              <p>{plan.rationale}</p>
            </div>
            <div className="evidence-list">
              <span>OBSERVED EVIDENCE</span>
              <ul>
                {plan.evidence.map((evidence) => <li key={evidence}>{evidence}</li>)}
              </ul>
            </div>
            <div className="safety-boundaries">
              <span>ENFORCED BOUNDARIES</span>
              <ul>
                <li>One allowlisted operation</li>
                <li>No arbitrary code or shell</li>
                <li>No automatic merge</li>
              </ul>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function VerifyStage({result, report}: {result?: ValidationResult; report: DriftReport}) {
  return (
    <section className="stage verify-stage">
      <StageHeader number="03" title="VERIFY" detail="DETERMINISTIC CONTRACT GATE" />
      <div className="stage-body">
        {!result ? (
          <div className="contract-preview">
            <span>REQUIRED CONTRACT</span>
            <dl>
              <div><dt>required</dt><dd>{report.contract.required.join(", ")}</dd></div>
              <div><dt>unique</dt><dd>{report.contract.unique_key}</dd></div>
              <div><dt>minimum rows</dt><dd>{report.contract.min_rows}</dd></div>
            </dl>
            <p>Success remains unavailable until every check passes.</p>
          </div>
        ) : (
          <div className="result-content enter">
            <span className="table-label">VALIDATION RESULTS</span>
            <div className="check-table" role="table" aria-label="Validation checks">
              {result.checks.map((check) => (
                <div role="row" key={check.name} className={check.passed ? "pass" : "fail"}>
                  <span role="cell">{check.name}</span>
                  <strong role="cell">{check.passed ? "PASS" : "FAIL"}</strong>
                  <small role="cell">{check.detail}</small>
                </div>
              ))}
            </div>
            <div className={`outcome ${result.status}`}>
              <span>OUTCOME</span>
              <strong>{result.status}</strong>
              <p>{result.status === "repaired" ? "Contract gate passed. The bounded patch is safe to propose." : "Contract gate did not authorize a repair. Human review is required."}</p>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function ProofStrip({summary, running, onRun}: {summary: ScenariosResponse["summary"]; running: boolean; onRun: () => void}) {
  const metrics = [
    [summary.decisions, "DECISIONS"],
    [summary.repaired, "REPAIRED"],
    [summary.escalated, "ESCALATED"],
    [summary.auto_merges, "AUTO-MERGES"],
  ];
  return (
    <footer className="proof-strip">
      <div className="proof-label">BENCHMARK<br />PROOF</div>
      {metrics.map(([value, label]) => (
        <div className="proof-metric" key={label}>
          <strong>{label === "DECISIONS" ? `${value}/10` : value}</strong>
          <span>{label}</span>
        </div>
      ))}
      <button className="run-button" type="button" onClick={onRun} disabled={running}>
        <span>{running ? "AGENT RUNNING" : "RUN INCIDENT"}</span>
        <Arrow />
      </button>
    </footer>
  );
}

export default function App() {
  const [data, setData] = useState<ScenariosResponse | null>(null);
  const [selectedId, setSelectedId] = useState("column-rename");
  const [results, setResults] = useState<Record<string, ValidationResult>>({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([getScenarios(), getRuns().catch(() => ({items: []}))])
      .then(([scenarios, runs]) => {
        if (!active) return;
        setData(scenarios);
        setResults(Object.fromEntries(runs.items.map((run) => [run.scenario_id, run])));
        if (!scenarios.items.some((item) => item.id === selectedId)) {
          setSelectedId(scenarios.items[0]?.id ?? "");
        }
      })
      .catch((reason: Error) => active && setError(reason.message));
    return () => { active = false; };
  }, []);

  const selected = useMemo(
    () => data?.items.find((item) => item.id === selectedId),
    [data, selectedId],
  );

  const handleRun = async () => {
    if (!selected || running) return;
    setRunning(true);
    setError(null);
    try {
      const result = await runScenario(selected.id);
      setResults((current) => ({...current, [selected.id]: result}));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Agent run failed");
    } finally {
      setRunning(false);
    }
  };

  if (!data || !selected) {
    return (
      <main className="loading-screen">
        <strong>DRIFTPATCH</strong>
        <span>{error ?? "LOADING PUBLIC BENCHMARK"}</span>
      </main>
    );
  }

  const result = results[selected.id];
  return (
    <main className="app-shell">
      <header className="topbar">
        <a href="/" className="wordmark">DRIFTPATCH</a>
        <span className="product-label">PUBLIC DATA REPAIR AGENT</span>
        <div className="runtime-labels" aria-label="Runtime stack">
          <span>GEMINI 3.5 FLASH</span>
          <span>GOOGLE ADK 2</span>
        </div>
      </header>
      <section className="headline-band">
        <h1>When the source changes, the pipeline shouldn't break.</h1>
      </section>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="workspace">
        <IncidentRail items={data.items} selectedId={selected.id} onSelect={(id) => {setSelectedId(id); setError(null);}} />
        <div className="trace-grid" key={selected.id}>
          <EvidenceStage report={selected.report} />
          <PatchStage result={result} running={running} />
          <VerifyStage result={result} report={selected.report} />
        </div>
      </div>
      <ProofStrip summary={data.summary ?? emptySummary} running={running} onRun={handleRun} />
    </main>
  );
}
