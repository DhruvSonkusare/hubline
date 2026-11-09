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

> **Status: Step 2 of 6 complete (agent worker).** The backend runs locally. It has the connector framework, a demo connector, the polling scheduler, ingest with dedup, a LangGraph agent that scores, summarises and drafts every new message, and the messages and connectors API. The dashboard, deployment and real connectors arrive in Steps 3–6. See [docs/PLAN.md](docs/PLAN.md).

| Step | What it adds | Status |
|---|---|---|
| 1 | Schema, ingest + dedup, connector framework, demo connector, scheduler, base API | ✅ done |
| 2 | LangGraph agent worker: priority, summary, drafts | ✅ done |
| 3 | Full API + Next.js dashboard (local) | ⏳ |
| 4 | Deploy at $0: Vercel Hobby + Render Free + Neon Free (Singapore) + tick cron | ⏳ |
| 5 | Slack + Gmail live | ⏳ |
| 6 | Teams, LinkedIn/X (read-only), hardening, demo script | ⏳ |

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

**Prerequisites:** Python 3.12, [uv](https://docs.astral.sh/uv/), Docker Desktop. Node 20+ is needed from Step 3.

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

## Tests and checks

```bash
cd backend
uv run pytest            # needs `docker compose up -d db` (uses the hubline_test database)
uv run pytest -m "not db"  # pure unit tests only, no database
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

- Database tests use **real Postgres**, because SKIP LOCKED, ON CONFLICT and partial indexes are Postgres-specific.
- `TEST_DATABASE_URL` points tests at another database, for example a Neon `test` branch. The suite refuses to reset any database whose name doesn't contain `test`.
- No test calls a real external API or LLM. The agent is tested with a scripted fake model, and the LangChain adapter with stub chat models and the real Groq and Gemini exception classes.

To check another database end to end (for example the Neon dev branch), run `uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL`. It applies migrations, compares the schema with the models, boots the app against it and calls `/readyz`. It prints the host only, never the URL.

## API (Step 1)

All `/api/*` routes need `Authorization: Bearer <BACKEND_API_TOKENS entry>`. Only the Next.js server calls them; the browser never does. Errors always look like `{"error": {"code", "message", "details"}}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness and worker status. Doesn't touch the DB, so frequent probes let Neon sleep. |
| GET | `/readyz` | Readiness, including a DB check. For deploy health checks. |
| GET | `/api/messages` | Feed of inbound messages, with the agent's `priority`, `summary`, `agent_status` and `has_draft`. Filters: `source` and `priority` (both repeatable), `min_priority`, `agent_status`, and `status` (`open`/`replied`/`dismissed`/`all`). Sort: `sort=received` (default) or `priority`. Paging: `cursor`, `limit` ≤ 100. |
| GET | `/api/connectors` | Every registered connector with status, sync state and settings schema. |
| PATCH | `/api/connectors/{key}` | `{"enabled": bool, "settings": {...}}`. Settings are merged and validated. |
| POST | `/api/connectors/{key}/sync` | Poll this connector now (202). |
| POST | `/api/connectors/{key}/sample` | Generate one message now. Only for connectors with the `sample` capability, i.e. demo. Optional body `{"scenario": "<name>"}`. The agent triages it within seconds. |
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
| `AGENT_OWNER_NAME` / `AGENT_OWNER_CONTEXT` / `AGENT_VIP_SENDERS` | Dhruv Sonkusare / Python backend developer / empty | Who the agent works for. VIP senders get higher priority. |

## Repository layout

```
backend/    FastAPI app, connectors, workers, Alembic migrations, tests (uv project)
frontend/   Next.js dashboard (Step 3)
docs/       PLAN.md (approved plan), setup guides (Steps 4–6)
reports/    step-<N>-report.md after every build step
```

## Adding a connector

1. Create `backend/app/connectors/<key>.py` with a `@register` subclass of `Connector`. The file name must equal the key.
2. Return `NormalizedMessage`s from `fetch_new()`, and pass the cursor back as an opaque string.
3. Run `uv run pytest tests/connectors/test_contract.py`. The contract suite runs against every registered connector automatically.

No other file changes.
