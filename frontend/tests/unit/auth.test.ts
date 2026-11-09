// @vitest-environment node
import * as crypto from "node:crypto";

import { describe, expect, it, vi } from "vitest";

vi.mock("node:crypto", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:crypto")>();
  return { ...real, timingSafeEqual: vi.fn(real.timingSafeEqual) };
});

import { actorFor, enabledProviders, isAllowedGithubLogin } from "@/lib/auth-config";
import {
  AttemptLimiter,
  configuredPassword,
  MIN_PASSWORD_LENGTH,
  PasswordConfigError,
  verifyPassword,
} from "@/lib/password";

describe("verifyPassword (constant time)", () => {
  const secret = "correct-horse-battery-staple-42";

  it("accepts the right password and rejects wrong ones", () => {
    expect(verifyPassword(secret, secret)).toBe(true);
    expect(verifyPassword("wrong", secret)).toBe(false);
    expect(verifyPassword("", secret)).toBe(false);
    expect(verifyPassword(secret + "x", secret)).toBe(false);
  });

  it("always compares equal-length digests with timingSafeEqual, whatever the input length", () => {
    const spy = vi.mocked(crypto.timingSafeEqual);
    spy.mockClear();
    for (const input of ["", "a", secret, "x".repeat(10_000)]) verifyPassword(input, secret);
    expect(spy).toHaveBeenCalledTimes(4);
    for (const [a, b] of spy.mock.calls) {
      expect((a as Buffer).length).toBe(32);
      expect((b as Buffer).length).toBe(32);
    }
  });
});

describe("configuredPassword", () => {
  it(`refuses a missing or shorter than ${MIN_PASSWORD_LENGTH}-character password`, () => {
    expect(() => configuredPassword({})).toThrow(PasswordConfigError);
    expect(() => configuredPassword({ DASHBOARD_PASSWORD: "short" })).toThrow(/at least 16/);
    expect(configuredPassword({ DASHBOARD_PASSWORD: "x".repeat(16) })).toBe("x".repeat(16));
  });
});

describe("AttemptLimiter", () => {
  it("blocks after 5 failures in 15 minutes per key, and forgets after the window", () => {
    let now = 0;
    const limiter = new AttemptLimiter(5, 15 * 60_000, () => now);
    for (let i = 0; i < 5; i++) limiter.recordFailure("1.2.3.4");
    expect(limiter.isBlocked("1.2.3.4")).toBe(true);
    expect(limiter.isBlocked("5.6.7.8")).toBe(false);
    now += 15 * 60_000 + 1;
    expect(limiter.isBlocked("1.2.3.4")).toBe(false);
  });

  it("reset clears a key after a successful login", () => {
    const limiter = new AttemptLimiter(2);
    limiter.recordFailure("k");
    limiter.recordFailure("k");
    limiter.reset("k");
    expect(limiter.isBlocked("k")).toBe(false);
  });
});

describe("providers from env vars only (switch to GitHub without code changes)", () => {
  it("defaults to the password provider", () => {
    expect(enabledProviders({})).toEqual(["password"]);
  });

  it("enables GitHub when configured, alone or alongside the password", () => {
    const github = { AUTH_GITHUB_ID: "id", AUTH_GITHUB_SECRET: "s", ALLOWED_GITHUB_LOGINS: "dhruv" };
    expect(enabledProviders({ AUTH_PROVIDERS: "github", ...github })).toEqual(["github"]);
    expect(enabledProviders({ AUTH_PROVIDERS: "password, github", ...github })).toEqual([
      "password",
      "github",
    ]);
  });

  it("refuses GitHub without its env vars, and unknown providers", () => {
    expect(() => enabledProviders({ AUTH_PROVIDERS: "github" })).toThrow(/AUTH_GITHUB_ID/);
    expect(() => enabledProviders({ AUTH_PROVIDERS: "google" })).toThrow(/unknown/);
  });

  it("only allow-listed GitHub logins get in", () => {
    const env = { ALLOWED_GITHUB_LOGINS: "Dhruv, someone" };
    expect(isAllowedGithubLogin("dhruv", env)).toBe(true);
    expect(isAllowedGithubLogin("intruder", env)).toBe(false);
    expect(isAllowedGithubLogin(undefined, env)).toBe(false);
  });

  it("maps the session to the backend actor", () => {
    expect(actorFor("password")).toBe("password:owner");
    expect(actorFor("github", "dhruv")).toBe("github:dhruv");
  });
});
