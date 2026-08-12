import {render, screen, waitFor} from "@testing-library/react";
import {afterEach, expect, test, vi} from "vitest";

import App from "./App";

afterEach(() => vi.restoreAllMocks());

test("renders the operational stages from the real API shape", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    return new Response(JSON.stringify(url.endsWith("/api/runs") ? {items: []} : {
      summary: {decisions: 10, repaired: 8, escalated: 2, auto_merges: 0},
      items: [{
        id: "column-rename",
        title: "A municipality renamed name to full_name",
        expected_status: "repaired",
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
  await waitFor(() => expect(screen.getByText("EVIDENCE")).toBeInTheDocument());
  expect(screen.getByText("PATCH")).toBeInTheDocument();
  expect(screen.getByText("VERIFY")).toBeInTheDocument();
  expect(screen.getByRole("button", {name: /run incident/i})).toBeEnabled();
});
