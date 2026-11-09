import "server-only";

import { auth } from "@/auth";
import type { components } from "@/lib/api-types";

export type Schemas = components["schemas"];
export type MessageSummary = Schemas["MessageSummary"];
export type MessageDetail = Schemas["MessageDetail"];
export type MessagePage = Schemas["MessagePage"];
export type MessageStats = Schemas["MessageStats"];
export type Connector = Schemas["ConnectorOut"];
export type Reply = Schemas["ReplyOut"];
export type Draft = Schemas["DraftOut"];

/** The backend is asleep / starting / unreachable -> show the waking-up screen. */
export class BackendUnavailable extends Error {}

/** The backend answered with an error body {"error": {code, message, details}}. */
export class BackendError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

const DATA_TIMEOUT_MS = 90_000; // Render Free cold start is ~1 min
const HEALTH_TIMEOUT_MS = 3_000;

function baseUrl(): string {
  const url = process.env.BACKEND_URL;
  if (!url) throw new Error("BACKEND_URL is not set (frontend/.env.local)");
  return url.replace(/\/+$/, "");
}

async function actor(): Promise<string> {
  const session = await auth();
  return session?.actor ?? "password:owner";
}

async function call<T>(
  path: string,
  init: { method?: string; body?: unknown; timeoutMs?: number } = {},
): Promise<T> {
  const token = process.env.BACKEND_API_TOKEN;
  if (!token) throw new Error("BACKEND_API_TOKEN is not set (frontend/.env.local)");
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      method: init.method ?? "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        "X-Actor": await actor(),
        ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(init.timeoutMs ?? DATA_TIMEOUT_MS),
    });
  } catch {
    throw new BackendUnavailable(`backend unreachable: ${path}`);
  }
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    // Render returns these while the instance is (re)starting; our own 502 has an error body.
    const body = await response.json().catch(() => null);
    if (!body?.error) throw new BackendUnavailable(`backend starting (${response.status})`);
    throw new BackendError(response.status, body.error.code, body.error.message, body.error.details);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new BackendError(
      response.status,
      body?.error?.code ?? "http_error",
      body?.error?.message ?? `HTTP ${response.status}`,
      body?.error?.details ?? {},
    );
  }
  return (await response.json()) as T;
}

/** Liveness only: /healthz never touches the database, so it does not wake Neon. */
export async function backendHealthy(timeoutMs = HEALTH_TIMEOUT_MS): Promise<boolean> {
  try {
    const r = await fetch(`${baseUrl()}/healthz`, {
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    return r.ok;
  } catch {
    return false;
  }
}

export type FeedQuery = {
  source?: string[];
  priority?: number[];
  min_priority?: number;
  status?: string;
  agent_status?: string[];
  sort?: string;
  cursor?: string;
  limit?: number;
};

function query(q: FeedQuery): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(q)) {
    if (value === undefined || value === "") continue;
    for (const v of Array.isArray(value) ? value : [value]) params.append(key, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

const id = (value: string) => encodeURIComponent(value);

export const api = {
  messages: (q: FeedQuery) => call<MessagePage>(`/api/messages${query(q)}`),
  stats: () => call<MessageStats>("/api/messages/stats"),
  message: (messageId: string) => call<MessageDetail>(`/api/messages/${id(messageId)}`),
  saveDraft: (messageId: string, body: string) =>
    call<Draft>(`/api/messages/${id(messageId)}/draft`, { method: "PUT", body: { body } }),
  approve: (messageId: string, body: string, idempotencyKey: string) =>
    call<Reply>(`/api/messages/${id(messageId)}/approve`, {
      method: "POST",
      body: { body, idempotency_key: idempotencyKey },
    }),
  triage: (messageId: string) =>
    call<Schemas["JobQueued"]>(`/api/messages/${id(messageId)}/triage`, { method: "POST" }),
  redraft: (messageId: string, instructions: string | null) =>
    call<Schemas["JobQueued"]>(`/api/messages/${id(messageId)}/redraft`, {
      method: "POST",
      body: { instructions },
    }),
  dismiss: (messageId: string) =>
    call<MessageSummary>(`/api/messages/${id(messageId)}/dismiss`, { method: "POST" }),
  reopen: (messageId: string) =>
    call<MessageSummary>(`/api/messages/${id(messageId)}/reopen`, { method: "POST" }),
  connectors: () => call<Connector[]>("/api/connectors"),
  patchConnector: (key: string, body: Schemas["ConnectorPatch"]) =>
    call<Connector>(`/api/connectors/${id(key)}`, { method: "PATCH", body }),
  syncConnector: (key: string) =>
    call<Schemas["SyncScheduled"]>(`/api/connectors/${id(key)}/sync`, { method: "POST" }),
  scenarios: (key: string) =>
    call<Schemas["ScenarioList"]>(`/api/connectors/${id(key)}/scenarios`),
  sample: (key: string, scenario: string | null) =>
    call<Schemas["SampleOut"]>(`/api/connectors/${id(key)}/sample`, {
      method: "POST",
      body: { scenario },
    }),
};
