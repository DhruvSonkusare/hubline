"""Smoke-test a deployed Hubline backend (plan §9 Step 4).

    uv run python -m scripts.smoke --base https://hubline-api.onrender.com
    uv run python -m scripts.smoke --base https://... --token-var SMOKE_API_TOKEN --approve-demo

Public checks (no secrets needed):
  1. /healthz is ok. The first call waits up to --wake-s for Render Free to wake up.
  2. /readyz reports db ok.
  3. /api/messages without a token -> 401, /internal/tick without the secret -> 401.
Authenticated checks (only if the token variable is set, in the environment or backend/.env):
  4. /api/connectors lists the demo connector; /api/messages answers.
  5. --approve-demo: create one demo message, wait for the agent's draft, approve it into the
     demo outbox and check the message is replied. Uses the live LLM once (free tier).

The token is never printed. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

BACKEND_DIR = Path(__file__).resolve().parents[1]


class SmokeError(Exception):
    pass


def _token(var: str) -> str | None:
    value = os.environ.get(var) or dotenv_values(BACKEND_DIR / ".env").get(var)
    return value.strip() if value else None


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{f'  ({detail})' if detail else ''}")
    if not ok:
        raise SmokeError(label)


def _wake(client: httpx.Client, wake_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + wake_s
    last = ""
    while time.monotonic() < deadline:
        try:
            r = client.get("/healthz", timeout=30)
            if r.status_code == 200:
                data: dict[str, Any] = r.json()
                return data
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(5)
    raise SmokeError(f"/healthz not ok after {wake_s:.0f}s ({last})")


def _approve_demo(client: httpx.Client, auth: dict[str, str], wait_s: float) -> None:
    r = client.post(
        "/api/connectors/demo/sample", json={"scenario": "client-question"}, headers=auth
    )
    _check("create a demo message", r.status_code == 201, f"HTTP {r.status_code}")
    mid = r.json()["message_id"]
    deadline = time.monotonic() + wait_s
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = client.get(f"/api/messages/{mid}", headers=auth).json()
        if detail.get("agent_status") in ("done", "failed") and (
            detail.get("draft") or detail.get("agent_status") == "failed"
        ):
            break
        time.sleep(3)
    _check(
        "the agent triaged it and drafted a reply",
        detail.get("agent_status") == "done" and bool(detail.get("draft")),
        f"agent_status={detail.get('agent_status')}, model={detail.get('agent_model')}",
    )
    body = detail["draft"]["body"]
    r = client.post(
        f"/api/messages/{mid}/approve",
        json={"body": body, "idempotency_key": str(uuid.uuid4())},
        headers=auth,
        timeout=60,
    )
    _check("approve and send (demo outbox)", r.status_code == 200, f"HTTP {r.status_code}")
    status = client.get(f"/api/messages/{mid}", headers=auth).json().get("status")
    _check("the message is replied", status == "replied", f"status={status}")


def run(base: str, token_var: str, approve_demo: bool, wake_s: float) -> None:
    with httpx.Client(base_url=base.rstrip("/"), timeout=30, follow_redirects=False) as client:
        print(f"Smoke test: {base}")
        health = _wake(client, wake_s)
        _check("/healthz", health.get("status") == "ok", f"version {health.get('version')}")
        ready = client.get("/readyz").json()
        _check("/readyz database", ready.get("db") == "ok", str(ready.get("status")))
        r = client.get("/api/messages")
        _check(
            "/api/messages without a token is refused",
            r.status_code == 401,
            f"HTTP {r.status_code}",
        )
        r = client.post("/internal/tick")
        _check(
            "/internal/tick without the secret is refused",
            r.status_code == 401,
            f"HTTP {r.status_code}",
        )

        token = _token(token_var)
        if not token:
            print(
                f"  skip authenticated checks: {token_var} is not set (environment or backend/.env)"
            )
            return
        auth = {"Authorization": f"Bearer {token}"}
        r = client.get("/api/connectors", headers=auth)
        _check("/api/connectors with the token", r.status_code == 200, f"HTTP {r.status_code}")
        keys = sorted(c["key"] for c in r.json())
        _check("the demo connector is registered", "demo" in keys, ", ".join(keys))
        r = client.get("/api/messages", headers=auth)
        _check("/api/messages with the token", r.status_code == 200, f"HTTP {r.status_code}")
        if approve_demo:
            _approve_demo(client, auth, wait_s=120)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", required=True, help="backend URL, e.g. https://hubline-api.onrender.com"
    )
    parser.add_argument(
        "--token-var", default="SMOKE_API_TOKEN", help="variable holding an API token"
    )
    parser.add_argument(
        "--approve-demo", action="store_true", help="also run one demo approve end to end"
    )
    parser.add_argument(
        "--wake-s", type=float, default=150.0, help="how long to wait for a cold start"
    )
    args = parser.parse_args()
    try:
        run(args.base, args.token_var, args.approve_demo, args.wake_s)
    except SmokeError as exc:
        print(f"SMOKE FAILED: {exc}")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
