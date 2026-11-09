/** Pure helpers shared by server and client components (no secrets, no I/O). */

export type Priority = 1 | 2 | 3 | 4 | 5;

/** Priority is shown by colour AND the "P" label, never colour alone. */
export const PRIORITY_STYLE: Record<Priority, { label: string; className: string; meaning: string }> =
  {
    5: { label: "P5", className: "bg-red-600 text-white", meaning: "Urgent: act within hours" },
    4: { label: "P4", className: "bg-orange-500 text-white", meaning: "Reply today" },
    3: { label: "P3", className: "bg-amber-300 text-amber-950", meaning: "Reply this week" },
    2: { label: "P2", className: "bg-slate-200 text-slate-800", meaning: "FYI" },
    1: { label: "P1", className: "bg-slate-100 text-slate-500", meaning: "Noise / automated" },
  };

export function priorityStyle(priority: number | null | undefined) {
  if (priority && priority >= 1 && priority <= 5) return PRIORITY_STYLE[priority as Priority];
  return { label: "P?", className: "bg-white text-slate-500 ring-1 ring-slate-300", meaning: "Not scored yet" };
}

export const AGENT_STATUS_LABEL: Record<string, string> = {
  pending: "Agent queued",
  running: "Agent working…",
  done: "Triaged",
  failed: "Agent failed",
  skipped: "Not triaged",
};

export function isAgentBusy(status: string | undefined): boolean {
  return status === "pending" || status === "running";
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

export function clockTime(iso: string, timeZone = "Asia/Kolkata"): string {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone,
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

/** Feed filters <-> URL search params (shareable, survive refresh). */
export type FeedFilters = {
  source: string[];
  priority: number[];
  status: string;
  agentStatus: string[];
  sort: "received" | "priority";
  cursor?: string;
};

type SearchParams = Record<string, string | string[] | undefined>;

const list = (v: string | string[] | undefined): string[] =>
  v === undefined ? [] : (Array.isArray(v) ? v : [v]).flatMap((x) => x.split(",")).filter(Boolean);

export function parseFilters(sp: SearchParams): FeedFilters {
  const status = typeof sp.status === "string" ? sp.status : "open";
  return {
    source: list(sp.source),
    priority: list(sp.priority)
      .map(Number)
      .filter((p) => Number.isInteger(p) && p >= 1 && p <= 5),
    status: ["open", "replied", "dismissed", "all"].includes(status) ? status : "open",
    agentStatus: list(sp.agent_status),
    sort: sp.sort === "received" ? "received" : "priority",
    cursor: typeof sp.cursor === "string" ? sp.cursor : undefined,
  };
}

export function filtersToSearch(f: Partial<FeedFilters>): string {
  const params = new URLSearchParams();
  if (f.source?.length) params.set("source", f.source.join(","));
  if (f.priority?.length) params.set("priority", f.priority.join(","));
  if (f.status && f.status !== "open") params.set("status", f.status);
  if (f.agentStatus?.length) params.set("agent_status", f.agentStatus.join(","));
  if (f.sort && f.sort !== "priority") params.set("sort", f.sort);
  if (f.cursor) params.set("cursor", f.cursor);
  const s = params.toString();
  return s ? `?${s}` : "";
}

export function sourceLabel(sourceDisplay: string, threadTitle: string | null | undefined): string {
  // Demo rows mimic other apps in their thread title ("LinkedIn · Messages").
  return threadTitle ? threadTitle : sourceDisplay;
}
