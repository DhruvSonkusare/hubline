import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const actions = vi.hoisted(() => ({
  pollOnOpen: vi.fn(async () => ({ ok: true, data: { polling: ["gmail"] } })),
}));
vi.mock("@/app/actions", () => actions);

import { PollOnOpen } from "@/components/PollOnOpen";

describe("PollOnOpen", () => {
  it("asks the backend to poll once when the dashboard opens, not on re-render", () => {
    const { rerender } = render(<PollOnOpen />);
    rerender(<PollOnOpen />);
    rerender(<PollOnOpen />);
    expect(actions.pollOnOpen).toHaveBeenCalledTimes(1);
  });
});
