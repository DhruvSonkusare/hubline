import Link from "next/link";
import { notFound } from "next/navigation";

import { AutoRefresh } from "@/components/AutoRefresh";
import { AgentStatusBadge, DemoBadge, PriorityBadge, StatusPill } from "@/components/Badges";
import { DraftPanel } from "@/components/DraftPanel";
import { TriageButton } from "@/components/TriageButton";
import { WakingScreen } from "@/components/WakingScreen";
import { api, BackendError, BackendUnavailable, type MessageDetail } from "@/lib/backend";
import { clockTime, isAgentBusy, priorityStyle, sourceLabel } from "@/lib/format";

export const metadata = { title: "Message · Hubline" };

export default async function MessagePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let m: MessageDetail;
  try {
    m = await api.message(id);
  } catch (error) {
    if (error instanceof BackendUnavailable) return <WakingScreen />;
    if (error instanceof BackendError && (error.status === 404 || error.status === 422)) notFound();
    throw error;
  }
  const source = sourceLabel(m.source_display, m.thread_title);
  const busy = isAgentBusy(m.agent_status);
  const lastReply = m.replies[0] ?? null;

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <div className="flex items-center justify-between">
        <Link href="/inbox" className="btn-ghost -ml-3">
          ← Inbox
        </Link>
        <AutoRefresh fast={busy} />
      </div>

      <header className="card px-6 py-5">
        <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
          {m.is_demo && <DemoBadge />}
          <span>{source}</span>
          <span className="text-slate-400">·</span>
          <span className="font-semibold text-slate-900">{m.sender.name ?? m.sender.id}</span>
          <span className="text-slate-400">&lt;{m.sender.id}&gt;</span>
          <span className="ml-auto text-slate-500">received {clockTime(m.received_at)} IST</span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <PriorityBadge priority={m.priority} />
          <h1 className="text-xl font-semibold">{m.subject ?? m.summary ?? "(no subject)"}</h1>
          <AgentStatusBadge status={m.agent_status} />
          <StatusPill status={m.status} />
        </div>
      </header>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <section className="card p-5" aria-label="Thread">
          <h2 className="label mb-3">Thread</h2>
          <ol className="space-y-4">
            {m.thread.map((t) => (
              <li
                key={t.id}
                className={`rounded-lg p-3 text-sm ${
                  t.direction === "outbound"
                    ? "ml-6 bg-accent-soft"
                    : t.id === m.id
                      ? "bg-slate-50 ring-1 ring-slate-200"
                      : "bg-slate-50"
                }`}
              >
                <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
                  <span className="font-semibold text-slate-700">
                    {t.direction === "outbound" ? "You" : (t.sender.name ?? t.sender.id)}
                  </span>
                  <time dateTime={t.received_at}>{clockTime(t.received_at)}</time>
                </div>
                <p className="whitespace-pre-wrap text-slate-800">{t.body}</p>
              </li>
            ))}
          </ol>
        </section>

        <div className="space-y-5">
          <section className="card p-5" aria-label="Agent">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="label">Agent</h2>
              {!busy && m.status === "open" && (
                <TriageButton
                  messageId={m.id}
                  variant="ghost"
                  label={
                    m.agent_status === "failed"
                      ? "Retry agent"
                      : m.agent_status === "skipped"
                        ? "Triage"
                        : "Re-triage"
                  }
                />
              )}
            </div>
            {m.summary ? (
              <dl className="space-y-2 text-sm">
                <div>
                  <dt className="sr-only">Summary</dt>
                  <dd className="text-[15px] font-medium text-slate-900">{m.summary}</dd>
                </div>
                <div className="flex gap-2 text-slate-600">
                  <dt className="font-medium">
                    Why {priorityStyle(m.priority).label}:
                  </dt>
                  <dd>{m.priority_reason}</dd>
                </div>
                <div className="text-xs text-slate-400">
                  {m.agent_model} · {m.prompt_version}
                  {m.category ? ` · ${m.category}` : ""}
                </div>
              </dl>
            ) : (
              <p className="text-sm text-slate-500">
                {busy
                  ? "The agent is reading this message…"
                  : m.agent_status === "skipped"
                    ? (m.agent_error ?? "Not triaged yet.")
                    : (m.agent_error ?? "Not triaged yet.")}
              </p>
            )}
            {m.agent_status === "failed" && m.agent_error && (
              <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{m.agent_error}</p>
            )}
          </section>

          <DraftPanel
            key={m.draft?.id ?? "no-draft"}
            messageId={m.id}
            status={m.status}
            canSend={m.can_send}
            sendBlockedReason={m.send_blocked_reason}
            source={source}
            sourceDisplay={m.source_display}
            threadTitle={m.subject ?? m.summary ?? ""}
            agentBusy={busy}
            needsReply={m.needs_reply ?? null}
            draft={m.draft ?? null}
            lastReply={lastReply}
          />
        </div>
      </div>
    </div>
  );
}
