/**
 * Which login providers are on, decided by env vars only (plan R12), so switching from the
 * password to GitHub OAuth later needs no code change:
 *   AUTH_PROVIDERS=password          (default)  DASHBOARD_PASSWORD
 *   AUTH_PROVIDERS=github                       AUTH_GITHUB_ID, AUTH_GITHUB_SECRET, ALLOWED_GITHUB_LOGINS
 *   AUTH_PROVIDERS=password,github              both (switch-over period)
 */
export type ProviderId = "password" | "github";

type Env = Record<string, string | undefined>;

export function enabledProviders(env: Env = process.env): ProviderId[] {
  const raw = (env.AUTH_PROVIDERS ?? "password")
    .split(",")
    .map((p) => p.trim().toLowerCase())
    .filter(Boolean);
  const unknown = raw.filter((p) => p !== "password" && p !== "github");
  if (unknown.length) {
    throw new Error(`AUTH_PROVIDERS has unknown provider(s): ${unknown.join(", ")}`);
  }
  const providers = [...new Set(raw)] as ProviderId[];
  if (!providers.length) throw new Error("AUTH_PROVIDERS must name at least one provider");
  if (providers.includes("github")) {
    const missing = ["AUTH_GITHUB_ID", "AUTH_GITHUB_SECRET", "ALLOWED_GITHUB_LOGINS"].filter(
      (k) => !env[k],
    );
    if (missing.length) {
      throw new Error(`AUTH_PROVIDERS includes github but ${missing.join(", ")} not set`);
    }
  }
  return providers;
}

export function isAllowedGithubLogin(login: unknown, env: Env = process.env): boolean {
  if (typeof login !== "string" || !login) return false;
  const allowed = (env.ALLOWED_GITHUB_LOGINS ?? "")
    .split(",")
    .map((l) => l.trim().toLowerCase())
    .filter(Boolean);
  return allowed.includes(login.toLowerCase());
}

/** The identity sent to the backend as X-Actor (audit trail on replies). */
export function actorFor(provider: string | undefined, githubLogin?: unknown): string {
  if (provider === "github" && typeof githubLogin === "string") return `github:${githubLogin}`;
  return "password:owner";
}
