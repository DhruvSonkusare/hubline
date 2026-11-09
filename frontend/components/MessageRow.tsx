import Link from "next/link";

import { AgentStatusBadge, DemoBadge, PriorityBadge, StatusPill } from "@/components/Badges";
import { TriageButton } from "@/components/TriageButton";
import type { MessageSummary } from "@/lib/backend";
import { relativeTime, sourceLabel } from "@/lib/format";

export function MessageRow({ message: m }: { message: MessageSummary }) {
  const canTriage = m.agent_status === "skipped" || m.agent_status === "failed";
  return (
    <li className="group relative flex items-start gap-4 px-5 py-4 hover:bg-slate-50">
      <div className="pt-0.5">
        <PriorityBadge priority={m.priority} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {m.is_demo && <DemoBadge />}
          <span className="font-semibold text-slate-900">{m.sender.name ?? m.sender.id}</span>
          <span className="text-slate-400">·</span>
          <span className="text-slate-500">{sourceLabel(m.source_display, m.thread_title)}</span>
          <AgentStatusBadge status={m.agent_status} />
          <StatusPill status={m.status} />
          {m.has_draft && m.status === "open" && (
            <span className="rounded-full bg-accent-soft px-2 py-0.5 text-xs font-medium text-accent">
              draft ready
            </span>
          )}
        </div>
        <Link
          href={`/inbox/${m.id}`}
          className="mt-1 block text-[15px] leading-snug text-slate-800 after:absolute after:inset-0 focus:outline-none"
        >
          {m.summary ?? m.subject ?? m.preview}
        </Link>
        {m.summary && m.subject && (
          <p className="mt-0.5 truncate text-sm text-slate-500">{m.subject}</p>
        )}
        {m.agent_status === "skipped" && m.agent_error && (
          <p className="mt-1 text-xs text-slate-500">{m.agent_error}</p>
        )}
      </div>
      <div className="relative z-10 flex shrink-0 flex-col items-end gap-2">
        <time className="text-xs text-slate-500" dateTime={m.received_at} title={m.received_at}>
          {relativeTime(m.received_at)}
        </time>
        {canTriage && (
          <TriageButton messageId={m.id} label={m.agent_status === "failed" ? "Retry agent" : "Triage"} />
        )}
      </div>
    </li>
  );
}
