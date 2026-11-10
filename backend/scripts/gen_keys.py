"""Print fresh random secrets for backend/.env, Render and Vercel. Nothing is written to disk.

Usage:  uv run python scripts/gen_keys.py
"""

import base64
import os
import secrets

print("# backend (Render Environment / backend/.env)")
print(f"BACKEND_API_TOKENS={secrets.token_urlsafe(32)}")
# Same format as cryptography's Fernet.generate_key(); used from Step 5.
print(f"CREDENTIALS_ENCRYPTION_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}")
print(f"# TICK_SECRET: Render generates it (render.yaml). Local/other: {secrets.token_urlsafe(32)}")
print()
print("# frontend (Vercel / frontend/.env.local). BACKEND_API_TOKEN = a BACKEND_API_TOKENS entry.")
print(f"AUTH_SECRET={secrets.token_urlsafe(32)}")
print(f"DASHBOARD_PASSWORD={secrets.token_urlsafe(18)}")
