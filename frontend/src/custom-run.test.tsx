import {cleanup, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, expect, test, vi} from "vitest";

import App from "./App";

const RUN_ID = "custom_0123456789abcdef0123456789abcdef";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function upload(label: string, name: string, content: string, type: string) {
  fireEvent.change(screen.getByLabelText(label), {
    target: {files: [new File([content], name, {type})]},
  });
}

test("submits four judge-selected files and renders a composed receipt", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/runs" && init?.method === "POST") {
      return new Response(JSON.stringify({
        id: RUN_ID,
        status: "queued",
        status_url: `/api/runs/${RUN_ID}`,
      }), {status: 202, headers: {"Content-Type": "application/json"}});
    }
    if (url === `/api/runs/${RUN_ID}`) {
      return new Response(JSON.stringify({
        id: RUN_ID,
        status: "repaired",
        program: {
          decision: "repair",
          steps: [
            {operation: "set_source_format", format: "json", field_sources: [], true_values: [], false_values: [], sources: [], split_fields: []},
            {operation: "set_record_path", path: "rows", field_sources: [], true_values: [], false_values: [], sources: [], split_fields: []},
          ],
          confidence: 1,
          evidence: ["source format: csv to json", "authorized candidate: c_123456789abc"],
          rationale: "verified_repair",
        },
        checks: [{name: "required:name", passed: true, detail: "missing=0"}],
        transformed_rows: 1,
        evidence_complete: true,
        summary: "repaired",
        patched_pipeline: {format: "json", record_path: "rows", fields: {id: "id", name: "name"}},
        patched_pipeline_hash: "3".repeat(64),
        application: {
          state: "applied",
          version: 1,
          affected_outputs: ["id", "name"],
          previous_sha256: "1".repeat(64),
          applied_sha256: "2".repeat(64),
          rollback_ready: true,
        },
      }), {status: 200, headers: {"Content-Type": "application/json"}});
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByLabelText("Chain name"), {target: {value: "Judge chain"}});
  upload("Baseline source", "before.csv", "id,name\n1,Ada\n", "text/csv");
  upload("Current source", "after.json", '{"rows":[{"id":1,"name":"Ada"}]}', "application/json");
  upload("Pipeline", "pipeline.json", '{"format":"csv","fields":{"id":"id","name":"name"}}', "application/json");
  upload("Contract", "contract.json", '{"required":["id","name"],"types":{"id":"integer","name":"string"},"unique_key":"id"}', "application/json");
  await waitFor(() => expect(screen.getByText("after.json")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", {name: "Repair this chain"}));

  expect(await screen.findByRole("heading", {name: "Repair verified"})).toBeInTheDocument();
  expect(screen.getByText("set source format")).toBeInTheDocument();
  expect(screen.getByText("set record path")).toBeInTheDocument();
  expect(screen.getByText("required:name")).toBeInTheDocument();
  expect(screen.getByText("required:name").closest("td")).toHaveAttribute(
    "data-label",
    "Contract",
  );
  expect(screen.getByText("missing=0").closest("td")).toHaveAttribute(
    "data-label",
    "Evidence",
  );
  expect(screen.getByText("pass").closest("td")).toHaveAttribute("data-label", "State");
  expect(screen.getByText("v1 · id, name")).toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Download patched pipeline"})).toBeEnabled();

  const post = fetchMock.mock.calls.find(([url, init]) =>
    String(url) === "/api/runs" && (init as RequestInit | undefined)?.method === "POST"
  );
  expect(post).toBeDefined();
  const body = JSON.parse(String((post?.[1] as RequestInit).body));
  expect(body).toMatchObject({
    label: "Judge chain",
    before: {format: "csv", content: "id,name\n1,Ada\n"},
    after: {format: "json", content: '{"rows":[{"id":1,"name":"Ada"}]}'},
  });
  expect(JSON.parse(body.pipeline_json).format).toBe("csv");
});

test("rejects unsupported source files before making a request", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);

  upload("Baseline source", "before.xml", "<rows />", "application/xml");

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Baseline source must be a CSV or JSON file.",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});
