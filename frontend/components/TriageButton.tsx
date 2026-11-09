"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";

import { triageMessage } from "@/app/actions";
import { useToast } from "@/components/Toast";

/** Runs the agent on a skipped (older backfill) or failed message, ahead of other work. */
export function TriageButton({
  messageId,
  label = "Triage",
  variant = "secondary",
}: {
  messageId: string;
  label?: string;
  variant?: "secondary" | "ghost";
}) {
  const router = useRouter();
  const toast = useToast();
  const [pending, start] = useTransition();
  return (
    <button
      type="button"
      className={`${variant === "ghost" ? "btn-ghost" : "btn-secondary"} px-2.5 py-1 text-xs`}
      disabled={pending}
      onClick={() =>
        start(async () => {
          const r = await triageMessage(messageId);
          if (r.ok) {
            toast("Queued for the agent.", "success");
            router.refresh();
          } else {
            toast(r.code === "job_active" ? "Already in progress." : r.message, "error");
          }
        })
      }
    >
      {pending ? "Queuing…" : label}
    </button>
  );
}
