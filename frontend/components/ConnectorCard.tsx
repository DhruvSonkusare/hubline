"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { saveConnectorSettings, setConnectorEnabled, syncConnector } from "@/app/actions";
import { DemoBadge } from "@/components/Badges";
import { useToast } from "@/components/Toast";
import type { Connector } from "@/lib/backend";
import { clockTime } from "@/lib/format";

const STATUS: Record<string, { label: string; dot: string }> = {
  connected: { label: "connected", dot: "bg-emerald-500" },
  disconnected: { label: "not connected", dot: "bg-slate-400" },
  not_configured: { label: "not configured", dot: "bg-slate-300" },
  needs_reauth: { label: "needs re-login", dot: "bg-amber-500" },
  error: { label: "error", dot: "bg-red-500" },
};

export function ConnectorCard({ connector: c }: { connector: Connector }) {
  const router = useRouter();
  const toast = useToast();
  const [pending, start] = useTransition();
  const [editing, setEditing] = useState(false);
  const settings = (c.settings ?? {}) as Record<string, unknown>;
  const [every, setEvery] = useState(String(settings.emit_every_minutes ?? 20));
  const [cap, setCap] = useState(String(settings.max_messages ?? 100));
  const status = STATUS[c.status] ?? { label: c.status, dot: "bg-slate-400" };
  const isDemo = c.key === "demo";

  const run = (fn: () => Promise<{ ok: boolean; message?: string }>, success: string) =>
    start(async () => {
      const r = await fn();
      if (r.ok) {
        toast(success, "success");
        router.refresh();
      } else toast(r.message ?? "Something went wrong", "error");
    });

  return (
    <article className="card flex flex-col p-5" aria-label={c.display_name}>
      <header className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold">{c.display_name}</h2>
            {c.is_demo && <DemoBadge />}
          </div>
          <p className="mt-1 flex items-center gap-2 text-sm text-slate-600">
            <span className={`h-2 w-2 rounded-full ${status.dot}`} aria-hidden />
            {status.label}
            {!c.can_send && <span className="text-slate-400">· read-only</span>}
          </p>
        </div>
        {c.status === "connected" && (
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <span className="text-slate-600">{c.enabled ? "Enabled" : "Disabled"}</span>
            <input
              type="checkbox"
              role="switch"
              aria-label={`Enable ${c.display_name}`}
              className="h-5 w-9 cursor-pointer appearance-none rounded-full bg-slate-300 transition before:block before:h-4 before:w-4 before:translate-x-0.5 before:rounded-full before:bg-white before:shadow before:transition checked:bg-accent checked:before:translate-x-4"
              checked={c.enabled}
              disabled={pending || c.disabled_by_env}
              onChange={(e) =>
                run(
                  () => setConnectorEnabled(c.key, e.target.checked),
                  e.target.checked ? `${c.display_name} enabled.` : `${c.display_name} disabled.`,
                )
              }
            />
          </label>
        )}
      </header>

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-slate-500">Last sync</dt>
        <dd>{c.last_success_at ? clockTime(c.last_success_at) : "never"}</dd>
        {c.enabled && c.next_poll_at && (
          <>
            <dt className="text-slate-500">Next sync</dt>
            <dd>{clockTime(c.next_poll_at)}</dd>
          </>
        )}
        {isDemo && (
          <>
            <dt className="text-slate-500">Cadence</dt>
            <dd>
              every {String(settings.emit_every_minutes ?? 20)} min · cap{" "}
              {String(settings.max_messages ?? 100)}
            </dd>
          </>
        )}
      </dl>
      {c.missing_env.length > 0 && (
        <p className="mt-3 rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600">
          Missing settings: {c.missing_env.join(", ")}
        </p>
      )}
      {c.last_error && (
        <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">{c.last_error}</p>
      )}

      {editing && isDemo && (
        <form
          className="mt-4 grid grid-cols-2 gap-3 rounded-lg bg-slate-50 p-3"
          onSubmit={(e) => {
            e.preventDefault();
            run(
              () =>
                saveConnectorSettings(c.key, {
                  emit_every_minutes: Number(every),
                  max_messages: Number(cap),
                }),
              "Settings saved.",
            );
            setEditing(false);
          }}
        >
          <label className="space-y-1 text-sm">
            <span className="text-slate-600">New message every (min)</span>
            <input className="field w-full" type="number" min={1} max={1440} value={every} onChange={(e) => setEvery(e.target.value)} />
          </label>
          <label className="space-y-1 text-sm">
            <span className="text-slate-600">Max messages</span>
            <input className="field w-full" type="number" min={0} max={1000} value={cap} onChange={(e) => setCap(e.target.value)} />
          </label>
          <div className="col-span-2 flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={pending}>
              Save
            </button>
          </div>
        </form>
      )}

      {c.status === "connected" && (
        <div className="mt-auto flex gap-2 pt-4">
          <button
            type="button"
            className="btn-secondary"
            disabled={pending || !c.enabled}
            title={c.enabled ? undefined : "Enable it first"}
            onClick={() => run(() => syncConnector(c.key), "Sync started.")}
          >
            Sync now
          </button>
          {isDemo && !editing && (
            <button type="button" className="btn-ghost" onClick={() => setEditing(true)}>
              Settings…
            </button>
          )}
        </div>
      )}
    </article>
  );
}
