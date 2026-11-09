import Link from "next/link";

import { logoutAction } from "@/app/actions";
import { ToastProvider } from "@/components/Toast";
import { WakingScreen } from "@/components/WakingScreen";
import { api, backendHealthy, type MessageStats } from "@/lib/backend";

/** App shell: sidebar + the backend gate (waking-up state while Render Free spins up). */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const healthy = await backendHealthy();
  let stats: MessageStats | null = null;
  if (healthy) {
    try {
      stats = await api.stats();
    } catch {
      stats = null; // the page itself will show the waking screen if the backend is gone
    }
  }
  return (
    <ToastProvider>
      <div className="flex min-h-screen">
        <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-slate-200 bg-white px-4 py-5">
          <Link href="/inbox" className="mb-8 flex items-center gap-2.5 px-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent font-bold text-white">
              H
            </span>
            <span className="text-lg font-semibold">Hubline</span>
          </Link>
          <nav className="flex flex-col gap-1 text-sm">
            <NavLink href="/inbox" label="Inbox" count={stats?.total_open} />
            {stats &&
              Object.entries(stats.open_by_source).map(([source, count]) => (
                <NavLink
                  key={source}
                  href={`/inbox?source=${encodeURIComponent(source)}`}
                  label={source === "demo" ? "Demo data" : source}
                  count={count}
                  indent
                />
              ))}
            <NavLink href="/connectors" label="Connectors" />
          </nav>
          <div className="mt-auto space-y-3 border-t border-slate-200 pt-4 text-sm">
            {stats && stats.agent_active > 0 && (
              <p className="flex items-center gap-2 px-2 text-accent">
                <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
                Agent: {stats.agent_active} working
              </p>
            )}
            <form action={logoutAction}>
              <button type="submit" className="btn-ghost w-full justify-start">
                Log out
              </button>
            </form>
          </div>
        </aside>
        <main className="min-w-0 flex-1 px-8 py-6">{healthy ? children : <WakingScreen />}</main>
      </div>
    </ToastProvider>
  );
}

function NavLink({
  href,
  label,
  count,
  indent = false,
}: {
  href: string;
  label: string;
  count?: number;
  indent?: boolean;
}) {
  return (
    <Link
      href={href}
      className={`flex items-center justify-between rounded-lg px-2 py-1.5 text-slate-700 hover:bg-slate-100 ${indent ? "pl-5 text-slate-600" : "font-medium"}`}
    >
      <span>{label}</span>
      {count !== undefined && (
        <span className="rounded-full bg-slate-100 px-2 text-xs font-semibold text-slate-600 tabular-nums">
          {count}
        </span>
      )}
    </Link>
  );
}
