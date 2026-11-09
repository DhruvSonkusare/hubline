import Link from "next/link";

import { AutoRefresh } from "@/components/AutoRefresh";
import { FeedToolbar } from "@/components/FeedToolbar";
import { MessageRow } from "@/components/MessageRow";
import { NewDemoMessageButton } from "@/components/NewDemoMessageButton";
import { WakingScreen } from "@/components/WakingScreen";
import { api, BackendUnavailable, type Connector, type MessagePage } from "@/lib/backend";
import { filtersToSearch, isAgentBusy, parseFilters } from "@/lib/format";

export const metadata = { title: "Inbox · Hubline" };

export default async function InboxPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const filters = parseFilters(await searchParams);
  let page: MessagePage;
  let connectors: Connector[];
  let scenarios: string[] = [];
  try {
    [page, connectors] = await Promise.all([
      api.messages({
        source: filters.source,
        priority: filters.priority,
        status: filters.status,
        agent_status: filters.agentStatus,
        sort: filters.sort,
        cursor: filters.cursor,
        limit: 50,
      }),
      api.connectors(),
    ]);
    const demo = connectors.find((c) => c.key === "demo");
    if (demo?.status === "connected") scenarios = (await api.scenarios("demo")).scenarios;
  } catch (error) {
    if (error instanceof BackendUnavailable) return <WakingScreen />;
    throw error;
  }

  const demoAvailable = connectors.some((c) => c.key === "demo" && c.status === "connected");
  const busy = page.items.some((m) => isAgentBusy(m.agent_status));
  const filtered =
    filters.source.length > 0 ||
    filters.priority.length > 0 ||
    filters.agentStatus.length > 0 ||
    filters.status !== "open";

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Inbox</h1>
          <p className="text-sm text-slate-500">
            Every alert in one place. The agent ranks and drafts; you approve every reply.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <AutoRefresh fast={busy} />
          {demoAvailable && <NewDemoMessageButton scenarios={scenarios} />}
        </div>
      </div>

      <FeedToolbar filters={filters} sources={connectors.map((c) => c.key)} />

      {page.items.length === 0 ? (
        <div className="card flex flex-col items-center gap-4 px-6 py-16 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-soft text-2xl">
            ✉
          </div>
          <div>
            <h2 className="text-lg font-semibold">
              {filtered ? "No messages match these filters" : "No messages yet"}
            </h2>
            <p className="mt-1 max-w-md text-sm text-slate-500">
              {filtered
                ? "Try clearing a filter."
                : "Connect an app on the Connectors page, or create a demo message to watch the agent triage it."}
            </p>
          </div>
          {filtered ? (
            <Link href="/inbox" className="btn-secondary">
              Clear filters
            </Link>
          ) : (
            demoAvailable && <NewDemoMessageButton scenarios={scenarios} />
          )}
        </div>
      ) : (
        <ul className="card divide-y divide-slate-100 overflow-hidden" data-testid="feed">
          {page.items.map((m) => (
            <MessageRow key={m.id} message={m} />
          ))}
        </ul>
      )}

      {(page.next_cursor || filters.cursor) && (
        <div className="flex justify-center gap-3">
          {filters.cursor && (
            <Link href={`/inbox${filtersToSearch({ ...filters, cursor: undefined })}`} className="btn-ghost">
              ← Newest
            </Link>
          )}
          {page.next_cursor && (
            <Link
              href={`/inbox${filtersToSearch({ ...filters, cursor: page.next_cursor })}`}
              className="btn-secondary"
            >
              Load more →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
