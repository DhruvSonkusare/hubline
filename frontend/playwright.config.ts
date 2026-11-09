import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

// webServer commands run through cmd.exe on Windows, which may not see uv on PATH.
const uvCandidate = path.join(os.homedir(), ".local", "bin", process.platform === "win32" ? "uv.exe" : "uv");
const UV = process.env.UV_BIN ?? (existsSync(uvCandidate) ? `"${uvCandidate}"` : "uv");

/**
 * One smoke test (login -> new demo message -> approve) plus real 1440x900 screenshots of
 * every page, against the REAL backend (fake LLM, no background loops = the production work
 * model) and a production build of the dashboard. Run with:  npm run e2e
 *
 * Servers: backend :8010 (DB hubline_e2e, reset every run), dashboard :3010, and a second
 * dashboard :3011 pointed at a closed port to capture the "waking the backend" state.
 */
export const E2E_PASSWORD = "e2e-dashboard-password-0123456789";
const E2E_TOKEN = "e2e-backend-token-0123456789abcdef0123456789";
const E2E_DB = "postgresql://hubline:hubline@localhost:5433/hubline_e2e";

const frontendEnv = (backendUrl: string) => ({
  ...process.env,
  BACKEND_URL: backendUrl,
  BACKEND_API_TOKEN: E2E_TOKEN,
  DASHBOARD_PASSWORD: E2E_PASSWORD,
  AUTH_PROVIDERS: "password",
  AUTH_SECRET: "e2e-auth-secret-not-for-production-0123456789",
  AUTH_TRUST_HOST: "true",
});

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3010",
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      name: "backend",
      cwd: "../backend",
      command:
        `${UV} run alembic downgrade base && ${UV} run alembic upgrade head && ` +
        `${UV} run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010`,
      url: "http://127.0.0.1:8010/healthz",
      timeout: 180_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        DATABASE_URL: E2E_DB,
        BACKEND_API_TOKENS: E2E_TOKEN,
        APP_ENV: "test",
        LOG_FORMAT: "console",
        LOG_LEVEL: "WARNING",
        LLM_PROVIDER: "fake",
        ENABLE_SCHEDULER: "false",
        ENABLE_AGENT_WORKER: "false",
        WORK_ON_DEMAND: "true",
        DEMO_MODE: "false",
        AGENT_MAX_TOKENS_PER_DAY: "0",
      },
    },
    {
      name: "dashboard",
      command: "npx next start -p 3010",
      url: "http://localhost:3010/login",
      timeout: 120_000,
      reuseExistingServer: false,
      env: frontendEnv("http://127.0.0.1:8010"),
    },
    {
      name: "dashboard-backend-asleep",
      command: "npx next start -p 3011",
      url: "http://localhost:3011/login",
      timeout: 120_000,
      reuseExistingServer: false,
      env: frontendEnv("http://127.0.0.1:8019"), // nothing listens here
    },
  ],
});
