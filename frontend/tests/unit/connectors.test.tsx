import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const refresh = vi.fn();
const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, replace, push: vi.fn() }),
}));

const actions = vi.hoisted(() => ({
  connectWithToken: vi.fn(),
  startOAuth: vi.fn(),
  disconnectConnector: vi.fn(),
  setConnectorEnabled: vi.fn(async () => ({ ok: true, data: {} })),
  syncConnector: vi.fn(async () => ({ ok: true, data: {} })),
  saveConnectorSettings: vi.fn(async () => ({ ok: true, data: {} })),
}));
vi.mock("@/app/actions", () => actions);

import { ConnectorCard } from "@/components/ConnectorCard";
import {
  ConnectResultToast,
  connectErrorMessage,
} from "@/components/ConnectResultToast";
import { ToastProvider } from "@/components/Toast";
import type { Connector } from "@/lib/backend";

function connector(overrides: Partial<Connector> = {}): Connector {
  return {
    key: "slack",
    display_name: "Slack",
    auth_type: "oauth2",
    capabilities: ["poll", "send", "webhook"],
    can_send: true,
    is_demo: false,
    enabled: false,
    status: "disconnected",
    account_identity: null,
    missing_env: [],
    disabled_by_env: false,
    poll_interval_s: 300,
    last_poll_started_at: null,
    last_success_at: null,
    next_poll_at: null,
    consecutive_failures: 0,
    last_error: null,
    settings: {},
    settings_schema: null,
    connect_method: "token",
    token_fields: [
      {
        name: "token",
        label: "User OAuth Token",
        placeholder: "xoxp-…",
        help: "Slack app page → OAuth & Permissions → User OAuth Token",
      },
    ],
    ...overrides,
  };
}

const gmail = (overrides: Partial<Connector> = {}) =>
  connector({
    key: "gmail",
    display_name: "Gmail",
    capabilities: ["poll", "send"],
    connect_method: "oauth",
    token_fields: [],
    ...overrides,
  });

function renderCard(c: Connector) {
  return render(
    <ToastProvider>
      <ConnectorCard connector={c} />
    </ToastProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ConnectorCard: token connectors (Slack)", () => {
  it("submits the pasted token once and clears the field", async () => {
    actions.connectWithToken.mockResolvedValue({
      ok: true,
      data: { status: "connected", account_identity: "Hubline Demo / dhruv" },
    });
    renderCard(connector());
    const input = screen.getByLabelText("User OAuth Token") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: "xoxp-secret" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    });
    expect(actions.connectWithToken).toHaveBeenCalledWith("slack", {
      token: "xoxp-secret",
    });
    expect(input.value).toBe("");
    expect(
      await screen.findByText("Slack connected as Hubline Demo / dhruv."),
    ).toBeInTheDocument();
    expect(refresh).toHaveBeenCalled();
  });

  it("shows the backend's reason when the token is rejected", async () => {
    actions.connectWithToken.mockResolvedValue({
      ok: false,
      code: "invalid_token",
      message:
        "Paste the User OAuth Token (it starts with xoxp-), not the bot or app token",
    });
    renderCard(connector());
    fireEvent.change(screen.getByLabelText("User OAuth Token"), {
      target: { value: "xoxb-1" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    });
    expect(await screen.findByText(/starts with xoxp-/)).toBeInTheDocument();
    expect(
      (screen.getByLabelText("User OAuth Token") as HTMLInputElement).value,
    ).toBe("");
  });

  it("connected: shows the account, no token form, and a confirmed disconnect", async () => {
    actions.disconnectConnector.mockResolvedValue({
      ok: true,
      data: { status: "disconnected" },
    });
    renderCard(
      connector({
        status: "connected",
        account_identity: "Hubline Demo / dhruv",
      }),
    );
    expect(screen.getByText("Hubline Demo / dhruv")).toBeInTheDocument();
    expect(screen.getByText(/Connected as/)).toBeInTheDocument();
    expect(screen.queryByLabelText("User OAuth Token")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(actions.disconnectConnector).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Yes, disconnect" }));
    });
    expect(actions.disconnectConnector).toHaveBeenCalledWith("slack");
  });
});

describe("ConnectorCard: OAuth connectors (Gmail)", () => {
  it("sends the browser to the provider's consent page", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    actions.startOAuth.mockResolvedValue({
      ok: true,
      data: {
        authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?state=s",
      },
    });
    renderCard(gmail());
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Connect Gmail" }));
    });
    expect(assign).toHaveBeenCalledWith(
      "https://accounts.google.com/o/oauth2/v2/auth?state=s",
    );
    vi.unstubAllGlobals();
  });

  it("needs_reauth: explains it and offers Reconnect", () => {
    renderCard(
      gmail({ status: "needs_reauth", account_identity: "demo@gmail.com" }),
    );
    expect(
      screen.getByText(/Access expired or was revoked/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reconnect Gmail" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Last connected as/)).toBeInTheDocument();
    expect(
      screen.getByText("demo@gmail.com is preselected on the sign-in screen."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Disconnect" }),
    ).toBeInTheDocument();
  });

  it("connected: shows the connected Gmail address next to the status", () => {
    renderCard(
      gmail({
        status: "connected",
        enabled: true,
        account_identity: "demo@gmail.com",
      }),
    );
    expect(screen.getByText("demo@gmail.com")).toBeInTheDocument();
    expect(screen.getByText(/Connected as/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Connect Gmail|Reconnect Gmail/ }),
    ).not.toBeInTheDocument();
  });

  it("not configured: no connect button, lists the missing settings", () => {
    renderCard(
      gmail({ status: "not_configured", missing_env: ["GOOGLE_CLIENT_ID"] }),
    );
    expect(
      screen.queryByRole("button", { name: /Connect/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Missing settings: GOOGLE_CLIENT_ID"),
    ).toBeInTheDocument();
  });

  it("demo connector never shows connect or disconnect", () => {
    renderCard(
      connector({
        key: "demo",
        display_name: "Demo",
        connect_method: "none",
        status: "connected",
        token_fields: [],
      }),
    );
    expect(
      screen.queryByRole("button", { name: /Connect|Disconnect/ }),
    ).not.toBeInTheDocument();
  });
});

describe("ConnectResultToast", () => {
  it("announces success once and cleans the URL", async () => {
    render(
      <ToastProvider>
        <ConnectResultToast connected="gmail" />
      </ToastProvider>,
    );
    expect(
      await screen.findByText(
        "Gmail connected. Switch it on to start syncing.",
      ),
    ).toBeInTheDocument();
    expect(replace).toHaveBeenCalledWith("/connectors");
  });

  it("explains callback errors in plain words", async () => {
    render(
      <ToastProvider>
        <ConnectResultToast error="missing_scope" connector="gmail" />
      </ToastProvider>,
    );
    expect(
      await screen.findByText(/Gmail: Both permissions are needed/),
    ).toBeInTheDocument();
    expect(connectErrorMessage("weird")).toBe(
      "Connecting failed (weird). Try again.",
    );
  });
});
