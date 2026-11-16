"use client";

import { useEffect, useRef } from "react";

import { pollOnOpen } from "@/app/actions";

/**
 * Opening the dashboard polls every active connector right away (Gmail included). In
 * production the background loops are off and the cron ticks only every 30 minutes.
 * Mounted once in the app layout, so client-side navigation and the 15 s auto-refresh
 * don't repeat it; the backend also skips connectors polled in the last minute.
 */
export function PollOnOpen() {
  const sent = useRef(false);
  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    void pollOnOpen().catch(() => undefined); // best effort: the next tick polls anyway
  }, []);
  return null;
}
