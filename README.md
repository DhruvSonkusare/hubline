# Hubline

One inbox for every alert. Hubline pulls messages from Gmail, Slack, Microsoft Teams, LinkedIn and X into a single feed. An AI agent ranks each message by priority, summarises it in one line and drafts a reply. You approve every reply before it is sent back through the app it came from. **The agent never sends anything on its own.**

> **Status: Step 2 of 6 complete (agent worker).** The backend runs locally. It has the connector framework, a demo connector, the polling scheduler, ingest with dedup, a LangGraph agent that scores, summarises and drafts every new message, and the messages and connectors API. The dashboard, deployment and real connectors arrive in Steps 3–6. See [docs/PLAN.md](docs/PLAN.md).

| Step | What it adds | Status |
|---|---|---|
| 1 | Schema, ingest + dedup, connector framework, demo connector, scheduler, base API | ✅ done |
| 2 | LangGraph agent worker: priority, summary, drafts | ✅ done |
| 3 | Full API + Next.js dashboard (local) | ⏳ |
| 4 | Deploy: Vercel + Railway + Neon (Singapore) | ⏳ |
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
  3. `draft`: writes a reply, only when one is needed or priority is 3 or higher.
  4. `validate_draft`: rejects placeholders and over-long drafts, with one retry that feeds the problem back.

  The worker loads context from the DB, runs the graph with no transaction open, then saves the priority, summary and a versioned draft.
- **The LLM provider is swappable.**
  - `LLM_PROVIDER=fake` (the default) is a deterministic offline model, so nothing costs money until you opt in.
  - `anthropic` uses the official SDK with structured outputs and `effort=low`.
  - Any other value goes through LangChain's `init_chat_model`; install the provider package first.
- **Background work runs in the API process.** The scheduler claims due connectors with `FOR UPDATE SKIP LOCKED` plus a lease, so even several instances never poll the same connector twice. When idle it sleeps up to `SCHEDULER_IDLE_MAX_S` (60s by default). API actions such as enabling a connector or "sync now" wake it immediately.

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

### Using the real model

Put `LLM_PROVIDER=anthropic`, `LLM_DRAFT_MODEL=claude-sonnet-5-5` and `ANTHROPIC_API_KEY=...` in `backend/.env`. Then check quality on 15 fixed samples, with a hard spend cap:

```bash
uv run python -m scripts.agent_eval --budget 0.05 --max-drafts 4   # triage table + a few drafts
```

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
- No test calls a real external API or LLM. The agent is tested with a scripted fake model, and the Anthropic adapter with a stub client.

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

## Configuration

Every setting is an environment variable. Locally they live in `backend/.env`; see [backend/.env.example](backend/.env.example) for the full list.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Any `postgres://` URL. Neon's `?sslmode=require` is handled. |
| `BACKEND_API_TOKENS` | — | Comma list, so tokens can be rotated. At least 32 characters each in production. |
| `ENABLE_SCHEDULER` / `ENABLE_AGENT_WORKER` | `true` | Set `false` to stop background DB traffic. |
| `SCHEDULER_IDLE_MAX_S` / `WORKER_IDLE_MAX_S` | `60` | Longest idle sleep. Neon only scales to zero after about 5 minutes without queries, so use more than 300 if the DB should sleep. |
| `DEMO_MODE` | `false` | `true` only pre-enables the demo connector on first registration. |
| `CONNECTORS_ENABLED` | unset | Optional allowlist, e.g. `demo,slack,gmail`. |
| `OWNER_TIMEZONE` | `Asia/Kolkata` | Used for times shown in prompts. |
| `LLM_PROVIDER` | `fake` | `fake` (offline, free), `anthropic`, or a LangChain provider name. |
| `LLM_MODEL` / `LLM_DRAFT_MODEL` | `claude-haiku-5-5` / same | Triage model and draft model. Production uses `claude-sonnet-5-5` for drafts. |
| `LLM_EFFORT` | `low` | Anthropic `effort`. Thinking tokens are billed, so keep it low. |
| `ANTHROPIC_API_KEY` | unset | Needed only when `LLM_PROVIDER=anthropic`. |
| `AGENT_MAX_JOBS_PER_HOUR` | `30` | Budget guard: jobs beyond this wait for the next hour. `0` pauses the agent. |
| `AGENT_CONCURRENCY` | `2` | Jobs processed in parallel. |
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
