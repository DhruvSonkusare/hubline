# Hubline

One inbox for every alert. Hubline pulls messages from Gmail, Slack, Microsoft Teams, LinkedIn and X into a single feed. An AI agent ranks each message by priority, summarises it in one line and drafts a reply. You approve every reply before it is sent back through the app it came from. **The agent never sends anything on its own.**

**Runs at $0.** The stack uses only free tiers:

| Piece | Service |
|---|---|
| Backend | Render Free |
| Frontend | Vercel Hobby |
| Database | Neon Free |
| LLM | Groq free tier (Gemini free tier optional) |
| Cron | GitHub Actions on this public repo |

> **Status: Step 5 of 7 built, locally (Slack + Gmail live).** The backend and dashboard run on your PC. Real Slack DMs, group DMs and @mentions arrive within a second through a cloudflared tunnel. Gmail inbox mail arrives within about 60 s. Groq triages each message and drafts replies. Slack replies you approve post into the right DM or thread; the live Gmail reply is still to be confirmed (see [reports/step-5-report.md](reports/step-5-report.md)). Deploying (Step 4) waits for GitHub (on hold). See [docs/PLAN.md](docs/PLAN.md) and the [Step 3 screenshots](reports/step-3-screenshots/).

| Step | What it adds | Status |
|---|---|---|
| 1 | Schema, ingest + dedup, connector framework, demo connector, scheduler, base API | ✅ done |
| 2 | LangGraph agent worker: priority, summary, drafts | ✅ done |
| 3 | Full API + Next.js dashboard (local) | ✅ done |
| 4 | Deploy at $0: Vercel Hobby + Render Free + Neon Free (Singapore) + tick cron | 🧰 files prepared; deploy waits for GitHub |
| 5 | Slack + Gmail live (locally first: cloudflared tunnel + localhost OAuth) | ✅ built; Slack verified live, Gmail reply still to confirm |
| 6 | Teams, LinkedIn/X (read-only), hardening, demo script | ⏳ |
| 7 | Self-hosting (Deploy to Render button, guide, setup check), public read-only demo, publish-safe repo | 📝 planned (R14) |

## How it works

```
App event ─► webhook / 60s poll ─► connector.fetch_new() ─► normalise ─► Postgres (dedup on source+external_id)
                                                                              │
              dashboard ◄── API ◄─────────────── agent worker: score 1-5, summarise, draft
                  │ approve
                  └──► API ─► connector.send() ─► reply lands in the original app thread (Step 3+)
```

- **Connectors are plug-ins.** Each app is one file in [backend/app/connectors/](backend/app/connectors/) implementing `describe()`, `fetch_new(since)`, and optionally `send()`, `parse_webhook()` and `maintain()`. It registers itself with `@register`. The core never imports a specific connector.
- **The agent never sends.** It is a LangGraph graph with no tools, and it runs four steps:
  1. `prefilter`: rules that skip your own messages and give bulk no-reply mail priority 1 without an LLM call.
  2. `triage`: scores priority 1–5 and writes a one-line summary.
  3. `draft`: writes a reply only when the owner should personally answer (`needs_reply`). If a proper answer needs facts the agent does not have, it writes a holding reply and lists the missing facts as open questions.
  4. `validate_draft`: rejects placeholders and over-long drafts, with one retry that feeds the problem back.

  The worker loads context from the DB, runs the graph with no transaction open, then saves the priority, summary and a versioned draft.
- **The LLM provider is swappable, and every provider goes through LangChain's `init_chat_model`.**
  - `LLM_PROVIDER=fake` (the default) is a deterministic offline model, used for local dev and all tests.
  - `groq` is the production default: free tier, `openai/gpt-oss-120b`, strict JSON-schema output.
  - `google_genai` is Gemini's free tier (`gemini-3.5-flash-lite`).
  - `anthropic` is optional and paid (`uv sync --extra anthropic`); the deployed setup doesn't use it.
- **Free-tier limits are respected.**
  - A per-model pacer keeps calls under the requests and tokens per minute.
  - A provider `429` defers the job by `retry-after` and never fails it.
  - A rolling 24-hour token budget and an hourly job cap keep the agent under the daily token limit.
- **Background work runs in the API process.** The scheduler claims due connectors with `FOR UPDATE SKIP LOCKED` plus a lease, so even several instances never poll the same connector twice.
  - **Locally,** the loops run and sleep up to 600s when idle. API actions such as enabling a connector or "sync now" wake them immediately.
  - **On Render Free** the loops are off, because the instance sleeps after 15 idle minutes and Neon's free compute should sleep too. Work runs instead when:
    - a GitHub Actions cron calls `POST /internal/tick` every 30 minutes;
    - the app boots;
    - an API action needs it.

## Local quickstart

**Prerequisites:** Python 3.12, [uv](https://docs.astral.sh/uv/), Docker Desktop, Node.js 26.x (the frontend pins `26.x`, Vercel's default).

```bash
# 1. Database (Postgres 16 on localhost:5433; also creates the hubline_test DB)
docker compose up -d --wait db

# 2. Backend
cd backend
uv sync
cp .env.example .env
uv run python scripts/gen_keys.py         # paste BACKEND_API_TOKENS into .env
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --port 8000
```

`.env.example` sets `DEMO_MODE=true`, so the demo connector starts enabled the first time it is registered. With the default `LLM_PROVIDER=fake`, every new message is triaged and drafted offline within a second or two. In another terminal:

```bash
TOKEN=<your BACKEND_API_TOKENS value>
curl localhost:8000/healthz                                             # liveness (no DB, no auth)
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/connectors    # connector status
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/api/messages?limit=5"

# switch the demo connector off / on, or poll now
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
     -d '{"enabled": false}' localhost:8000/api/connectors/demo
curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/api/connectors/demo/sync

# generate one demo message now (works even with the drip off); the agent picks it up
curl -X POST -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
     -d '{"scenario": "prod-incident"}' localhost:8000/api/connectors/demo/sample

# highest priority first, with the agent's output
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/api/messages?sort=priority&limit=5"
uv run python -m scripts.show_feed     # same, plus the current draft text
```

### Dashboard (Next.js)

```bash
cd frontend
npm install
cp .env.example .env.local   # set DASHBOARD_PASSWORD (16+ chars), AUTH_SECRET, BACKEND_API_TOKEN
npm run dev                  # http://localhost:3000, with the backend running on :8000
```

- **Login:** a single password (`DASHBOARD_PASSWORD`, constant-time check, `httpOnly` session cookie). GitHub OAuth can be switched on later with env vars only (`AUTH_PROVIDERS=github`, plan R12).
- **Feed:**
  - filters for app, priority, status and sort
  - priority shown by colour and P-label
  - DEMO badges
  - auto-refresh every 15 s while visible (3 s while the agent works), pausing after 10 idle minutes, plus a manual Refresh
  - the **New demo message** split button, which takes a scenario
  - **Triage** on skipped or failed messages
- **Detail:**
  - the thread and the agent's summary and reason
  - an editable draft with versions, with **open questions beside it**
  - **Approve and send:** the confirmation names the destination, lists unticked open questions, and sends at most once
  - **Copy draft** for read-only apps, **Redraft** with instructions, and **Dismiss/Reopen**
  - a failed send shows its error and keeps the edited draft
- **Waking-up state:** if the backend is asleep (Render Free), every page shows "Waking the backend…" and continues by itself.
- **Secrets:** only the Next.js server talks to the backend (`lib/backend.ts` is server-only). The browser never sees `BACKEND_API_TOKEN`.

### Using a real (free) model

1. Get a free key and put it in `backend/.env`:
   - **Groq** (default): https://console.groq.com, `GROQ_API_KEY=...`
   - **Gemini**: https://aistudio.google.com, `GOOGLE_API_KEY=...`
2. Check it works with **one** request:
   ```bash
   uv run python -m scripts.llm_probe --provider groq
   uv run python -m scripts.llm_probe --provider google_genai
   ```
3. Check quality on 15 fixed samples. The run is token-capped and paced to stay inside the free limits:
   ```bash
   uv run python -m scripts.agent_eval --provider groq --max-tokens 40000
   ```
4. To let the local worker use it, set `LLM_PROVIDER=groq` (or `google_genai`, with `LLM_MODEL=gemini-3.5-flash-lite`).

> **Gemini free tier: demo accounts only.** Google's terms for unpaid services say submitted content is used to "provide, improve, and develop" Google's products, that "human reviewers may read, annotate, and process your API input and output", and "Do not submit sensitive, confidential, or personal information to the Unpaid Services". Use `google_genai` only with the dedicated demo Gmail and Slack accounts, never with a real inbox. Groq is the default for that reason.

Interactive API docs are at http://localhost:8000/docs (disabled in production).

### Live connectors: Slack and Gmail (local)

Both connect from the dashboard's **Connectors** page. Their tokens are stored encrypted with `CREDENTIALS_ENCRYPTION_KEY` (`uv run python -m scripts.gen_keys` prints one) and never returned or logged.

- **Slack:** [docs/setup/slack.md](docs/setup/slack.md), using the app manifest in [docs/setup/slack-app-manifest.yaml](docs/setup/slack-app-manifest.yaml).
  - It reads DMs, group DMs and @mentions of you, and replies **as you** with a user token. You paste the `xoxp-` token on the Connectors page.
  - Events reach your PC through a free cloudflared quick tunnel:
    ```bash
    uv run python -m scripts.dev_tunnel     # prints the Request URL ending in /webhooks/slack
    ```
    The URL changes every run, so paste it into the Slack app's **Event Subscriptions** each session.
  - A safety-net poll every 5 minutes makes at most one `conversations.history` call, to stay under Slack's non-Marketplace limit of about 1 per minute.
- **Gmail:** [docs/setup/gmail.md](docs/setup/gmail.md).
  - OAuth with PKCE to `http://localhost:8000/oauth/gmail/callback`, polling every 60 s with `history.list`.
  - Replies go to `Reply-To`, else `From`, in the same thread.
  - In Google's Testing mode the login lasts **7 days**. Click **Reconnect Gmail** before then.
- **Disconnect** revokes the token at the provider and deletes it locally. Message history stays.

## Tests and checks

```bash
cd backend
uv run pytest            # needs `docker compose up -d db` (uses the hubline_test database)
uv run pytest -m "not db"  # pure unit tests only, no database
uv run ruff check . && uv run ruff format --check . && uv run mypy

cd ../frontend
npm run lint && npm run typecheck && npm test && npm run build
npm run e2e    # Playwright: login -> new demo message -> approve, + 1440x900 screenshots
```

- Database tests use **real Postgres**, because SKIP LOCKED, ON CONFLICT and partial indexes are Postgres-specific.
- `TEST_DATABASE_URL` points tests at another database, for example a Neon `test` branch. The suite refuses to reset any database whose name doesn't contain `test`.
- No test calls a real external API or LLM. The agent is tested with a scripted fake model, and the LangChain adapter with stub chat models and the real Groq and Gemini exception classes.

To check another database end to end (for example the Neon dev branch), run `uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL`. It applies migrations, compares the schema with the models, boots the app against it and calls `/readyz`. It prints the host only, never the URL.

## API

All `/api/*` routes need `Authorization: Bearer <BACKEND_API_TOKENS entry>`. Only the Next.js server calls them; the browser never does. Errors always look like `{"error": {"code", "message", "details"}}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness and worker status. Doesn't touch the DB, so frequent probes let Neon sleep. |
| GET | `/readyz` | Readiness, including a DB check. For deploy health checks. |
| GET | `/api/messages` | Feed of inbound messages, with the agent's `priority`, `summary`, `agent_status` and `has_draft`. Filters: `source` and `priority` (both repeatable), `min_priority`, `agent_status`, and `status` (`open`/`replied`/`dismissed`/`all`). Sort: `sort=received` (default) or `priority`. Paging: `cursor`, `limit` ≤ 100. |
| GET | `/api/connectors` | Every registered connector with status, sync state and settings schema. |
| PATCH | `/api/connectors/{key}` | `{"enabled": bool, "settings": {...}}`. Settings are merged and validated. |
| POST | `/api/connectors/{key}/sync` | Poll this connector now (202). |
| GET | `/api/messages/stats` | Open counts per app and priority, and agent jobs in progress (sidebar). |
| GET | `/api/messages/{id}` | Detail: the thread (including your replies), the current draft with `open_questions`, `draft_versions`, replies, and `can_send`. |
| PUT | `/api/messages/{id}/draft` | Save your edit as a new draft version (no-op if unchanged). |
| POST | `/api/messages/{id}/approve` | `{body, idempotency_key}`. Sends through the message's own app, at most once. A failure returns 502 and keeps the draft. Read-only apps return 409. |
| POST | `/api/messages/{id}/triage` / `redraft` | Run the agent now at dashboard priority (`redraft` takes `{instructions}`). 409 if a job is already active. |
| POST | `/api/messages/{id}/dismiss` / `reopen` | Status changes. |
| GET | `/api/connectors/{key}/scenarios` | Scenario names for New demo message. |
| POST | `/api/connectors/{key}/sample` | Generate one message now. Only for connectors with the `sample` capability, i.e. demo. Optional body `{"scenario": "<name>"}`. The agent triages it within seconds. |
| POST | `/api/connectors/{key}/credentials` | `{"fields": {...}}` for token connectors (Slack: `{"token": "xoxp-…"}`). Checked live, stored encrypted, never returned. |
| POST | `/api/connectors/{key}/oauth/start` | Returns `{authorize_url}` for OAuth connectors (Gmail). Single-use state with PKCE, valid 10 minutes. |
| POST | `/api/connectors/{key}/disconnect` | Best-effort revoke at the provider, deletes the credentials, switches the connector off. History stays. |
| GET | `/oauth/{key}/callback` | Public. Consumes the state, exchanges the code and redirects to the Connectors page with `?connected=` or `?error=`. |
| POST | `/webhooks/{key}` | Public, verified by the connector (Slack: v0 HMAC signature, ±5 min). Bad signature → 401. |
| POST | `/internal/tick` | **Cron endpoint**, header `X-Tick-Secret: $TICK_SECRET`. Runs one scheduler pass, then drains agent jobs for at most `TICK_BUDGET_S` (60s). `?wait=true` returns `{polled, agent_jobs, elapsed_s, stopped}`; otherwise it answers `202` and runs in the background. Returns 404 unless `TICK_SECRET` is set. |

**Free cron:** [.github/workflows/tick.yml](.github/workflows/tick.yml) calls the tick every 30 minutes from 06:30 to 23:00 IST. It stays inert until the repository secrets `TICK_URL` and `TICK_SECRET` exist.

> **GitHub Actions schedule caveats:**
> - Scheduled runs **can be delayed**, especially at busy times, so a tick may land minutes late.
> - GitHub **disables scheduled workflows after 60 days without repository activity** (on public repos). If ticks stop, re-enable the workflow in the **Actions** tab or push any commit.
> - The app also ticks when it boots and runs work on demand, so a late or missed tick only delays polling. Nothing is lost.

## Configuration

Every setting is an environment variable. Locally they live in `backend/.env`; see [backend/.env.example](backend/.env.example) for the full list.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Any `postgres://` URL. Neon's `?sslmode=require` is handled. |
| `BACKEND_API_TOKENS` | — | Comma list, so tokens can be rotated. At least 32 characters each in production. |
| `ENABLE_SCHEDULER` / `ENABLE_AGENT_WORKER` | `true` | Background loops. **Set both to `false` on Render Free.** |
| `SCHEDULER_IDLE_MAX_S` / `WORKER_IDLE_MAX_S` | `600` | Longest idle sleep. This is above Neon's 5-minute suspend delay, so an idle app lets the DB scale to zero. |
| `WORK_ON_DEMAND` | `true` | With the loops off, API actions run one scheduler/agent pass in the background. |
| `TICK_SECRET` | unset | Enables `POST /internal/tick`. Use 32+ random characters. |
| `TICK_BUDGET_S` / `TICK_ON_STARTUP` | `60` / `false` | Longest agent drain per tick, and whether to tick once at boot (`true` on Render). |
| `DEMO_MODE` | `false` | `true` only pre-enables the demo connector on first registration. |
| `CONNECTORS_ENABLED` | unset | Optional allowlist, e.g. `demo,slack,gmail`. |
| `OWNER_TIMEZONE` | `Asia/Kolkata` | Used for times shown in prompts. |
| `LLM_PROVIDER` | `fake` | `fake` (offline), `groq` (production), `google_genai` (Gemini, demo data only), `anthropic` (optional extra, paid), or any `init_chat_model` provider. |
| `LLM_MODEL` / `LLM_DRAFT_MODEL` | `openai/gpt-oss-120b` / same | Triage model and draft model. For Gemini, use `gemini-3.5-flash-lite`. |
| `LLM_EFFORT` | `low` | Reasoning effort, where the model supports it (gpt-oss, Anthropic). |
| `GROQ_API_KEY` / `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` | unset | Key for the chosen provider. |
| `LLM_RPM` / `LLM_TPM` | `30` / `8000` | Client-side pacing per model; Groq free limits for gpt-oss-120b. `0` turns it off. |
| `AGENT_MAX_JOBS_PER_HOUR` | `30` | Burst shaping only. The rolling 24 h token budget and the per-minute pacer are the real limits. `0` pauses the agent. |
| `BACKFILL_AUTO_TRIAGE` | `10` | A connector's first sync stores all history but auto-triages only the newest N (at the lowest priority). The rest stay `skipped`, with Triage available on request. |
| `AGENT_MAX_TOKENS_PER_DAY` | `180000` | Rolling 24 h token budget: 90% of Groq's free 200K tokens/day. `0` turns it off. |
| `AGENT_CONCURRENCY` | `1` | Jobs processed in parallel; 1 fits the 8K tokens/min free limit. |
| `AGENT_RETRY_DELAYS_S` | `30,120,600` | Waits between retries. After 3 attempts the message shows `agent_status=failed`. |
| `PUBLIC_BASE_URL` / `FRONTEND_URL` | `http://localhost:8000` / `http://localhost:3000` | The backend's own URL (OAuth redirect URIs are built from it; keep it localhost locally, never the tunnel) and where OAuth callbacks send you back. |
| `CREDENTIALS_ENCRYPTION_KEY` | unset | Fernet key for connector tokens. Required by Slack and Gmail. Losing it means reconnecting. |
| `SLACK_SIGNING_SECRET` | unset | Slack app → Basic Information → Signing Secret. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | unset | Google Cloud Web OAuth client for Gmail. |
| `OAUTH_STATE_TTL_S` | `600` | How long a Connect link stays valid. |
| `AGENT_OWNER_NAME` / `AGENT_OWNER_CONTEXT` / `AGENT_VIP_SENDERS` | Dhruv Sonkusare / Python backend developer / empty | Who the agent works for. VIP senders get higher priority. |

## Deploy ($0)

The files are ready, but nothing is deployed yet. Render and Vercel deploy from GitHub, which is on hold. The step-by-step guide is [docs/setup/deploy.md](docs/setup/deploy.md).

- [`render.yaml`](render.yaml) is a Render Blueprint:
  - **Free** plan, **Singapore**, Docker, 1 instance
  - health check `/healthz`
  - production env with loops off and tick on startup
  - Groq as the model
  - secrets prompted at creation, never committed
- [`backend/Dockerfile`](backend/Dockerfile) builds a `python:3.12-slim` image from `uv.lock` without dev tools. It runs as a non-root user, runs `alembic upgrade head` on boot, and starts one uvicorn worker. [`backend/.dockerignore`](backend/.dockerignore) keeps `.env` out of the image.
- [`frontend/vercel.json`](frontend/vercel.json) pins Vercel functions to `sin1`.
- `tests/unit/test_deploy_config.py` fails if `render.yaml` stops being free, turns the loops on, or uses an env name that `Settings` doesn't know.

Try the image locally:

```bash
docker build -t hubline-api:local backend    # then see "Test the image locally" in deploy.md
```

## Repository layout

```
render.yaml Render Blueprint for the backend (Free, Singapore)
backend/    FastAPI app, connectors, workers, Alembic migrations, tests (uv project)
frontend/   Next.js dashboard (App Router, Tailwind, Auth.js; Playwright e2e)
docs/       PLAN.md (approved plan), setup guides (deploy, Slack, Gmail)
reports/    step-<N>-report.md after every build step
```

## Adding a connector

1. Create `backend/app/connectors/<key>.py` with a `@register` subclass of `Connector`. The file name must equal the key.
2. Return `NormalizedMessage`s from `fetch_new()`, and pass the cursor back as an opaque string.
3. Run `uv run pytest tests/connectors/test_contract.py`. The contract suite runs against every registered connector automatically.

No other file changes.
