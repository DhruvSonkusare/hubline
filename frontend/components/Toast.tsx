"use client";

import { createContext, useCallback, useContext, useState } from "react";

type Tone = "info" | "success" | "error";
type ToastItem = { id: number; message: string; tone: Tone };

const ToastContext = createContext<(message: string, tone?: Tone) => void>(() => {});

const TONE: Record<Tone, string> = {
  info: "bg-slate-900 text-white",
  success: "bg-emerald-700 text-white",
  error: "bg-red-700 text-white",
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((message: string, tone: Tone = "info") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, message, tone }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 5000);
  }, []);
  return (
    <ToastContext value={push}>
      {children}
      <div
        className="pointer-events-none fixed right-5 bottom-5 z-50 flex flex-col gap-2"
        aria-live="polite"
      >
        {items.map((t) => (
          <div key={t.id} role="status" data-testid="toast" className={`rounded-lg px-4 py-2.5 text-sm shadow-lg ${TONE[t.tone]}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
