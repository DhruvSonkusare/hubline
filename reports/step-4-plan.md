# Step 4 plan — deploy at $0 (Render Free + Vercel Hobby + Neon `main`)

**Status:** plan only, not built or deployed.
**Date:** 2026-11-16
**Repo:** https://github.com/DhruvSonkusare/hubline (public). `main` was pushed on 2026-11-16 at `c356663`, and CI passed on GitHub.

## Goal
A live, password-protected Hubline at a `*.vercel.app` URL. Its backend runs on Render Free and its data in Neon `main`. Everything stays at $0.

- **Phase A (required): demo data.** Log in, generate a demo message, see Groq triage it, approve it into the demo outbox.
- **Phase B (decided: part of Step 4): real Slack + Gmail in production**, with a new production Slack app named "Hubline".

Nothing here adds a paid plan, an add-on or a card. If any console asks for a card or an upgrade, stop and tell me.

## How production runs (already decided: R6, R9, R10)
| Piece | Where | Notes |
|---|---|---|
| Frontend | Vercel Hobby, functions in `sin1` | `frontend/vercel.json`; Node 26.x (supported by Vercel) |
| Backend | Render Free web service `hubline-api`, Singapore, Docker, 1 instance | `render.yaml`. Sleeps after 15 min idle; about 1 min to wake. |
| Database | Neon Free, project in AWS Singapore, branch **`main`**, direct (non-pooled) host, autoscaling max 0.25 CU | `alembic upgrade head` runs on every boot |
| Background work | **No loops in production.** GitHub Actions `tick.yml` calls `POST /internal/tick` every 30 min (06:30–23:00 IST), plus a tick on boot and on dashboard actions | Neon sleeps between ticks |
| LLM | Groq free tier, `openai/gpt-oss-120b` | 180K tokens/day budget, per-minute pacing |

**Gmail timing in production (decided):** up to 30 min on the cron alone, which is accepted, because:
- **opening the dashboard** polls Gmail (and every active connector) at once;
- **Sync now** polls at once.

Both are Part 1 item 6. Slack DMs arrive in seconds through the webhook once Render is awake. If Render is asleep, Slack retries (immediately, +1 min, +5 min).

---

## Part 1 — what I build before you click anything
> **Done (2026-11-16).** All 7 items are built and pushed.
> - Backend: 376 passed, 6 skipped; ruff, format and mypy clean.
> - Frontend: lint, typecheck, 37 Vitest tests, build and 2 e2e tests pass.
> - `scripts.smoke` passed against a local production-like backend (loops off, tick secret set): public checks, the token checks, and a demo message end to end. A wrong token fails it.
> - The new test for "Sync now during a running tick" fails on the old runner code and passes on the new.
> - **Your clicks start at Part 2, step 1 (Neon).**

Small changes, all tested, committed and pushed as "Step 4 deploy prep". Render and Vercel then deploy the right files.

1. **`backend/scripts/smoke.py`** (in the plan's Step 4 file list, not written yet). `uv run python -m scripts.smoke --base https://<render-url>` checks:
   - `/healthz` and `/readyz`
   - that `/api/messages` without a token returns 401, and the same for `/internal/tick` without the secret
   - the connector list with the token, read from an env var and never printed
   - optionally, one demo approve end to end
2. **`render.yaml`:**
   - Replace the stale `SLACK_CLIENT_ID/SECRET` comment, since Slack uses token paste, with the 4 real Phase B names.
   - They are **not** added as Blueprint keys: a dummy value would make Hubline treat the connectors as configured. You add them in Phase B via **Environment → Add Environment Variable** instead.
   - `tests/unit/test_deploy_config.py` keeps checking: Free plan, loops off, and only env names `Settings` knows.
3. **`docs/setup/deploy.md`:**
   - Remove the "GitHub on hold" banners and change "revision 0005" to `0006`.
   - Write the Phase B section to match what Step 5 actually built: Slack token paste, a separate production Slack app, the Gmail redirect URI.
4. **`.github/workflows/ci.yml`:** remove the stale "Not run yet: GitHub is on hold" comment.
5. **`README.md`:** add a "Live demo" line once the URL exists. That's the only change after the deploy.
6. **Poll on open + Sync now (decision 3):**
   - **Opening the dashboard** calls the new `POST /api/connectors/poll-now`. It marks every active connector due, unless it was polled in the last `DASHBOARD_POLL_MIN_AGE_S` (60 s), and runs one pass in the background.
   - **Sync now** already polls at once in production. One gap to fix: if a cron tick is already running, the kick returned "busy" and the connector waited for the next tick. The running tick must poll again before it ends.
   - Both are user actions, never periodic, so Neon still sleeps.
7. **Local default model (decision 4):** `backend/.env` → `LLM_PROVIDER=fake`. Switch to `groq` only while testing.

---

## Part 2 — what you click and paste, in order

Keep a password manager or a private note open. You'll generate secrets in step 2 and paste each one into exactly the places listed. **Never paste a secret into chat, an issue, a commit or `NEXT_PUBLIC_*`.**

### 1. Neon: production branch (5 min)
1. <https://console.neon.tech> → open your **Hubline** project (AWS Asia Pacific, Singapore).
2. **Branches** → **`main`**. It's the default branch; production uses it, and `dev` stays for checks.
3. On `main`'s compute: **Edit** → **Autoscaling max = 0.25 CU** → **Save**. Keep scale-to-zero **on** (5 min).
4. **Dashboard → Connect**:
   - Branch `main`, database `neondb`, role as shown.
   - **Connection pooling: OFF.** The host must **not** contain `-pooler`.
   - Copy the whole string. It starts `postgresql://`, the host starts `ep-…`, and it ends with `?sslmode=require`.
5. Check it from your PC, so the first Render boot can't fail on the URL:
   1. Add a temporary line to `backend/.env`: `NEON_MAIN_DATABASE_URL=<the string>`.
   2. Run:
      ```powershell
      cd D:\project\Hubline\backend
      uv run python -m scripts.verify_db --var NEON_MAIN_DATABASE_URL
      ```
      Expected: `migrations: at revision 0006; schema diff vs models: none` and `/readyz: HTTP 200`.
   3. **Delete that line from `backend/.env`** afterwards.

### 2. Generate fresh production secrets (2 min)
```powershell
cd D:\project\Hubline\backend
uv run python scripts/gen_keys.py
```
It prints new random values in your terminal only. Nothing is written to disk. Save these four in your password manager as "Hubline production":

| Value | Goes to | Note |
|---|---|---|
| `BACKEND_API_TOKENS` | Render `BACKEND_API_TOKENS` **and** Vercel `BACKEND_API_TOKEN` | the same value in both |
| `AUTH_SECRET` | Vercel | |
| `DASHBOARD_PASSWORD` | Vercel | **This is the NEW production password.** It must differ from your local one; don't reuse `frontend/.env.local`. |
| `CREDENTIALS_ENCRYPTION_KEY` | Render (Phase B) | Separate from the local key. If you lose it, you reconnect the apps. |

**Groq:** at <https://console.groq.com/keys> → **Create API Key**, name it `hubline-prod`, and copy it. A separate key can be revoked on its own. Local and production still share your organisation's free-tier limits (open question 4).

### 3. Render: backend (10 min, mostly waiting for the build)
1. <https://dashboard.render.com> → **Sign up / Log in with GitHub**. No card is needed for Free.
2. **New + → Blueprint**.
3. **Connect GitHub**:
   - Install the Render GitHub app.
   - Choose **Only select repositories → `hubline`** → **Install**.
4. Pick **`DhruvSonkusare/hubline`**, branch **`main`**. Blueprint name: `hubline`. Render reads `render.yaml` and lists one service, **`hubline-api` (Free, Singapore, Docker)**.
5. Fill the prompted values:

   | Key | Paste |
   |---|---|
   | `DATABASE_URL` | the Neon `main` **direct** string from step 1.4 |
   | `BACKEND_API_TOKENS` | the token from step 2 |
   | `GROQ_API_KEY` | the `hubline-prod` Groq key |
   | `PUBLIC_BASE_URL` | `https://hubline-api.onrender.com` |
   | `FRONTEND_URL` | `https://example.invalid` (a placeholder; fixed in step 5) |

   Render asks for these 5 only. The Slack and Gmail variables come later, in Phase B.

6. **Deploy Blueprint.** Wait for **Live** (about 5–10 min for the first Docker build).
7. On the service page:
   - **Check the URL at the top.** If it isn't exactly `hubline-api.onrender.com` (Render adds a suffix when the name is taken), note it. Every `hubline-api.onrender.com` below then means *your* URL, and you update `PUBLIC_BASE_URL` in step 5.
   - **Settings → Instance Type** must say **Free**.
   - **Environment →** reveal and copy **`TICK_SECRET`** (Render generated it).
8. Quick check (the first call may take about a minute while it wakes):
   ```powershell
   curl.exe -s https://hubline-api.onrender.com/healthz
   curl.exe -s https://hubline-api.onrender.com/readyz
   ```
   Expected: `"status":"ok"`, and `"db":"ok"` on `/readyz`.

### 4. Vercel: dashboard (5 min)
1. <https://vercel.com> → **Sign up / Log in with GitHub** → plan **Hobby**.
2. **Add New… → Project → Import Git Repository**:
   - Install the Vercel GitHub app.
   - Choose **Only select repositories → `hubline`**.
   - Then **Import**.
3. **Configure Project:**
   - **Project Name:** `hubline`.
   - **Root Directory → Edit → `frontend`**.
   - Framework: **Next.js** (auto). Leave the build and output settings at their defaults.
4. **Environment Variables**. Add each one; Production is enough:

   | Key | Value |
   |---|---|
   | `AUTH_PROVIDERS` | `password` |
   | `DASHBOARD_PASSWORD` | the **new** production password (step 2) |
   | `AUTH_SECRET` | from step 2 |
   | `BACKEND_URL` | `https://hubline-api.onrender.com` (your Render URL, no trailing slash) |
   | `BACKEND_API_TOKEN` | the same token as Render's `BACKEND_API_TOKENS` |

   Don't add `AUTH_TRUST_HOST` (Vercel is detected), and never prefix any of these with `NEXT_PUBLIC_`.
5. **Deploy.** When it's done, copy the production domain, e.g. `https://hubline.vercel.app` (or `hubline-xxxx.vercel.app` if the name is taken).
6. **Settings → Deployment Protection:** leave **Vercel Authentication** on for preview deployments (the Hobby default).

### 5. Wire the two URLs together (2 min)
1. Render → `hubline-api` → **Environment** → **Edit**:
   - `FRONTEND_URL` = your Vercel production URL from step 4.5.
   - `PUBLIC_BASE_URL` = your exact Render URL, if it differs.
2. **Save, rebuild and deploy.** It's live again in about 2 minutes.

### 6. Tick cron on GitHub (3 min)
1. <https://github.com/DhruvSonkusare/hubline> → **Settings → Secrets and variables → Actions → New repository secret**. Add two:
   - `TICK_URL` = `https://hubline-api.onrender.com/internal/tick?wait=true`
   - `TICK_SECRET` = the value copied from Render in step 3.7
2. **Actions → Tick backend → Run workflow → Run workflow.** The log should end with `{"polled": …, "agent_jobs": …, "stopped": …}`. The first run may take about 90 s while Render wakes.
3. If Actions shows "Workflows aren't being run on this repository", click **I understand… enable them**.

### 7. Verify Phase A (5 min)
1. Open the Vercel URL. It should redirect to **/login**.
2. Check that the **old local password is rejected**, then log in with the **new** one.
3. **Connectors → Demo data → switch on**, then **Inbox → New demo message**. If Render was asleep you'll see "Waking the backend…" first.
4. Within about a minute: a **Groq** priority (the agent line says `openai/gpt-oss-120b`, not `fake`), a summary and a draft.
5. **Approve and send** → confirm. The message moves to **Replied**, and the reply goes into the demo outbox.
6. Tell me it's done. I'll run `scripts.smoke` against the Render URL and the unauthenticated checks (expecting 401), and check Render and Neon usage.

### Phase B — real Slack and Gmail in production (about 20 min, after Phase A)
**B1. Render:** **Environment → Add Environment Variable**, add these 4, then **Save, rebuild and deploy**:
- `CREDENTIALS_ENCRYPTION_KEY` (from step 2)
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (the same Web client as local)
- `SLACK_SIGNING_SECRET` (see B3)

**B2. Google Cloud** → <https://console.cloud.google.com> → project **hubline** → **APIs & Services → Clients** → your Web client:
- **Authorized redirect URIs → Add URI:** `https://hubline-api.onrender.com/oauth/gmail/callback`. **Keep** the localhost one.
- **Save.** It can take a few minutes to apply.

**B3. Slack: a separate production app** (recommended; open question 2):
1. <https://api.slack.com/apps> → **Create New App → From a manifest** → workspace **Hubline Demo**.
2. Paste `docs/setup/slack-app-manifest.yaml`, changing `name: Hubline Dev` to `name: Hubline` → **Create**.
3. **Basic Information → Signing Secret → Show → copy** it into Render's `SLACK_SIGNING_SECRET` (B1).
4. **Install App → Install to Hubline Demo → Allow**, then copy the **User OAuth Token** (`xoxp-…`).
5. **Event Subscriptions → Enable Events: On**:
   - **Request URL:** `https://hubline-api.onrender.com/webhooks/slack`. Wake Render first by opening `/healthz`; Slack has to see **Verified ✓**.
   - **Subscribe to events on behalf of users:** add `message.im`, `message.mpim`, `message.channels` and `message.groups` → **Save Changes** (reinstall if asked, then copy the token again).

**B4. Connect them on the production dashboard:**
- **Connectors → Slack:** paste the `xoxp-` token → **Connect** → switch on.
- **Connectors → Gmail → Connect Gmail:**
  - Pick **`dhruv.sonkusare.dev@gmail.com`**. There's no preselect on a first connect, so check the account picker carefully.
  - Tick both boxes, then switch Gmail on.
  - The card must say **Connected as dhruv.sonkusare.dev@gmail.com**.

**B5. Live test:**
- From your second Slack account, DM yourself. It should appear within seconds if Render is awake, or after Slack's retry if it was asleep. Approve → the reply lands in the DM.
- Email the demo inbox from your personal address. It appears on the next tick, within 30 min; click **Sync now** on the Gmail card to fetch it at once. Approve → the reply lands in the same conversation.
- Then tell me. I'll check the production DB through the API: the `sent` rows, the stored ids, and no duplicates.

**Local vs production after Phase B:** both point at the same demo accounts, but they keep separate databases, separate encrypted tokens and separate Slack apps. Locally, "Hubline Dev" plus the tunnel keeps working for development. Gmail allows both logins; each lasts 7 days in Testing mode, so reconnect each before it expires.

---

## Done when (Phase A, from PLAN §9 Step 4)
- The Vercel URL redirects to login. The new password works, and the old local one doesn't.
- A demo message is triaged by Groq in production, and approve lands it in Replied.
- `scripts/smoke.py --base <render-url>` passes. `/api/messages` and `/internal/tick` without credentials return **401**.
- A redeploy mid-job loses nothing: the reaper re-queues it. I trigger this once with **Manual Deploy** while a job runs.
- `tick.yml` runs green on schedule.
- After a week, Neon shows about 1.5 CU-h/day (well under 100/month), and Render shows Free with about 350 of 750 instance-hours/month.
- `reports/step-4-report.md` is written. Then I stop.

**Phase B adds:** a production Slack DM and a Gmail email, both answered into the same thread, each with no duplicate rows.

## What can go wrong (and the fix)
| Symptom | Likely cause | Fix |
|---|---|---|
| Render deploy fails at `alembic upgrade head` | wrong `DATABASE_URL` (pooled host, or missing `?sslmode=require`) | Step 1.5's `verify_db` catches this before Render |
| `/readyz` says the DB is down | Neon `main` compute suspended **and** the URL wrong | re-copy the direct string; the compute wakes by itself |
| Vercel login loops back to /login | `AUTH_SECRET` missing, or set only for Preview | add it for **Production**, then **Redeploy** |
| Dashboard "Waking the backend…" never ends | `BACKEND_URL` has a trailing slash or the wrong host, or the token differs | fix it in Vercel → Redeploy |
| Slack "Your URL didn't respond" | Render asleep during verification | open `/healthz`, wait for `ok`, then **Retry** |
| Gmail `redirect_uri_mismatch` | the redirect isn't added or isn't the exact Render URL | B2, then wait a few minutes |
| A console asks for a card or plan upgrade | — | **stop**; the plan never needs one |

## Decisions (2026-11-16)
1. **Phase B is part of Step 4:** real Slack + Gmail in production.
2. **Production Slack:** a new app named **"Hubline"** with a fixed Request URL. "Hubline Dev" stays local with the tunnel.
3. **Gmail up to 30 min on the cron is OK,** provided opening the dashboard and Sync now both poll Gmail immediately (Part 1 item 6).
4. **Local `LLM_PROVIDER=fake` by default;** `groq` only while testing.
5. **The local backup branch** `backup/pre-history-rewrite` is deleted. The global git `user.email` is the GitHub noreply address.
