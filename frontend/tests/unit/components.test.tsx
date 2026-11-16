import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh, push: vi.fn() }) }));
vi.mock("@/app/actions", () => ({
  approveAndSend: vi.fn(async () => ({ ok: false, code: "upstream_error", message: "sending failed: boom" })),
  saveDraft: vi.fn(async () => ({ ok: true, data: { version: 3 } })),
  redraftMessage: vi.fn(async () => ({ ok: true, data: { job_id: 1 } })),
  dismissMessage: vi.fn(async () => ({ ok: true, data: {} })),
  reopenMessage: vi.fn(async () => ({ ok: true, data: {} })),
  triageMessage: vi.fn(async () => ({ ok: true, data: { job_id: 1 } })),
}));

import { AutoRefresh, IDLE_STOP_MS, SLOW_MS } from "@/components/AutoRefresh";
import { AgentStatusBadge, PriorityBadge } from "@/components/Badges";
import { ConfirmSendDialog } from "@/components/ConfirmSendDialog";
import { DraftPanel } from "@/components/DraftPanel";
import { ToastProvider } from "@/components/Toast";
import { WakingScreen } from "@/components/WakingScreen";

const draft = {
  id: "d1",
  version: 1,
  body: "Hi Rahul, I'll get the retry policy to you shortly.",
  author: "agent" as const,
  instructions: null,
  open_questions: ["The webhook retry count and window", "Whether Friday works"],
  model: "fake:draft",
  created_at: "2026-11-10T10:00:00Z",
};

function panel(overrides: Partial<React.ComponentProps<typeof DraftPanel>> = {}) {
  return render(
    <ToastProvider>
      <DraftPanel
        messageId="m1"
        status="open"
        canSend
        source="Email · Inbox"
        sourceDisplay="Demo data"
        threadTitle="Webhook retry policy"
        agentBusy={false}
        needsReply
        draft={draft}
        lastReply={null}
        {...overrides}
      />
    </ToastProvider>,
  );
}

describe("badges", () => {
  it("priority shows the P-label with a meaning, not colour alone", () => {
    render(<PriorityBadge priority={5} />);
    const badge = screen.getByText("P5");
    expect(badge).toHaveAttribute("title", "Urgent: act within hours");
    expect(badge).toHaveAccessibleName(/Priority 5/);
  });

  it("agent status is hidden once triaged", () => {
    const { container } = render(<AgentStatusBadge status="done" />);
    expect(container).toBeEmptyDOMElement();
    render(<AgentStatusBadge status="running" />);
    expect(screen.getByText("Agent working…")).toBeInTheDocument();
  });
});

describe("DraftPanel", () => {
  it("offers Approve and send for apps that can send, and open questions beside the draft", () => {
    panel();
    expect(screen.getByRole("button", { name: "Approve and send" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy draft" })).not.toBeInTheDocument();
    expect(screen.getByText("Before sending, answer:")).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
  });

  it("offers Copy draft (no send) for read-only sources", () => {
    panel({ canSend: false, sourceDisplay: "LinkedIn" });
    expect(screen.getByRole("button", { name: "Copy draft" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and send" })).not.toBeInTheDocument();
    expect(screen.getByText(/LinkedIn is read-only in Hubline/)).toBeInTheDocument();
  });

  it("explains why sending is off for a no-reply sender", () => {
    panel({
      canSend: false,
      sourceDisplay: "Gmail",
      sendBlockedReason:
        "notification@slack.com is an automated no-reply sender; Hubline never replies to it",
    });
    expect(screen.queryByRole("button", { name: /Approve and send/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Sending is off for this message: notification@slack.com/)).toBeInTheDocument();
    expect(screen.queryByText(/read-only in Hubline/)).not.toBeInTheDocument();
  });

  it("puts unchecked open questions inside the send confirmation", () => {
    panel();
    fireEvent.click(screen.getAllByRole("checkbox")[0]); // first one handled
    fireEvent.click(screen.getByRole("button", { name: "Approve and send" }));
    const unchecked = screen.getByTestId("unchecked-questions");
    expect(unchecked).toHaveTextContent("1 open question is not ticked");
    expect(unchecked).toHaveTextContent("Whether Friday works");
    expect(unchecked).not.toHaveTextContent("The webhook retry count");
    expect(screen.getByRole("button", { name: "Send anyway" })).toBeInTheDocument();
  });

  it("shows a failed send in the panel and keeps the edited draft", async () => {
    panel();
    const box = screen.getByLabelText("Draft reply text");
    fireEvent.change(box, { target: { value: "My edited reply" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve and send" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send anyway" }));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("sending failed: boom");
    expect(screen.getByLabelText("Draft reply text")).toHaveValue("My edited reply");
  });

  it("shows the server-side failure too (after a reload)", () => {
    panel({
      lastReply: {
        id: "r1",
        status: "failed",
        body: "x",
        external_id: null,
        error: "upstream exploded",
        approved_by: "password:owner",
        created_at: "2026-11-10T10:00:00Z",
        sent_at: null,
      },
    });
    expect(screen.getByRole("alert")).toHaveTextContent("upstream exploded");
  });

  it("explains a missing draft when no reply is needed", () => {
    panel({ draft: null, needsReply: false });
    expect(screen.getByText(/No reply needed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Write a draft" })).toBeInTheDocument();
  });
});

describe("ConfirmSendDialog", () => {
  it("sends with one idempotency key per opening", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmSendDialog
        open
        destination="Demo outbox"
        body="Hello"
        uncheckedQuestions={[]}
        sending={false}
        onCancel={() => {}}
        onConfirm={onConfirm}
      />,
    );
    const send = screen.getByRole("button", { name: "Send" });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(onConfirm).toHaveBeenCalledTimes(2);
    expect(onConfirm.mock.calls[0][0]).toBe(onConfirm.mock.calls[1][0]); // same key: sends once
    expect(onConfirm.mock.calls[0][0]).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("AutoRefresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    refresh.mockClear();
  });
  afterEach(() => vi.useRealTimers());

  it("refreshes every 15 s and pauses after 10 idle minutes; manual refresh always works", () => {
    render(<AutoRefresh />);
    act(() => vi.advanceTimersByTime(SLOW_MS));
    expect(refresh).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(IDLE_STOP_MS + SLOW_MS));
    expect(screen.getByTestId("refresh-mode")).toHaveTextContent("paused");
    const calls = refresh.mock.calls.length;
    act(() => vi.advanceTimersByTime(SLOW_MS * 3));
    expect(refresh.mock.calls.length).toBe(calls); // no refreshes while idle
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    expect(refresh.mock.calls.length).toBe(calls + 1);
  });
});

describe("WakingScreen", () => {
  it("polls the backend status and refreshes once it is up", async () => {
    vi.useFakeTimers();
    refresh.mockClear();
    const replies = [{ ok: false }, { ok: true }];
    const fetchMock = vi.fn(async () => ({ json: async () => replies.shift() ?? { ok: true } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<WakingScreen />);
    expect(screen.getByText("Waking the backend…")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_500);
    });
    expect(refresh).toHaveBeenCalled(); // backend answered -> page re-renders with data
    const callsAfterUp = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(fetchMock.mock.calls.length).toBe(callsAfterUp); // polling stopped once it was up
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});
