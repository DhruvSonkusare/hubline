"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

export const SLOW_MS = 15_000;
export const FAST_MS = 3_000;
export const FAST_FOR_MS = 2 * 60_000;
export const IDLE_STOP_MS = 10 * 60_000;

/**
 * Refreshes server data every 15 s while the tab is visible, every 3 s for up to 2 min while
 * the agent is working (`fast`), and stops after 10 min without user input, so a forgotten tab
 * doesn't keep the free database awake. A manual Refresh button is always available.
 */
export function AutoRefresh({ fast = false }: { fast?: boolean }) {
  const router = useRouter();
  const lastActivity = useRef(0);
  const fastSince = useRef<number | null>(null);
  const [paused, setPaused] = useState(false);

  const refresh = useCallback(() => router.refresh(), [router]);

  useEffect(() => {
    lastActivity.current = Date.now();
  }, []);

  useEffect(() => {
    fastSince.current = fast ? (fastSince.current ?? Date.now()) : null;
  }, [fast]);

  useEffect(() => {
    const touch = () => {
      lastActivity.current = Date.now();
      setPaused(false);
    };
    const events = ["pointerdown", "keydown", "scroll", "pointermove"] as const;
    events.forEach((e) => window.addEventListener(e, touch, { passive: true }));
    return () => events.forEach((e) => window.removeEventListener(e, touch));
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      const since = fastSince.current;
      const inFast = since !== null && Date.now() - since < FAST_FOR_MS;
      timer = setTimeout(
        () => {
          if (Date.now() - lastActivity.current > IDLE_STOP_MS) {
            setPaused(true);
          } else if (document.visibilityState === "visible") {
            refresh();
          }
          schedule();
        },
        inFast ? FAST_MS : SLOW_MS,
      );
    };
    schedule();
    return () => clearTimeout(timer);
  }, [refresh]);

  return (
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <span aria-live="polite" data-testid="refresh-mode">
        {paused ? "Auto-refresh paused (idle)" : fast ? "Updating every 3 s" : "Auto-refresh 15 s"}
      </span>
      <button
        type="button"
        className="btn-secondary px-2.5 py-1.5"
        onClick={() => {
          lastActivity.current = Date.now();
          setPaused(false);
          refresh();
        }}
      >
        ⟳ Refresh
      </button>
    </div>
  );
}
