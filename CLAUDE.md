# CLAUDE.md — working rules for Hubline

Hubline is a single-user unified inbox. Connector plug-ins ingest messages from Gmail, Slack, Teams, LinkedIn and X into Postgres. A LangGraph agent scores priority (1–5), summarises each message and drafts a reply. A human approves every reply on a Next.js dashboard, and the reply goes out through the original app.

The full approved plan is **[docs/PLAN.md](docs/PLAN.md)**. Its "Revisions" (R1–R10) and "Decisions" tables override the original sections.

> **HARD REQUIREMENT: the whole project runs at $0.**
> - Only free tiers: Render Free, Vercel Hobby, Neon Free, Groq/Gemini free LLM tiers, GitHub Actions cron on the public repo.
> - Never add a paid service, plan, API or default.
> - Never make paid API calls (e.g. Anthropic) without the user explicitly asking.

## Build process (MANDATORY)

- We build in **6 steps** (plan §9). Work on **one step only**, the one the user asked for.
- Every step must end with: runnable code, **all tests and checks passing**, an updated README, and `reports/step-<N>-report.md` written with the template below.
- **After writing the report, STOP and wait for the user.** Do not start the next step, even partially.
- Any deviation from the plan goes in the report under "Deviations from the plan". Don't silently diverge.
- Commit or push only when the user asks.

## Fixed tech decisions (do not change without asking)

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, pydantic-settings, pytest. Managed with **uv** (`backend/pyproject.toml`, `uv.lock`).
- **Database:** Postgres (Neon free tier in production, Singapore / AWS ap-southeast-1). SQL only.
- **Agent:** LangGraph orchestration (`app/agent/graph.py`). **The agent never sends.** It has no tools and no access to `send()`.
- **LLM (R7):** every provider goes through **LangChain `init_chat_model`** (`app/agent/llm.py`). There is no direct vendor SDK path.
  - `fake` (default): offline, used by all tests.
  - `groq` (production): `openai/gpt-oss-120b`, strict `json_schema`, `reasoning_effort=low`.
  - `google_genai` (Gemini free tier): `gemini-3.5-flash-lite`, **demo data only**, because Google may use free-tier prompts.
  - `anthropic`: optional extra (`uv sync --extra anthropic`), paid, not used by the plan. It never gets `temperature`.
- **Free-tier limits (R8):**
  - A per-model RPM/TPM pacer runs on every call.
  - A provider 429 **defers** the job by `retry-after` (attempt not counted) and pauses the worker. **It never fails a job.**
  - A rolling 24h token budget is summed from `agent_jobs` token columns (`AGENT_MAX_TOKENS_PER_DAY=180000`), with `AGENT_MAX_JOBS_PER_HOUR=30` for burst shaping only. The token budget and the per-minute pacer are the real limits.
  - **Backfill** (a connector's first sync) is stored in full, but only the newest `BACKFILL_AUTO_TRIAGE=10` per connector are auto-triaged. The rest are `agent_status='skipped'`, with Triage available on request.
  - **Job priority** (`agent_jobs.priority`, claimed highest first): dashboard actions 20 > live messages 10 > backfill 0.
  - Prompts are clipped (body 2,000 chars, thread 3×300). Output caps: 400 tokens (triage), 600 (draft).
- **Background work (R3, R9):** the scheduler and agent worker live inside the backend process. Work is claimed with `SELECT … FOR UPDATE SKIP LOCKED` plus a lease.
  - **Local:** the loops run, idling up to `*_IDLE_MAX_S` (600s, above Neon's 5-min suspend).
  - **Production (Render Free):** the loops are **off**. Work runs only through `POST /internal/tick` (`X-Tick-Secret`; one scheduler pass + ≤60s agent drain, called by the GitHub Actions cron every 30 min), `TICK_ON_STARTUP`, and on-demand kicks from API actions (`WorkRunner`, `app/workers/runner.py`).
  - **Never add anything that queries the DB periodically in production:** Neon Free (100 CU-h) must stay well under its limit.
  - **Scheduling times use the DB clock** (`func.now()`), never the app host's clock.
- **Frontend:** Next.js App Router + TypeScript + Tailwind on Vercel (functions in `sin1`). Auth.js GitHub login with an allowlist. BFF pattern: the browser talks only to Next.js; the Next.js server calls the backend with `BACKEND_API_TOKEN`. No secrets in `NEXT_PUBLIC_*`.
- **Hosting ($0):** backend on **Render Free** (Singapore, 1 instance, 1 uvicorn worker; sleeps after 15 min idle, ~1 min cold start), frontend on **Vercel Hobby** (`sin1`), DB on **Neon Free** (Singapore, autoscaling max 0.25 CU). Slack webhooks that hit a sleeping instance rely on Slack's retries (immediately, +1 min, +5 min).
- **Secrets:** environment variables only. Third-party tokens are encrypted with a single Fernet key (`CREDENTIALS_ENCRYPTION_KEY`, Step 5) and never returned by the API or logged.
- **Repo:** `backend/` and `frontend/` in one public GitHub repo named `Hubline`. The product name in the UI is "Hubline".
- **Timezone:** `Asia/Kolkata`. Owner: Dhruv Sonkusare, Python backend developer.

## Key design choices (from the plan)

- **Connector contract** ([backend/app/connectors/base.py](backend/app/connectors/base.py)): `describe()`, `fetch_new(since: str | None)` where the cursor is opaque, `send(thread_id, text, *, reply_to)`, `parse_webhook(WebhookRequest)`, and `maintain()`.
  - One file per connector, `app/connectors/<key>.py`, with `@register`. **Adding a connector must not require core changes.**
  - Connectors never touch the DB or FastAPI. They receive a `ConnectorContext`.
- **Common format:** `NormalizedMessage` (source, external_id, thread_id, sender, body, received_at, plus extras). Dedup is `UNIQUE (source, external_id)` with `INSERT … ON CONFLICT DO NOTHING`.
- **A connector is active** when it is registered, configured (`required_settings`), connected, enabled (Connectors page / `PATCH /api/connectors/{key}`), and allowed by `CONNECTORS_ENABLED`.
- **Demo data** always has `is_demo=true` and shows a DEMO badge. `DEMO_MODE` (default false) only pre-enables the demo connector on first registration.
- **Incremental schema (R1):** each step adds only the tables and columns it uses, in its own Alembic migration (`0001` Step 1, `0002` Step 2, …). Migrations are hand-written and must pass `alembic check` / `tests/test_migrations.py`.
- **Dropped for v1 (R2):** the audit_events table, tsvector search, and MultiFernet rotation.
- **Sending is at-most-once:** a human triggers it, with an idempotency key. Never retry a send automatically.
- **The agent graph is pure.** It never touches the DB. The worker loads context, runs the graph with no transaction open, then persists. Drafts are versioned (`drafts.version`), and the message row is locked when a new version is written.
- **Agent jobs:**
  - One active job per message, enforced by a partial unique index.
  - Ingest enqueues triage in the same transaction as the insert.
  - A refusal or config error kills a job immediately. A rate limit defers it without counting an attempt. Other errors retry per `AGENT_RETRY_DELAYS_S`.
  - The reaper re-queues jobs stuck in `running`.
- **Errors:** always `{"error": {"code", "message", "details"}}` via `app.errors.ApiError`.
- **`/healthz` must not touch the DB.** Use `/readyz` for DB checks.

## Commands

```bash
docker compose up -d --wait db                 # local Postgres on :5433 (+ hubline_test)
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --port 8000
uv run pytest                                  # all tests (real Postgres, no external APIs)
uv run ruff check . && uv run ruff format --check . && uv run mypy   # strict mypy on app/
```

## Code conventions

- `from __future__ import annotations`; full type hints; mypy strict must pass on `app/`.
- Settings only via `app.config.Settings`. Never read `os.environ` elsewhere.
- Keep DB transactions short. Never hold a transaction open across network or LLM calls.
- Tests: no real external APIs (respx for HTTP, a fake LLM, stub chat models). DB tests are marked `pytestmark = pytest.mark.db`.
- Live LLM checks are free-tier only and run on demand: `scripts/llm_probe.py` (one call) and `scripts/agent_eval.py` (token-capped).
- Don't log secrets; the structlog redaction processor is a backstop, not a licence.

## Step report template — `reports/step-<N>-report.md`

```markdown
# Step <N> report — <title>

**Status:** complete | partial | blocked
**Date:** YYYY-MM-DD

## Summary
2–4 sentences: what now works.

## Files changed
Grouped list (added / modified / deleted) with a one-line purpose each.

## Decisions I made myself
Choices not specified in the plan or by the user, each with the reason.

## Test results
Exact commands run and their output summary (counts, pass/fail), plus lint/type-check results.

## Deviations from the plan
What differs from docs/PLAN.md and why. "None" if none.

## Known issues
Bugs, limitations, tech debt, anything flaky.

## Manual steps for you
Numbered, concrete actions the user must take (accounts, env vars, consoles), with where to click.

## How to verify
Copy-pasteable commands / clicks to see it working yourself, with the expected result.

## Open questions
Decisions needed before or during the next step.
```
