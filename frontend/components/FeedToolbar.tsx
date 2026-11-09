"use client";

import { useRouter } from "next/navigation";

import { type FeedFilters, filtersToSearch, PRIORITY_STYLE, type Priority } from "@/lib/format";

/** Filters live in the URL, so they are shareable and survive a refresh. */
export function FeedToolbar({ filters, sources }: { filters: FeedFilters; sources: string[] }) {
  const router = useRouter();
  const go = (patch: Partial<FeedFilters>) =>
    router.push(`/inbox${filtersToSearch({ ...filters, ...patch, cursor: undefined })}`);

  const togglePriority = (p: number) =>
    go({
      priority: filters.priority.includes(p)
        ? filters.priority.filter((x) => x !== p)
        : [...filters.priority, p].sort((a, b) => b - a),
    });

  return (
    <div className="card flex flex-wrap items-center gap-x-5 gap-y-3 px-4 py-3">
      <label className="flex items-center gap-2 text-sm">
        <span className="label">App</span>
        <select
          className="field py-1.5"
          value={filters.source[0] ?? ""}
          onChange={(e) => go({ source: e.target.value ? [e.target.value] : [] })}
        >
          <option value="">All apps</option>
          {sources.map((s) => (
            <option key={s} value={s}>
              {s === "demo" ? "Demo data" : s}
            </option>
          ))}
        </select>
      </label>

      <div className="flex items-center gap-2 text-sm" role="group" aria-label="Priority filter">
        <span className="label">Priority</span>
        {([5, 4, 3, 2, 1] as Priority[]).map((p) => {
          const active = filters.priority.includes(p);
          const style = PRIORITY_STYLE[p];
          return (
            <button
              key={p}
              type="button"
              aria-pressed={active}
              title={style.meaning}
              onClick={() => togglePriority(p)}
              className={`h-7 min-w-9 rounded-md px-1.5 text-xs font-bold transition ${
                active
                  ? `${style.className} ring-2 ring-accent ring-offset-1`
                  : "bg-white text-slate-600 ring-1 ring-slate-300 hover:bg-slate-50"
              }`}
            >
              {style.label}
            </button>
          );
        })}
      </div>

      <label className="flex items-center gap-2 text-sm">
        <span className="label">Status</span>
        <select className="field py-1.5" value={filters.status} onChange={(e) => go({ status: e.target.value })}>
          <option value="open">Open</option>
          <option value="replied">Replied</option>
          <option value="dismissed">Dismissed</option>
          <option value="all">All</option>
        </select>
      </label>

      <label className="flex items-center gap-2 text-sm">
        <span className="label">Sort</span>
        <select
          className="field py-1.5"
          value={filters.sort}
          onChange={(e) => go({ sort: e.target.value as FeedFilters["sort"] })}
        >
          <option value="priority">Priority</option>
          <option value="received">Newest</option>
        </select>
      </label>
    </div>
  );
}
