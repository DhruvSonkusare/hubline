"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import {
  approveAndSend,
  dismissMessage,
  redraftMessage,
  reopenMessage,
  saveDraft,
} from "@/app/actions";
import { ConfirmSendDialog } from "@/components/ConfirmSendDialog";
import { OpenQuestions } from "@/components/OpenQuestions";
import { RedraftDialog } from "@/components/RedraftDialog";
import { useToast } from "@/components/Toast";
import type { Draft, Reply } from "@/lib/backend";

type Props = {
  messageId: string;
  status: string;
  canSend: boolean;
  /** Set when the app can send but this message must not be answered (e.g. no-reply sender). */
  sendBlockedReason?: string | null;
  source: string;
  sourceDisplay: string;
  threadTitle: string;
  agentBusy: boolean;
  needsReply: boolean | null;
  draft: Draft | null;
  lastReply: Reply | null;
};

export function DraftPanel(props: Props) {
  const { messageId, status, canSend, draft, lastReply, agentBusy } = props;
  const router = useRouter();
  const toast = useToast();
  const [pending, start] = useTransition();
  const [text, setText] = useState(draft?.body ?? "");
  const [handled, setHandled] = useState<Set<number>>(new Set());
  const [sendError, setSendError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [redraftOpen, setRedraftOpen] = useState(false);

  const dirty = draft !== null && text !== draft.body;
  const questions = draft?.open_questions ?? [];
  const unchecked = questions.filter((_, i) => !handled.has(i));
  const editable = status === "open";
  const failed = lastReply?.status === "failed" ? lastReply : null;
  const unknown = lastReply?.status === "unknown" ? lastReply : null;

  const save = () =>
    start(async () => {
      const r = await saveDraft(messageId, text);
      if (r.ok) {
        toast(`Saved as version ${r.data.version}.`, "success");
        router.refresh();
      } else toast(r.message, "error");
    });

  const send = (idempotencyKey: string) =>
    start(async () => {
      setSendError(null);
      const r = await approveAndSend(messageId, text, idempotencyKey);
      setConfirmOpen(false);
      if (r.ok) {
        toast(`Sent via ${props.sourceDisplay}.`, "success");
      } else {
        setSendError(r.message); // the edited text stays in the editor (and is saved server-side)
      }
      router.refresh();
    });

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied. Paste it in ${props.sourceDisplay} (read-only here).`, "success");
    } catch {
      toast("Copy failed: select the text and copy it manually.", "error");
    }
  };

  const redraft = (instructions: string | null) =>
    start(async () => {
      setRedraftOpen(false);
      const r = await redraftMessage(messageId, instructions);
      if (r.ok) {
        toast("The agent is rewriting the draft…", "success");
        router.refresh();
      } else toast(r.code === "job_active" ? "The agent is already working on it." : r.message, "error");
    });

  const setMessageStatus = (target: "dismissed" | "open") =>
    start(async () => {
      const r = target === "dismissed" ? await dismissMessage(messageId) : await reopenMessage(messageId);
      if (r.ok) router.refresh();
      else toast(r.message, "error");
    });

  return (
    <section className="card p-5" aria-label="Draft reply">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="label">Draft reply</h2>
        {draft && (
          <span className="text-xs text-slate-500" data-testid="draft-version">
            v{draft.version} · {draft.author === "agent" ? "agent" : "you"}
            {draft.model ? ` · ${draft.model}` : ""}
            {draft.instructions ? ` · “${draft.instructions}”` : ""}
          </span>
        )}
      </div>

      {status === "replied" && (
        <p className="mb-4 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          Replied{lastReply?.status === "sent" ? ` via ${props.sourceDisplay}` : ""}. The reply is
          shown in the thread.
        </p>
      )}
      {status === "dismissed" && (
        <p className="mb-4 rounded-lg bg-slate-100 px-3 py-2 text-sm text-slate-600">
          Dismissed. Reopen it to reply.
        </p>
      )}
      {(sendError || failed) && status === "open" && (
        <div role="alert" className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          <p className="font-semibold">Sending failed. Your edited draft is kept below.</p>
          <p className="mt-0.5">{sendError ?? failed?.error}</p>
        </div>
      )}
      {unknown && (
        <div role="alert" className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          The app stopped while sending this reply, so it may or may not have been delivered.
          Check {props.sourceDisplay} before doing anything else.
        </div>
      )}
      {!canSend && props.sendBlockedReason && (
        <p className="mb-4 rounded-lg bg-slate-100 px-3 py-2 text-sm text-slate-700">
          Sending is off for this message: {props.sendBlockedReason}.
        </p>
      )}
      {!canSend && !props.sendBlockedReason && (
        <p className="mb-4 rounded-lg bg-slate-100 px-3 py-2 text-sm text-slate-700">
          {props.sourceDisplay} is read-only in Hubline: copy the draft and paste it in{" "}
          {props.sourceDisplay}.
        </p>
      )}

      {draft === null ? (
        <div className="rounded-lg border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-500">
          {agentBusy
            ? "The agent is working on this message…"
            : props.needsReply === false
              ? "No reply needed (the agent's assessment)."
              : "No draft yet."}
          {!agentBusy && editable && (
            <div className="mt-3">
              <button type="button" className="btn-secondary" disabled={pending} onClick={() => redraft(null)}>
                Write a draft
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_15rem]">
          <div className="space-y-2">
            <textarea
              aria-label="Draft reply text"
              className="field min-h-56 w-full resize-y font-sans leading-relaxed"
              value={text}
              disabled={!editable || pending}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="flex items-center gap-2 text-xs text-slate-500">
              {editable && (
                <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={!dirty || pending} onClick={save}>
                  Save
                </button>
              )}
              {dirty && <span className="text-amber-700">Unsaved changes</span>}
              {agentBusy && <span className="text-accent">Agent rewriting…</span>}
            </div>
          </div>
          <OpenQuestions
            questions={questions}
            handled={handled}
            onToggle={(i) =>
              setHandled((prev) => {
                const next = new Set(prev);
                if (next.has(i)) next.delete(i);
                else next.add(i);
                return next;
              })
            }
          />
        </div>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
        {editable && draft !== null && canSend && (
          <button
            type="button"
            className="btn-primary"
            disabled={pending || !text.trim() || agentBusy}
            onClick={() => setConfirmOpen(true)}
          >
            Approve and send
          </button>
        )}
        {draft !== null && !canSend && (
          <button type="button" className="btn-primary" disabled={!text.trim()} onClick={copy}>
            Copy draft
          </button>
        )}
        {editable && draft !== null && (
          <button type="button" className="btn-secondary" disabled={pending || agentBusy} onClick={() => setRedraftOpen(true)}>
            Redraft…
          </button>
        )}
        {status === "open" && (
          <button type="button" className="btn-ghost" disabled={pending} onClick={() => setMessageStatus("dismissed")}>
            Dismiss
          </button>
        )}
        {status === "dismissed" && (
          <button type="button" className="btn-secondary" disabled={pending} onClick={() => setMessageStatus("open")}>
            Reopen
          </button>
        )}
      </div>

      <ConfirmSendDialog
        open={confirmOpen}
        destination={`${props.source} · ${props.threadTitle || "this conversation"}`}
        body={text}
        uncheckedQuestions={unchecked}
        sending={pending}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={send}
      />
      <RedraftDialog open={redraftOpen} onCancel={() => setRedraftOpen(false)} onSubmit={redraft} />
    </section>
  );
}
