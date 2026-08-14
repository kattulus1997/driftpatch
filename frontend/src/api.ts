import type {
  CustomRunReceipt,
  CustomRunStatus,
  CustomRunSubmission,
} from "./types";

function problemMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = payload.detail;
    if (typeof detail === "string") return detail;
    if (
      detail &&
      typeof detail === "object" &&
      "message" in detail &&
      typeof detail.message === "string"
    ) {
      return detail.message;
    }
  }
  return `Request failed with status ${status}`;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    throw new Error(problemMessage(payload, response.status));
  }
  return response.json() as Promise<T>;
}

export const getExample = () =>
  request<CustomRunSubmission>("/api/examples/column-rename");

export const startCustomRun = (submission: CustomRunSubmission) =>
  request<CustomRunReceipt>("/api/runs", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(submission),
  });

export const getCustomRun = (runId: string) =>
  request<CustomRunStatus>(`/api/runs/${encodeURIComponent(runId)}`);
