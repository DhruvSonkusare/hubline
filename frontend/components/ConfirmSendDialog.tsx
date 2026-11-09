"use client";

import { useEffect, useRef, useState } from "react";

/**
 * The one confirmation before anything leaves Hubline. It names the destination, shows the
 * exact text, and lists open questions not yet ticked (inside this dialog, not a separate
 * popup). The idempotency key is created when the dialog opens, so a double click sends once.
 */
export function ConfirmSendDialog({
  open,
  destination,
  body,
  uncheckedQuestions,
  sending,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  destination: string;
  body: string;
  uncheckedQuestions: string[];
  sending: boolean;
  onCancel: () => void;
  onConfirm: (idempotencyKey: string) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [key, setKey] = useState("");

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setKey(crypto.randomUUID());
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        if (!sending) onCancel();
      }}
      aria-labelledby="confirm-send-title"
      className="m-auto w-full max-w-lg rounded-xl p-0 shadow-2xl backdrop:bg-slate-900/40"
    >
      <div className="space-y-4 p-6">
        <h2 id="confirm-send-title" className="text-lg font-semibold">
          Send this reply?
        </h2>
        <p className="text-sm text-slate-600">
          Reply in <span className="font-medium text-slate-900">{destination}</span>
        </p>
        <pre className="max-h-48 overflow-auto rounded-lg bg-slate-50 p-3 font-sans text-sm whitespace-pre-wrap text-slate-800 ring-1 ring-slate-200">
          {body}
        </pre>
        {uncheckedQuestions.length > 0 && (
          <div className="rounded-lg bg-amber-50 p-3 text-sm ring-1 ring-amber-200" data-testid="unchecked-questions">
            <p className="font-semibold text-amber-900">
              {uncheckedQuestions.length === 1
                ? "1 open question is not ticked:"
                : `${uncheckedQuestions.length} open questions are not ticked:`}
            </p>
            <ul className="mt-1.5 list-disc space-y-1 pl-5 text-amber-950">
              {uncheckedQuestions.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-amber-800/80">
              They are not sent. Make sure the reply covers them, or send anyway.
            </p>
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={sending}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={sending || !key}
            onClick={() => onConfirm(key)}
          >
            {sending ? "Sending…" : uncheckedQuestions.length ? "Send anyway" : "Send"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
