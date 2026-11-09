"use client";

/** The agent's checklist of facts YOU must supply. Shown beside the draft; never sent. */
export function OpenQuestions({
  questions,
  handled,
  onToggle,
}: {
  questions: string[];
  handled: Set<number>;
  onToggle: (index: number) => void;
}) {
  return (
    <aside className="rounded-lg bg-amber-50 p-3 ring-1 ring-amber-200" aria-label="Open questions">
      <h3 className="text-sm font-semibold text-amber-900">Before sending, answer:</h3>
      {questions.length === 0 ? (
        <p className="mt-2 text-sm text-amber-800/80">Nothing missing: the draft is complete.</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {questions.map((q, i) => (
            <li key={i}>
              <label className="flex cursor-pointer items-start gap-2 text-sm text-amber-950">
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 accent-[var(--color-accent)]"
                  checked={handled.has(i)}
                  onChange={() => onToggle(i)}
                />
                <span className={handled.has(i) ? "text-amber-800/60 line-through" : undefined}>{q}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-xs text-amber-800/70">Not sent. Tick each one once the reply covers it.</p>
    </aside>
  );
}
