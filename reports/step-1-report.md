# Step 1 report — Foundation: schema, ingest, connector framework, demo source

**Status:** complete
**Date:** 2026-11-09

## Summary
The backend runs locally against Postgres 16 in Docker.
- **Connectors:** the plug-in framework (contract, registry, auto-discovery) works, and a deterministic demo connector produces labelled demo messages.
- **Scheduler:** it polls enabled connectors using `SKIP LOCKED` leases, backs off when idle, and wakes immediately on API actions.
- **Ingest:** it stores messages in the common format and drops duplicates on `(source, external_id)`.
- **API:** the API serves the feed and connector status and toggles, protected by the server-to-server bearer token.
- **Repo setup:** the approved plan is saved as `docs/PLAN.md`, and `CLAUDE.md` holds the working rules and this report template.

## Files changed
All files are new; the repo started empty. `git init` was run, with no commits.

**Repo root**
- `docs/PLAN.md` — the approved plan with revisions R1–R5 and your decisions merged in at the top
- `CLAUDE.md` — fixed tech decisions, key choices, the stop-after-each-step rule, the report template
- `README.md` — overview, step status, quickstart, API, configuration, how to add a connector
- `docker-compose.yml`, `docker/postgres-init/01-create-test-db.sql` — Postgres 16 on `localhost:5433`, plus a `hubline_test` DB
- `.github/workflows/ci.yml` — backend CI: ruff, format check, mypy, pytest with a Postgres service
- `.gitignore`, `.gitattributes`, `.editorconfig`

**backend/ (uv project)**
- Project and config: `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `alembic.ini`
- `app/config.py` — `Settings` (all env vars, typed), and `DatabaseSettings` for Alembic
- `app/db/base.py` (naming convention), `app/db/engine.py` (asyncpg engine, Neon URL normalisation)
- `app/models/connector.py` (`ConnectorAccount`, `SyncState`), `app/models/message.py` (`Thread`, `Message`)
- `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial_schema.py` — the 4 Step 1 tables and their indexes
- `app/connectors/base.py` — `Connector` ABC, `ConnectorDescriptor`, `ConnectorContext`, capability/auth enums, error hierarchy
- `app/connectors/models.py` — `NormalizedMessage`, `FetchResult`, webhook/reply/send types
- `app/connectors/registry.py` (`@register`), `app/connectors/__init__.py` (`discover()`)
- `app/connectors/demo.py` — the demo connector (12 realistic scenarios, slow cadence, capped)
- `app/services/ingest.py` — thread upsert, `ON CONFLICT DO NOTHING` dedup, echo detection, auto-`replied`
- `app/services/connector_runtime.py` — registry→DB sync, activation rules, `build_connector`
- `app/services/feed.py` — feed query with keyset pagination
- `app/workers/scheduler.py` — claim/lease/poll loop, failure backoff, idle sleep
- `app/workers/supervisor.py` — crash-restarting task runner, wake events
- `app/api/deps.py` (bearer auth, session), `app/api/router.py`, `app/api/routes/{health,messages,connectors}.py`
- `app/schemas/{common,messages,connectors}.py`
- `app/errors.py` (uniform error body), `app/logging.py` (structlog + secret redaction + request id)
- `app/runtime.py`, `app/main.py` (app factory + lifespan)
- `scripts/gen_keys.py` — prints fresh secrets
- `tests/` — 89 tests: `unit/`, `connectors/` (contract suite + demo), `services/`, `workers/`, `api/`, `test_end_to_end.py`, `test_migrations.py`, plus `conftest.py` and `helpers.py`

## Decisions I made myself
1. **Your "my call" answers** (recorded in `docs/PLAN.md` → Decisions):
   - Teams: assume no tenant, so it runs on demo data unless `MS_*` env vars appear.
   - LLM budget: `AGENT_MAX_JOBS_PER_HOUR=30`, plus prefilter and draft-only-when-needed.
   - Hosting: Railway Hobby in Singapore. I rejected Render free with keep-alive, because cold starts on deploy break Slack's 3-second webhook limit and the ping keeps Neon awake anyway.
   - Docker: yes. Docker Desktop was installed, and I started it to run Postgres.
2. **R1 applied to columns as well as tables.**
   - The agent columns on `messages` (priority, summary, agent_status, …) will be added by migration `0002` in Step 2.
   - The Teams subscription columns on `sync_state` will come in Step 6.
   - I dropped `sync_state.backfill_done`, because a null cursor already means "first run".
3. **Connector endpoints arrive in Step 1.** `GET /api/connectors`, `PATCH /api/connectors/{key}` and `POST /api/connectors/{key}/sync` were pulled in from Step 3, so the demo connector can be toggled now (R4).
4. **`DEMO_MODE=true` only pre-enables the demo connector the first time its row is created.** It never overrides your toggle on restart. `backend/.env.example` sets it to `true` for local convenience; the code default is `false`.
5. **The scheduler doesn't use exponential idle backoff.** It sleeps exactly until the next due poll, clamped to `[SCHEDULER_MIN_SLEEP_S=1, SCHEDULER_IDLE_MAX_S=60]`. API actions wake it instantly through an in-process event. This is more accurate than blind doubling and still idles at 60s. The worker's own backoff comes in Step 2.
6. **The lease is renewed after every committed page**, so a long multi-page poll can't be claimed twice. Lease is 120s, per-page timeout 60s, at most 5 pages per poll.
7. **Demo connector cadence:** 6 backfill messages, then one new message every 20 minutes, at most 3 per poll, capped at 100 in total. All of these can be changed via settings. Scenarios are fictional but realistic (an urgent prod incident with a follow-up in the same thread, PR review, newsletter, CI failure, recruiter, client deadline, …). The thread titles mimic other apps, e.g. "LinkedIn · Messages", to make the feed look varied, but `source` is always `demo` and `is_demo` is always true.
8. **`required_settings` (names of Settings fields) replaces the plan's `required_env`.** Values from `.env` never reach `os.environ`, so checking Settings is the correct test. The API still reports missing variables in upper case as `missing_env`.
9. **`ConnectorContext` gained a `clock`**, so connectors are deterministic in tests.
10. **Enum-like columns are `text` + `CHECK`**, with a constraint-naming convention. Connectors with `auth_type=NONE` are `connected` automatically.
11. **The app starts via a factory** (`uvicorn app.main:create_app --factory`), so importing it never needs env vars. `/docs` is disabled when `APP_ENV=production`.
12. **The test DB is reset once per session** (drop schema, `alembic upgrade head`) and truncated per test. The suite refuses to reset any database whose name lacks `test`. The local Postgres uses port 5433 to avoid clashing with a native install.
13. **Sync is a "nudge".** `POST …/sync` just sets `next_poll_at = now()` and wakes the scheduler. It never polls inside the request.

## Test results
```
$ uv run ruff check .            → All checks passed!
$ uv run ruff format --check .   → 63 files already formatted
$ uv run mypy                    → Success: no issues found in 35 source files (strict)
$ uv run pytest                  → 89 passed in 18.8s
$ uv run pytest -m "not db"      → 40 passed, 48 deselected (no database needed)
$ uv run alembic upgrade head / downgrade base / upgrade head → clean
$ uv run alembic check           → No new upgrade operations detected.
```

Highlights:
- **Concurrency:** 4 schedulers ticking at once poll each connector exactly once (SKIP LOCKED + lease).
- **Dedup:** re-ingesting adds 0 rows.
- **Echo handling:** a self-sent message becomes `outbound` and marks earlier open messages in the thread `replied`.
- **Failures:** failure backoff doubles; `AuthExpiredError` sets `needs_reauth` and stops polling; rate limits wait `retry_after`.
- **Wake-up:** an hour-long idle sleep is cut short by the wake event.
- **End to end:** an in-process run goes from app start → `PATCH enable demo` → scheduler → 6 messages in `/api/messages`.
- **Live check:** I ran `uvicorn` against Docker Postgres. `/healthz` and `/readyz` returned 200, a request without a token got 401, and the demo backfill of 6 messages was polled and listed.

## Deviations from the plan
- **Health checks split in two.** `/healthz` no longer touches the DB, so frequent probes don't keep Neon awake. The new `/readyz` does the DB check. The plan had a single `/healthz` with a DB check.
- **Connector endpoints moved earlier**, from Step 3 into Step 1 (see decision 3).
- **`ConnectorDescriptor.required_env` is now `required_settings`** (decision 8).
- **Feed filters.** The `q` full-text filter is gone (R2). The priority and agent filters plus `GET /api/messages/{id}` and `/stats` come in Steps 2–3.
- **Deferred to Step 2:** `workers/maintenance.py`, since nothing needs maintenance yet.
- **Deferred to Step 3:** the frontend CI job.
- **Deferred to Step 4:** `docs/architecture.md`, written alongside the deploy docs.
- **Columns added later** than the plan implied (decision 2).

## Known issues
- **Not yet run against Neon.** No Neon project exists yet, so the migration has only been verified on local Postgres 16. The URL normaliser is unit-tested with a Neon-style URL (`sslmode=require&channel_binding=require`).
- **A 60s idle poll still keeps Neon awake.** Neon free tier only scales to zero after about 5 minutes without queries. Set `ENABLE_SCHEDULER=false`, or `SCHEDULER_IDLE_MAX_S` above 300, when you want it to sleep. This is documented in the README and PLAN.
- **Node.js is not installed on this machine.** It isn't needed until Step 3.
- **uvicorn's access log is plain text.** It isn't routed through structlog yet, so in production it won't be JSON. This is cosmetic; I'll fix it in Step 4 with the deploy config.
- **CI hasn't run yet.** There is no GitHub remote. The workflow mirrors the local commands that pass.

## Manual steps for you
1. **Create the GitHub repo** `Hubline` (public) with no README, so the local repo can be pushed. Tell me when you want the initial commit made and pushed; I haven't committed anything.
2. **Create a Neon project.** At https://console.neon.tech → New project → region **AWS Asia Pacific (Singapore)**, Postgres 16, name `hubline`. Then create a branch called `dev`.
   - Optional check: put the `dev` connection string in `backend/.env` as `DATABASE_URL`, run `uv run alembic upgrade head`, and tell me the result. You can also just send it to me privately via `.env`, never in chat.
3. **Before Step 2:** create an Anthropic API key at https://console.anthropic.com, and set a monthly spend limit of about $5 under Settings → Limits.
4. **Before Step 3:** install Node.js 22 LTS (https://nodejs.org).
5. **Keep Docker Desktop running** whenever you run the backend tests.

## How to verify
```bash
# from the repo root
docker compose up -d --wait db
cd backend
uv sync
cp .env.example .env          # skip if backend/.env exists (I created one with a local token)
uv run python scripts/gen_keys.py   # put BACKEND_API_TOKENS into .env if you created a new .env
uv run alembic upgrade head
uv run pytest                 # expect: 89 passed
uv run uvicorn app.main:create_app --factory --port 8000
```

In a second terminal (Git Bash), from `backend/`:

```bash
TOKEN=$(grep ^BACKEND_API_TOKENS .env | cut -d= -f2)
curl localhost:8000/healthz                                          # {"status":"ok",...,"scheduler":"running"}
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/api/messages  # 401
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/connectors  # demo: enabled true, status connected
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/api/messages?limit=3"   # demo messages, is_demo true
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
     -d '{"enabled": false}' localhost:8000/api/connectors/demo      # enabled false
```

Or open http://localhost:8000/docs, click **Authorize**, and paste the token.

## Open questions
1. **Commit and push:** should I make the initial commit for Step 1 now, and push once the GitHub repo exists?
2. **Neon `dev` branch for local dev:** keep local dev on Docker Postgres (the current setup) and use Neon only for production from Step 4? I recommend Docker locally. It's faster, and it doesn't use up Neon compute hours.
3. **Demo cadence:** is one demo message every 20 minutes, capped at 100, right for presenting? The settings can be changed at runtime with `PATCH /api/connectors/demo`.
4. **Step 2 agent evaluation:** is it OK to spend about $0.05 of Anthropic credit running `scripts/agent_eval.py` on 15 sample messages once your key is in place?
