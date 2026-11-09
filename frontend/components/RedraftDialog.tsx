"use client";

import { useEffect, useRef, useState } from "react";

const SUGGESTIONS = ["shorter", "more formal", "friendlier", "decline politely"];

export function RedraftDialog({
  open,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  onCancel: () => void;
  onSubmit: (instructions: string | null) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [instructions, setInstructions] = useState("");

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
      aria-labelledby="redraft-title"
      className="m-auto w-full max-w-md rounded-xl p-0 shadow-2xl backdrop:bg-slate-900/40"
    >
      <form
        className="space-y-4 p-6"
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(instructions.trim() || null);
          setInstructions("");
        }}
      >
        <h2 id="redraft-title" className="text-lg font-semibold">
          Redraft
        </h2>
        <label className="block space-y-1.5">
          <span className="text-sm text-slate-600">How should the agent change it? (optional)</span>
          <input
            className="field w-full"
            value={instructions}
            maxLength={500}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="e.g. shorter, more formal"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          {SUGGESTIONS.map((s) => (
            <button key={s} type="button" className="btn-ghost px-2 py-1 text-xs ring-1 ring-slate-200" onClick={() => setInstructions(s)}>
              {s}
            </button>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="btn-primary">
            Redraft
          </button>
        </div>
      </form>
    </dialog>
  );
}
