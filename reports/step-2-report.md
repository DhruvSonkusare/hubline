# Step 2 report: Agent worker

**Status:** complete. Two items are blocked on your input (see Known issues): the Neon dev-branch check and the real-model eval.
**Date:** 2026-11-09

## Summary
Every new inbound message is now picked up by an in-process agent worker. A LangGraph graph scores its priority from 1 to 5, writes a one-line summary using thread context, and drafts a reply when one is needed. Results are stored on the message, and drafts are versioned. The agent cannot send anything.

Everything is built and tested on the offline fake model (`LLM_PROVIDER=fake`, the default). The Anthropic adapter uses the official SDK with structured outputs and is ready to switch on with a key.

You asked for a way to trigger the agent live, so `POST /api/connectors/demo/sample` creates one demo message on demand. It is triaged within about a second.

Step 1 was committed as `1e14277`. **Step 2 is not committed yet**; see the open questions.

## Files changed
**Added**
- `backend/app/agent/`
  - `schemas.py`: `TriageResult` and `DraftResult`, plus normalisation (trims rather than failing).
  - `state.py`: `MessageForAgent`, `ThreadItem`, `OwnerProfile`, `AgentState`.
  - `prompts.py`: triage and draft prompts with an injection guard; `PROMPT_VERSION`.
  - `llm.py`: `StructuredLLM` interface with `AnthropicLLM` (official SDK), `LangChainLLM` (other providers, imported lazily), `build_llm`, `call_structured` (one retry on invalid output) and error types.
  - `fake.py`: deterministic offline model (heuristic mode plus a scripted mode for tests).
  - `nodes.py`: prefilter rules, triage, draft, `validate_draft`, and the routing functions.
  - `graph.py`: LangGraph wiring and `run_agent`.
- `backend/app/workers/agent_worker.py`: claims jobs with SKIP LOCKED, loads context, runs the graph, persists results. Also handles retries and dead jobs, the hourly budget guard, and idle backoff with wake-ups.
- `backend/app/workers/maintenance.py`: stale-job reaper.
- `backend/app/services/jobs.py`: `enqueue_triage` (idempotent) and `enqueue` (redraft/retriage, raises if a job is already active).
- `backend/migrations/versions/0002_agent.py`: agent columns on `messages`, the `agent_jobs` and `drafts` tables, `ix_messages_status_priority`, and a backfill for existing rows.
- `backend/scripts/agent_eval.py`: real-model eval on 15 samples with a hard USD cap.
- `backend/scripts/show_feed.py`: prints the latest messages with priority, summary and draft.
- `backend/scripts/verify_db.py`: runs migrations, checks the schema, boots the app and calls `/readyz` against any database URL variable (prints the host only).
- `backend/tests/fixtures/agent/samples.yaml`: 15 eval samples with expected priority bands.
- Tests:
  - `tests/agent/test_rules.py`, `test_graph.py`, `test_llm.py`, `helpers.py`
  - `tests/workers/test_agent_worker.py`
  - `tests/api/test_sample.py`

**Modified**
- `app/config.py`: LLM and agent settings, with `ANTHROPIC_API_KEY` stored as a `SecretStr`.
- `app/models/message.py`, `app/models/__init__.py`: agent columns, plus the `Draft` and `AgentJob` models.
- `app/services/ingest.py`: enqueues a triage job in the same transaction as the insert; outbound rows are marked `skipped`.
- `app/workers/scheduler.py`: wakes the agent worker when it enqueues jobs.
- `app/main.py`: starts the agent worker (`ENABLE_AGENT_WORKER`) and warns if the Anthropic key is missing.
- `app/connectors/base.py`, `app/connectors/demo.py`: `Capability.SAMPLE`, `Connector.sample()`, and the demo implementation.
- `app/api/routes/connectors.py`, `app/schemas/connectors.py`: `POST /api/connectors/{key}/sample`.
- `app/api/routes/messages.py`, `app/schemas/messages.py`, `app/services/feed.py`, `app/schemas/common.py`:
  - agent fields in the feed
  - `priority`, `min_priority` and `agent_status` filters
  - `sort=priority` with keyset paging
- `pyproject.toml`, `uv.lock`: added `anthropic` 2.6.0, `langgraph` 1.2.20 and `tzdata`; dev dependencies `pyyaml` and `types-pyyaml`.
- `.env.example`, `README.md`, `CLAUDE.md`.
- Existing tests updated for the new columns, tables and capability: `conftest.py`, `test_connectors.py`, `test_messages.py`, `test_contract.py`, `test_end_to_end.py`, `test_migrations.py`, `test_config.py`, `test_cursor.py`.

## Decisions I made myself
1. **Anthropic calls use the official `anthropic` SDK directly, not LangChain's `with_structured_output`.**
   - I checked the current API reference. Claude Sonnet 5.5 returns a 400 for forced `tool_choice`, which is how LangChain's structured output works by default. Current models also reject a non-default `temperature`.
   - So `AnthropicLLM` calls `messages.parse(output_format=Schema, output_config={"effort": "low"})`, which gives native structured outputs and validated Pydantic results.
   - LangGraph still orchestrates the agent.
   - The provider stays swappable: `fake`, `anthropic`, or any LangChain provider through `init_chat_model`. That last path needs `uv add langchain langchain-<provider>`; it isn't installed.
2. **No `temperature`, and `effort=low`.** Thinking is on by default on these models and is billed as output, so `low` keeps triage cheap. It is configurable with `LLM_EFFORT`.
3. **The graph is pure.** `load_context` and `persist` are steps in the worker around the graph, not graph nodes. The graph never touches the DB, it is unit-testable without one, and no transaction is open during LLM calls.
4. **Retries happen in three layers:**
   - Transport errors (429, 529, 5xx, network) are retried by the SDK client (`LLM_MAX_RETRIES=3`, honours `retry-after`).
   - Invalid structured output gets one retry with the error fed back.
   - The job itself retries with `AGENT_RETRY_DELAYS_S=30,120,600`, up to 3 attempts.
   - A refusal or config/auth error marks the job dead immediately.
   - No `tenacity` dependency was needed.
5. **The code default is `LLM_PROVIDER=fake`**, so a fresh checkout or CI can never spend money. Production sets `anthropic` (Step 4).
6. **Drafting rule:** a draft is written when `needs_reply` is true or priority is 3 or higher. Drafts are also written for read-only sources; Step 3 will show "Copy draft" for those.
7. **`validate_draft` rejects:**
   - an empty body
   - a body over the source's `max_reply_chars` (default 4000)
   - placeholders such as `[date]`, `{{name}}` or `TODO`
   - an echo of the incoming text

   A first failure gets one redraft with feedback. A second failure keeps the triage and records `agent_error="draft_invalid: …"`.
8. **The rule-based prefilter** gives priority 1 without an LLM call when the mail is bulk (`List-Unsubscribe` or `Precedence: bulk/list/junk`) **and** comes from a no-reply-style sender. Headers are read from `raw["headers"]`; the Gmail connector in Step 5 must put them there.
9. **The budget guard counts jobs started in the last hour** (`AGENT_MAX_JOBS_PER_HOUR=30`). Over the cap, jobs simply aren't claimed until the window frees up. Setting it to `0` pauses the agent.
10. **The reaper runs inside the worker**, at start-up and every `AGENT_MAINTENANCE_INTERVAL_S=900`, not as a separate task. 15 minutes rather than 5 lets Neon sleep when the system is idle. Jobs stuck in `running` for over 10 minutes go back to pending, or to dead if out of attempts.
11. **Worker idle backoff** goes 2s → 4s → … → `WORKER_IDLE_MAX_S` (60s). Ingest, the sample endpoint and the scheduler wake it instantly.
12. **Migration `0002` backfills existing rows.** Existing open inbound messages get a triage job; outbound and already-handled messages get `skipped`. That way no row is left "pending" with no work queued.
13. **On-demand demo message**, which you asked for, uses a generic `sample` connector capability, so the core doesn't special-case the demo connector.
    - Each sample gets its own thread and ID.
    - It works while the 20-minute drip is disabled.
    - It doesn't count toward `max_messages`.
    - An unknown scenario returns 422 with the list of valid ones.
14. **`tzdata` was added.** Windows has no system timezone database, so `Asia/Kolkata` would fail without it.
15. **`verify_db.py`** turns your Neon check into one command, and it prints only the host, never the URL.

## Test results
```
$ uv run ruff check .            → All checks passed!
$ uv run ruff format --check .   → 85 files already formatted
$ uv run mypy                    → Success: no issues found in 46 source files (strict)
$ uv run pytest                  → 161 passed in 33.6s   (Step 1: 89)
$ uv run pytest -m "not db"      → 85 passed, 76 deselected
$ uv run alembic check           → No new upgrade operations detected.
$ alembic upgrade 0002 / downgrade 0001 / upgrade head on local Postgres → clean
```

**Graph** (`tests/agent/test_graph.py`, scripted fake):
- triage + draft
- low priority with no draft
- priority 3 always drafted
- `allow_draft=False`
- skip for outbound/self (no LLM calls)
- rule-based triage (no LLM calls)
- invalid draft retried with feedback
- invalid twice: triage kept, error recorded
- structured-output error retried once, then raised
- refusal propagates
- redraft skips triage and uses the previous draft and instructions
- heuristic fake works with no script

**Rules** (`test_rules.py`):
- prefilter cases, including the bulk-rule matrix
- validator rejections
- normalisation
- the prompt contains the injection guard, and untrusted `</message>` is escaped
- owner timezone shown in prompts (`Mon 09 Nov 2026 12:00 IST`)

**Anthropic adapter** (`test_llm.py`, stub client):
- the request carries `output_format`, `output_config={"effort":"low"}`, and no `temperature` or `thinking`
- usage is accumulated per model
- refusal → `LLMRefusalError`
- `max_tokens` or missing output → `LLMOutputError`
- auth error → `LLMConfigError`
- provider selection; a missing LangChain package gives a clear error

**Worker on real Postgres** (`test_agent_worker.py`):
- ingest enqueues exactly one job, and none for outbound
- persists priority, summary, model, prompt version and draft v1
- thread context reaches the prompt
- retry → pending (respects `run_after`) → dead with `agent_status=failed`
- refusal is not retried
- redraft creates v2 with instructions and doesn't re-triage; a duplicate active job is rejected
- a failed redraft keeps the message `done`
- budget guard caps claims and `0` pauses the agent
- the reaper re-queues or kills stale jobs and leaves fresh ones alone
- **4 concurrent workers process 6 jobs exactly once**
- the wake event interrupts an hour-long idle sleep

**API and end to end:**
- agent fields in the feed
- the priority, `min_priority` and `agent_status` filters
- priority sort with keyset paging (a cursor from one sort is rejected by the other: 400)
- the sample endpoint: 201, job enqueued, 422 for an unknown scenario, 409 without the capability, 404 for an unknown connector
- **end to end:** enabling demo leads to 6 messages ingested and all triaged by the running worker. A live sample is triaged at priority 5 with a draft, while both loops are configured to idle for an hour.

**Migrations:** the `0002` backfill test downgrades to `0001`, inserts rows, upgrades, and checks the jobs and statuses that result.

**Contract:** connectors with the `sample` capability produce valid, unique messages and reject unknown scenarios.

**Live run (local Postgres, fake LLM):**
- `uvicorn` reported both workers `running`.
- On startup the worker triaged the 6 messages left over from Step 1, which `0002` had backfilled.
- `POST /api/connectors/demo/sample {"scenario":"manager-1on1"}` was triaged with a draft within about 2s.
- `scripts.show_feed` printed priorities, summaries and drafts.

**Eval dry run** (`--allow-fake`): all 15 samples ran, and the budget guard and the rule-based newsletter path worked. The fake model scored 11/15. That is keyword heuristics, not a quality signal.

## Deviations from the plan
- **The Anthropic path uses the official SDK instead of LangChain `init_chat_model`** (decision 1). LangGraph orchestration and the env-swappable provider are kept.
- **No `temperature=0.2`.** Current models reject sampling parameters.
- **The graph has no DB nodes** (decision 3): `load_context` and `persist` live in the worker.
- **No `tenacity`**: retries follow decision 4.
- **No chat-message sign-off check in `validate_draft`.** Connectors don't declare an email vs chat style yet. The prompt handles style (email → greeting and sign-off; chat → none).
- **The reaper interval is 15 minutes**, not 5, and runs inside the worker loop (decision 10).
- **Added beyond the plan, at your request:** `Capability.SAMPLE` and `POST /api/connectors/{key}/sample`.
- **Feed agent fields, filters and priority sort were added now.** `GET /api/messages/{id}` and the redraft/retriage/approve endpoints are still Step 3. The services they need (`services/jobs.enqueue`) already exist and are tested.
- **Step 1's "done-when" Neon check is still not done**: the URL isn't in `.env` (see Known issues).

## Known issues
- **Neon dev branch not verified.** `NEON_DEV_DATABASE_URL` is still absent from `backend/.env`; the file hasn't changed since 18:16. It isn't in the Windows environment either. `scripts/verify_db.py` does the whole check in one command once the URL is there. Against local Postgres it reports revision 0002, no schema diff, and `/readyz` 200.
- **The real-model eval has not run.** `ANTHROPIC_API_KEY` is absent, so prompt quality on Haiku 5.5 / Sonnet 5.5 is unverified, and the ≥12/15 done-when target is untested.
  - Estimated cost: about $0.004 for 15 Haiku triages plus about $0.03 for 4 Sonnet drafts.
  - The script stops before crossing `--budget 0.05`, keeping a worst-case reserve before each call.
- **The budget guard counts each job once per hour window** (by its latest start time), so a retry's earlier attempt isn't counted separately. This slightly undercounts during failure storms. It's acceptable for a soft cap; the hard cap is your Anthropic console limit.
- **Older Claude models are not supported.** `effort` is always sent, and a model that rejects it (e.g. Haiku 4.5) would fail its jobs after the retries. Only current models are configured, so this doesn't arise today.
- **No prompt caching.** The system prompts are shorter than the minimum cacheable prefix, so caching wouldn't save anything yet.
- **The LangChain provider path is only tested for its "package missing" error.** It's an escape hatch, not a supported path.
- **Thread context** comes only from messages Hubline has ingested; this is unchanged from the plan.
- **Haiku 5.5 refusals have no server-side fallback.** The job goes dead and the message shows `agent_status=failed` with the reason. Step 3 adds a "Retry agent" button.

## Manual steps for you
1. **Neon:** add `NEON_DEV_DATABASE_URL=postgresql://…` (the `dev` branch connection string from the Neon console) to `backend/.env` and **save the file**. Then either tell me, or run:
   `cd backend && uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL`
   Expected output: revision `0002`, schema diff `none`, `/readyz: HTTP 200`.
2. **Anthropic key:**
   - In https://console.anthropic.com, set a monthly spend limit of about $5.
   - Add `ANTHROPIC_API_KEY=sk-ant-…` to `backend/.env` and save it.
   - Keep `LLM_PROVIDER=fake` in `.env` for everyday local development, so local demo traffic stays free. The eval overrides it per run:
     `LLM_PROVIDER=anthropic LLM_DRAFT_MODEL=claude-sonnet-5-5 uv run python -m scripts.agent_eval`
   - Or tell me the key is in place and I'll run it, capped at $0.05.
3. **GitHub:** send me the repo URL and I'll add it as `origin` and push.
4. **Before Step 3:** install Node.js 22 LTS.

## How to verify
```bash
docker compose up -d --wait db
cd backend
uv sync
uv run alembic upgrade head          # applies 0002
uv run pytest                        # expect: 161 passed
uv run uvicorn app.main:create_app --factory --port 8000
```

In a second terminal (Git Bash), from `backend/`:

```bash
TOKEN=$(grep ^BACKEND_API_TOKENS .env | cut -d= -f2)
curl localhost:8000/healthz      # workers: scheduler running, agent_worker running
curl -X POST -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
     -d '{"scenario": "prod-incident"}' localhost:8000/api/connectors/demo/sample
sleep 2
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/api/messages?sort=priority&limit=3"
#  → top item: priority 5, agent_status "done", has_draft true, a one-line summary
uv run python -m scripts.show_feed --limit 3     # shows the draft text too
uv run python -m scripts.agent_eval --allow-fake # offline dry run of the eval harness
```

## Open questions
1. **Commit Step 2?** I haven't committed it, because you only asked for Step 1 to be committed. Should I commit it now, and push both once you send the URL?
2. **Official SDK rather than LangChain:** are you OK with the Anthropic path using the official SDK instead of LangChain's structured output (decision 1)? It's required for Sonnet 5.5, and the provider stays swappable.
3. **Drafts for read-only sources** (LinkedIn, X): keep drafting them, with "Copy draft" in Step 3, or skip drafting to save tokens?
4. **VIP senders:** send the list whenever you're ready; it goes in `AGENT_VIP_SENDERS`. VIP senders are moved up one priority level.
