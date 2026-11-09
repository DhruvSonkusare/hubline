import { AGENT_STATUS_LABEL, isAgentBusy, priorityStyle } from "@/lib/format";

/** Colour AND label (P5…P1), never colour alone; the meaning is in the tooltip. */
export function PriorityBadge({ priority }: { priority: number | null | undefined }) {
  const style = priorityStyle(priority);
  return (
    <span
      title={style.meaning}
      aria-label={`Priority ${style.label.slice(1)}: ${style.meaning}`}
      className={`inline-flex h-6 min-w-9 items-center justify-center rounded-md px-1.5 text-xs font-bold tabular-nums ${style.className}`}
    >
      {style.label}
    </span>
  );
}

export function AgentStatusBadge({ status }: { status: string }) {
  if (status === "done") return null;
  const busy = isAgentBusy(status);
  const tone =
    status === "failed"
      ? "bg-red-50 text-red-700 ring-red-200"
      : busy
        ? "bg-accent-soft text-accent ring-indigo-200"
        : "bg-slate-100 text-slate-600 ring-slate-200";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${tone}`}
    >
      {busy && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
      {AGENT_STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function DemoBadge() {
  return (
    <span
      title="Synthetic demo data, not a real message"
      className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-bold tracking-wide text-amber-800"
    >
      DEMO
    </span>
  );
}

export function StatusPill({ status }: { status: string }) {
  if (status === "open") return null;
  const tone =
    status === "replied" ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}>{status}</span>;
}
