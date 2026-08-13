import type {RunReceipt, RunStatus, ScenariosResponse} from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `Request failed with status ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const getScenarios = () => request<ScenariosResponse>("/api/scenarios");

export const runScenario = (scenarioId: string) =>
  request<RunReceipt>(`/api/scenarios/${encodeURIComponent(scenarioId)}/run`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
  });

export const getRun = (scenarioId: string) =>
  request<RunStatus>(`/api/scenarios/${encodeURIComponent(scenarioId)}/run`);
