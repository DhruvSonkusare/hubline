"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

export const POLL_MS = 3_000;
export const STILL_WAKING_S = 120;

/** Shown while the free backend (Render Free) spins up after sleeping. Recovers by itself. */
export function WakingScreen() {
  const router = useRouter();
  const routerRef = useRef(router);
  const [elapsed, setElapsed] = useState(0);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    routerRef.current = router;
  }, [router]);

  // One poller per attempt: it must not restart when the elapsed counter re-renders.
  useEffect(() => {
    const started = Date.now();
    const clock = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    let stopped = false;
    async function poll() {
      while (!stopped) {
        try {
          const r = await fetch("/api/backend-status", { cache: "no-store" });
          const body = (await r.json()) as { ok?: boolean };
          if (body.ok) {
            routerRef.current.refresh();
            return;
          }
        } catch {
          /* keep polling */
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearInterval(clock);
    };
  }, [attempt]);

  const minutes = Math.floor(elapsed / 60);
  const seconds = String(elapsed % 60).padStart(2, "0");
  const still = elapsed >= STILL_WAKING_S;

  return (
    <div className="flex min-h-[60vh] items-center justify-center p-8" role="status" aria-live="polite">
      <div className="card w-full max-w-md p-8 text-center">
        <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-4 border-accent-soft border-t-accent" />
        <h2 className="text-lg font-semibold">
          {still ? "Still waking the backend…" : "Waking the backend…"}
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          The free server sleeps when idle and takes about a minute to start. This page will
          continue on its own.
        </p>
        <p className="mt-4 font-mono text-sm text-slate-500" data-testid="waking-elapsed">
          {minutes}:{seconds}
        </p>
        {still && (
          <div className="mt-4 space-y-3">
            <p className="text-sm text-slate-600">
              This is taking longer than usual. Check the backend (the Render dashboard, or the
              local uvicorn process).
            </p>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setElapsed(0);
                setAttempt((a) => a + 1);
              }}
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
