"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";

import { createSampleMessage } from "@/app/actions";
import { useToast } from "@/components/Toast";

/** Split button: a random demo message, or pick a scenario. Triggers the agent live. */
export function NewDemoMessageButton({ scenarios }: { scenarios: string[] }) {
  const router = useRouter();
  const toast = useToast();
  const [pending, start] = useTransition();
  const [open, setOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!menu.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [open]);

  const create = (scenario: string | null) =>
    start(async () => {
      setOpen(false);
      const r = await createSampleMessage(scenario);
      if (r.ok) {
        toast("Demo message created. The agent is working on it…", "success");
        router.refresh();
      } else {
        toast(r.message, "error");
      }
    });

  return (
    <div className="relative inline-flex" ref={menu}>
      <button
        type="button"
        className="btn-primary rounded-r-none"
        disabled={pending}
        onClick={() => create(null)}
      >
        {pending ? "Creating…" : "+ New demo message"}
      </button>
      <button
        type="button"
        aria-label="Choose a demo scenario"
        aria-haspopup="menu"
        aria-expanded={open}
        className="btn-primary rounded-l-none border-l border-indigo-400 px-2"
        disabled={pending || scenarios.length === 0}
        onClick={() => setOpen((o) => !o)}
      >
        ▾
      </button>
      {open && (
        <div
          role="menu"
          className="card absolute top-full right-0 z-20 mt-1 max-h-80 w-64 overflow-auto py-1"
        >
          <p className="label px-3 py-1.5">Scenario</p>
          {scenarios.map((s) => (
            <button
              key={s}
              type="button"
              role="menuitem"
              className="block w-full px-3 py-1.5 text-left text-sm hover:bg-accent-soft"
              onClick={() => create(s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
