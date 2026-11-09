import "server-only";

import { createHash, timingSafeEqual } from "node:crypto";

/** Minimum length for DASHBOARD_PASSWORD; shorter values refuse to start (plan R12). */
export const MIN_PASSWORD_LENGTH = 16;

export class PasswordConfigError extends Error {}

/** The configured dashboard password, validated. Throws if missing or too short. */
export function configuredPassword(env: Record<string, string | undefined> = process.env): string {
  const value = env.DASHBOARD_PASSWORD ?? "";
  if (value.length < MIN_PASSWORD_LENGTH) {
    throw new PasswordConfigError(
      `DASHBOARD_PASSWORD must be set and at least ${MIN_PASSWORD_LENGTH} characters ` +
        `(frontend/.env.local).`,
    );
  }
  return value;
}

/**
 * Constant-time password check. Both values are hashed first, so the buffers always have
 * the same length (32 bytes) and `timingSafeEqual` reveals neither content nor length.
 * There is deliberately no early return.
 */
export function verifyPassword(input: string, expected: string): boolean {
  const a = createHash("sha256").update(input, "utf8").digest();
  const b = createHash("sha256").update(expected, "utf8").digest();
  return timingSafeEqual(a, b);
}

/**
 * Failed-login limiter: `max` failures per `windowMs` per key (client IP). In memory, so on
 * serverless it is best effort; the long random password is the real protection.
 */
export class AttemptLimiter {
  private failures = new Map<string, number[]>();

  constructor(
    private readonly max = 5,
    private readonly windowMs = 15 * 60_000,
    private readonly now: () => number = Date.now,
  ) {}

  private recent(key: string): number[] {
    const cutoff = this.now() - this.windowMs;
    const kept = (this.failures.get(key) ?? []).filter((t) => t > cutoff);
    if (kept.length) this.failures.set(key, kept);
    else this.failures.delete(key);
    return kept;
  }

  isBlocked(key: string): boolean {
    return this.recent(key).length >= this.max;
  }

  recordFailure(key: string): void {
    this.failures.set(key, [...this.recent(key), this.now()]);
  }

  reset(key: string): void {
    this.failures.delete(key);
  }
}

export const FAILED_LOGIN_DELAY_MS = 1000;

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
