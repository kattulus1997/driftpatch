import {cleanup, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, expect, test, vi} from "vitest";

import App from "./App";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("renders the proof path from the real API shape", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    return new Response(JSON.stringify({
      items: [{
        id: "column-rename",
        title: "A municipality renamed name to full_name",
        report: {
          scenario_id: "column-rename",
          title: "A municipality renamed name to full_name",
          before: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          after: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          added_fields: ["full_name"],
          removed_fields: ["name"],
          type_changes: {},
          current_failure: "required:name: missing=3 rows=3",
          contract: {required: ["id", "name"], types: {id: "integer", name: "string"}, unique_key: "id", min_rows: 3},
        },
      }],
    }), {status: 200, headers: {"Content-Type": "application/json"}});
  }));

  render(<App />);
  await waitFor(() => expect(screen.getByText("Evidence")).toBeInTheDocument());
  expect(screen.getByText("Decision")).toBeInTheDocument();
  expect(screen.getByText("Verify")).toBeInTheDocument();
  expect(screen.getByText(/one execution per incident/i)).toBeInTheDocument();
  expect(screen.getByRole("button", {name: /run today’s proof/i})).toBeEnabled();
});

test("queues an incident, polls its receipt, and renders terminal evidence", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/run") && init?.method === "POST") {
      return new Response(JSON.stringify({
        id: "event-1",
        scenario_id: "column-rename",
        status: "queued",
      }), {status: 202, headers: {"Content-Type": "application/json"}});
    }
    if (url.endsWith("/api/scenarios/column-rename/run") && fetchMock.mock.calls.length > 1) {
      return new Response(JSON.stringify({
        id: "event-1",
        scenario_id: "column-rename",
        status: "repaired",
        plan: {
          operation: "update_field_sources",
          field_sources: [{output_field: "name", source_field: "full_name"}],
          delimiter: null,
          field: null,
          strategy: null,
          input_format: null,
          true_values: [],
          false_values: [],
          path: null,
          sources: [],
          source: null,
          split_fields: [],
          separator: null,
          confidence: 1,
          evidence: ["full_name observed"],
          rationale: "Observed rename",
        },
        checks: [{name: "required:name", passed: true, detail: "missing=0"}],
        transformed_rows: 3,
        evidence_complete: true,
        summary: "repaired",
        trigger: "cloud-scheduler",
        source_sha256: "da3e209b6e97103c43bc5045fce139503d266cf7fc3041b6132c652ac376f196",
      }), {status: 200, headers: {"Content-Type": "application/json"}});
    }
    return new Response(JSON.stringify({
      items: [{
        id: "column-rename",
        title: "A municipality renamed name to full_name",
        report: {
          scenario_id: "column-rename",
          title: "A municipality renamed name to full_name",
          before: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          after: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          added_fields: ["full_name"],
          removed_fields: ["name"],
          type_changes: {},
          current_failure: "required:name: missing=3 rows=3",
          contract: {required: ["id", "name"], types: {id: "integer", name: "string"}, unique_key: "id", min_rows: 3},
        },
      }],
    }), {status: 200, headers: {"Content-Type": "application/json"}});
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  const button = await screen.findByRole("button", {name: /run today’s proof/i});
  fireEvent.click(button);

  await waitFor(() => expect(screen.getByText("update_field_sources")).toBeInTheDocument());
  expect(screen.getByText("required:name")).toBeInTheDocument();
  expect(screen.getByText("Gate open")).toBeInTheDocument();
  expect(screen.getByText("Cloud Scheduler")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/scenarios/column-rename/run",
    expect.objectContaining({method: "POST"}),
  );
  expect(fetchMock).toHaveBeenCalledWith("/api/scenarios/column-rename/run", undefined);
});

test("offers a retry when the incident index cannot load", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({detail: "Index unavailable"}), {
      status: 503,
      headers: {"Content-Type": "application/json"},
    }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      items: [{
        id: "column-rename",
        title: "A municipality renamed name to full_name",
        report: {
          scenario_id: "column-rename",
          title: "A municipality renamed name to full_name",
          before: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          after: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          added_fields: ["full_name"],
          removed_fields: ["name"],
          type_changes: {},
          current_failure: "required:name: missing=3 rows=3",
          contract: {required: ["id", "name"], types: {id: "integer", name: "string"}, unique_key: "id", min_rows: 3},
        },
      }],
    }), {status: 200, headers: {"Content-Type": "application/json"}}));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Index unavailable");
  fireEvent.click(screen.getByRole("button", {name: /retry incident index/i}));
  expect(await screen.findByText("Observed source change")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("fails closed when a queued proof has no admitted run", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/run") && init?.method === "POST") {
      return new Response(JSON.stringify({
        id: "event-1",
        scenario_id: "column-rename",
        status: "queued",
      }), {status: 202, headers: {"Content-Type": "application/json"}});
    }
    if (url.endsWith("/api/scenarios/column-rename/run")) {
      return new Response(JSON.stringify({
        id: "event-1",
        scenario_id: "column-rename",
        status: "not_started",
      }), {status: 200, headers: {"Content-Type": "application/json"}});
    }
    return new Response(JSON.stringify({
      items: [{
        id: "column-rename",
        title: "A municipality renamed name to full_name",
        report: {
          scenario_id: "column-rename",
          title: "A municipality renamed name to full_name",
          before: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          after: {format: "csv", delimiter: ",", record_path: null, row_count: 3, fields: []},
          added_fields: ["full_name"],
          removed_fields: ["name"],
          type_changes: {},
          current_failure: "required:name: missing=3 rows=3",
          contract: {required: ["id", "name"], types: {id: "integer", name: "string"}, unique_key: "id", min_rows: 3},
        },
      }],
    }), {status: 200, headers: {"Content-Type": "application/json"}});
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(await screen.findByRole("button", {name: /run today’s proof/i}));

  expect(await screen.findByRole("alert")).toHaveTextContent("This proof run has not been admitted.");
});
