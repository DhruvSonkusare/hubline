import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { backendHealthy } from "@/lib/backend";

// Polled by the waking screen. Pings the backend's /healthz server-side (never touches the
// DB); the first ping is what starts a sleeping Render instance.
export async function GET() {
  if (!(await auth())) return NextResponse.json({ ok: false }, { status: 401 });
  return NextResponse.json({ ok: await backendHealthy(8_000) });
}
