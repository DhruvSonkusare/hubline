"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";

import { useToast } from "@/components/Toast";

// Error slugs the backend's /oauth/{key}/callback can send back (?error=...).
const MESSAGES: Record<string, string> = {
  access_denied: "You cancelled on the consent screen. Nothing was connected.",
  invalid_state:
    "That sign-in link expired or was already used. Click Connect again.",
  missing_scope:
    "Both permissions are needed. Click Connect again and tick read and send on Google's screen.",
  no_refresh_token: "Google sent no refresh token. Click Connect again.",
  token_exchange_failed: "Google refused the sign-in. Click Connect again.",
  not_configured: "This app is not configured on the backend yet.",
  upstream_error: "Couldn't reach the provider. Try again in a moment.",
};

export function connectErrorMessage(code: string): string {
  return MESSAGES[code] ?? `Connecting failed (${code}). Try again.`;
}

/** Shows the result of an OAuth round trip once, then cleans the URL. */
export function ConnectResultToast({
  connected,
  error,
  connector,
}: {
  connected?: string;
  error?: string;
  connector?: string;
}) {
  const toast = useToast();
  const router = useRouter();
  const shown = useRef(false);

  useEffect(() => {
    if (shown.current || (!connected && !error)) return;
    shown.current = true;
    if (connected)
      toast(
        `${title(connected)} connected. Switch it on to start syncing.`,
        "success",
      );
    else if (error)
      toast(
        `${connector ? `${title(connector)}: ` : ""}${connectErrorMessage(error)}`,
        "error",
      );
    router.replace("/connectors");
  }, [connected, error, connector, toast, router]);

  return null;
}

function title(key: string): string {
  return key.charAt(0).toUpperCase() + key.slice(1);
}
