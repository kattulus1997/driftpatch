import {cleanup, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, expect, test, vi} from "vitest";

import App from "./App";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("opens on one concise custom-chain workspace", () => {
  vi.stubGlobal("fetch", vi.fn());

  render(<App />);

  expect(screen.getByRole("heading", {name: "Repair your data chain"})).toBeInTheDocument();
  expect(screen.getByLabelText("Baseline source")).toHaveAttribute("accept", ".csv,.json");
  expect(screen.getByLabelText("Current source")).toHaveAttribute("accept", ".csv,.json");
  expect(screen.getByLabelText("Pipeline")).toHaveAttribute("accept", ".json");
  expect(screen.getByLabelText("Contract")).toHaveAttribute("accept", ".json");
  expect(screen.queryByText(/phase/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/^01|^02|^03/)).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", {name: "Source change"})).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", {name: "Contract checks"})).not.toBeInTheDocument();
});

test("loads the curated example into fields that remain replaceable", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    label: "Municipal names",
    before: {format: "csv", content: "id,name\n1,Ada\n"},
    after: {format: "csv", content: "id,full_name\n1,Ada\n"},
    pipeline_json: '{"format":"csv","fields":{"id":"id","name":"name"}}',
    contract_json: '{"required":["id","name"],"types":{"id":"integer","name":"string"},"unique_key":"id"}',
  }), {status: 200, headers: {"Content-Type": "application/json"}}));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(screen.getByRole("button", {name: "Load example"}));

  expect(await screen.findByDisplayValue("Municipal names")).toBeInTheDocument();
  expect(screen.getByText("baseline.csv")).toBeInTheDocument();
  expect(screen.getByText("current.csv")).toBeInTheDocument();
  expect(screen.getByText("pipeline.json")).toBeInTheDocument();
  expect(screen.getByText("contract.json")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith("/api/examples/column-rename", undefined);
});

test("renders stable problem details returned by custom admission", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: {code: "invalid_baseline", message: "Baseline does not satisfy the contract."},
  }), {status: 422, headers: {"Content-Type": "application/json"}}));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(screen.getByRole("button", {name: "Load example"}));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Baseline does not satisfy the contract.",
  );
  expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
});
