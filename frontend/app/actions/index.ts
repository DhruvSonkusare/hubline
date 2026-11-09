"use server";

// Server actions: the ONLY way the browser changes anything. Each one re-checks the session
// (never trusting the client) and talks to the backend with the server-only token.

import { AuthError } from "next-auth";

import { auth, signIn, signOut } from "@/auth";
import { api, BackendError, BackendUnavailable } from "@/lib/backend";

export type ActionResult<T = unknown> =
  | { ok: true; data: T }
  | { ok: false; code: string; message: string; details?: Record<string, unknown> };

async function guarded<T>(fn: () => Promise<T>): Promise<ActionResult<T>> {
  const session = await auth();
  if (!session) return { ok: false, code: "unauthorized", message: "Please log in again." };
  try {
    return { ok: true, data: await fn() };
  } catch (error) {
    if (error instanceof BackendUnavailable) {
      return {
        ok: false,
        code: "backend_unavailable",
        message: "The backend is waking up. Try again in a moment.",
      };
    }
    if (error instanceof BackendError) {
      return { ok: false, code: error.code, message: error.message, details: error.details };
    }
    throw error;
  }
}

// ---------------------------------------------------------------- session

export type LoginState = { error: string | null };

export async function loginAction(_prev: LoginState, formData: FormData): Promise<LoginState> {
  const password = String(formData.get("password") ?? "");
  const next = String(formData.get("next") ?? "");
  const redirectTo = next.startsWith("/") && !next.startsWith("//") ? next : "/inbox";
  try {
    await signIn("password", { password, redirectTo });
  } catch (error) {
    if (error instanceof AuthError) return { error: "Incorrect password." };
    throw error; // the success redirect is thrown, too
  }
  return { error: null };
}

export async function logoutAction(): Promise<void> {
  await signOut({ redirectTo: "/login" });
}

// ---------------------------------------------------------------- messages

export async function createSampleMessage(scenario: string | null) {
  return guarded(() => api.sample("demo", scenario));
}

export async function triageMessage(messageId: string) {
  return guarded(() => api.triage(messageId));
}

export async function redraftMessage(messageId: string, instructions: string | null) {
  return guarded(() => api.redraft(messageId, instructions?.trim() || null));
}

export async function saveDraft(messageId: string, body: string) {
  return guarded(() => api.saveDraft(messageId, body));
}

export async function approveAndSend(messageId: string, body: string, idempotencyKey: string) {
  return guarded(() => api.approve(messageId, body, idempotencyKey));
}

export async function dismissMessage(messageId: string) {
  return guarded(() => api.dismiss(messageId));
}

export async function reopenMessage(messageId: string) {
  return guarded(() => api.reopen(messageId));
}

// ---------------------------------------------------------------- connectors

export async function setConnectorEnabled(key: string, enabled: boolean) {
  return guarded(() => api.patchConnector(key, { enabled }));
}

export async function syncConnector(key: string) {
  return guarded(() => api.syncConnector(key));
}

export async function saveConnectorSettings(key: string, settings: Record<string, unknown>) {
  return guarded(() => api.patchConnector(key, { settings }));
}
