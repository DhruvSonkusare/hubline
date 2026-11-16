"""Expose the local backend to Slack through a free cloudflared quick tunnel (no account).

Usage (backend already running on :8000):
    uv run python -m scripts.dev_tunnel            # --port 8000

It starts `cloudflared tunnel --url http://localhost:<port>`, waits for the random
https://<words>.trycloudflare.com address, checks /healthz through it, and prints the exact
Slack Request URL to paste into Event Subscriptions. The URL CHANGES EVERY RUN, so paste it
into the Slack app each session. Press Ctrl+C to close the tunnel.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_WINDOWS_PATHS = (  # where winget installs it
    Path(r"C:\Program Files (x86)\cloudflared"),
    Path(r"C:\Program Files\cloudflared"),
)


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for folder in _WINDOWS_PATHS:
        exe = folder / "cloudflared.exe"
        if exe.is_file():
            return str(exe)
    return None


def wait_healthy(url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=5.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    local = f"http://localhost:{args.port}"

    if not wait_healthy(f"{local}/healthz", 3):
        print(f"The backend is not answering on {local}/healthz. Start it first:")
        print("  uv run uvicorn app.main:create_app --factory --port 8000")
        return 1
    exe = find_cloudflared()
    if exe is None:
        print("cloudflared not found. Install it (free):")
        print("  winget install --id Cloudflare.cloudflared -e")
        return 1

    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", local],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    found: list[str] = []
    ready = threading.Event()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not found:
                m = _URL.search(line)
                if m:
                    found.append(m.group(0))
                    ready.set()
            if "ERR" in line and "failed to" in line.lower():
                print("cloudflared:", line.strip())
        ready.set()

    threading.Thread(target=pump, daemon=True).start()
    try:
        if not ready.wait(60) or not found:
            print("cloudflared did not report a tunnel URL within 60 s.")
            print("If ~/.cloudflared/config.yaml exists, rename it; behind strict networks try")
            print("  cloudflared tunnel --protocol http2 --url", local)
            return 1
        public = found[0]
        print(f"Tunnel: {public}  ->  {local}")
        print("Checking the tunnel (can take 10-30 s)...", flush=True)
        if not wait_healthy(f"{public}/healthz", 90):
            print(
                "Warning: the tunnel URL is not reachable yet; Slack's check may fail. Retry soon."
            )
        print()
        print("Paste this into Slack app -> Event Subscriptions -> Request URL:")
        print()
        print(f"    {public}/webhooks/slack")
        print()
        print("It changes every time this script starts. Keep this window open; Ctrl+C stops it.")
        sys.stdout.flush()
        proc.wait()
        return proc.returncode or 0
    except KeyboardInterrupt:
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
