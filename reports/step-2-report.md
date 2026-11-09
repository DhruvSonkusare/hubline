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

---

# Addendum (2026-11-09): env files, LangChain check, Neon check

**Status:** Step 2 committed (`036a8a8`). Items 2 and 3 are **still blocked**: neither `ANTHROPIC_API_KEY` nor `NEON_DEV_DATABASE_URL` exists in any env file on disk. The checks are prepared so each one is a single command.

## 1. The `.env` problem
Every `.env*` file in the repo, including git-ignored folders (only `.git` excluded). Variable **names only**:

| File | Size, modified | Variable names |
|---|---|---|
| `D:\project\Hubline\backend\.env` | 1151 B, 2026-11-09 18:16 | DATABASE_URL, BACKEND_API_TOKENS, APP_ENV, LOG_LEVEL, LOG_FORMAT, PUBLIC_BASE_URL, FRONTEND_URL, ENABLE_SCHEDULER, ENABLE_AGENT_WORKER, SCHEDULER_IDLE_MAX_S, WORKER_IDLE_MAX_S, DEMO_MODE, BACKFILL_HOURS, BACKFILL_MAX, OWNER_TIMEZONE |
| `D:\project\Hubline\backend\.env.example` | 1678 B, 2026-11-09 20:46 | the 15 above plus LLM_PROVIDER, LLM_MODEL, LLM_DRAFT_MODEL, LLM_EFFORT, AGENT_CONCURRENCY, AGENT_MAX_JOBS_PER_HOUR, AGENT_RETRY_DELAYS_S, AGENT_OWNER_NAME, AGENT_OWNER_CONTEXT, AGENT_VIP_SENDERS. It also has a commented-out `# ANTHROPIC_API_KEY=` line. |

**Diagnosis:** `backend/.env` is the right file. The backend, Alembic and every script read it. It was created by me in Step 1 and has **not been modified since 18:16**, and it contains neither `NEON_DEV_DATABASE_URL` nor `ANTHROPIC_API_KEY`. The edit most likely went into an unsaved editor tab, or into `.env.example`. Don't put secrets in `.env.example`: it is committed to the public repo.

I also searched outside the repo, up to 3 levels under `D:\project`, `D:\`, and your Desktop, Documents and Downloads, reporting only yes/no per variable. None of these files defines either variable: `D:\Kadders\async-lead-pipeline\.env`, `D:\LLM_API_PRAC\.env`, `D:\eve healthcare\.env(.example)`, `D:\github_repo_guide\.env(.example)`. Neither variable is set in the Windows user or machine environment either (checked earlier).

**Fix:** add these two lines to `D:\project\Hubline\backend\.env` and save:
```
NEON_DEV_DATABASE_URL=postgresql://...      (Neon console → dev branch → connection string)
ANTHROPIC_API_KEY=sk-ant-...
```

## 2. LangChain vs the official-SDK path

**Live calls: not run (no key).** The probe is ready: `backend/scripts/langchain_probe.py`.
- It runs langchain-anthropic in a temporary environment (`uv run --with`), so `pyproject.toml` is unchanged.
- It covers 2 models × 2 methods, with no `temperature`, `max_tokens=1024`, `max_retries=0`, so the first error is shown verbatim.
- It caps spend at $0.02, keeping a $0.012 reserve before each call. The expected total is about $0.01.
```
cd backend
uv run --with langchain-anthropic python -m scripts.langchain_probe --live
```

**What I could establish offline (no API calls).** langchain-anthropic **1.7.10** (with langchain-core 1.7.6 and anthropic 2.6.0) builds these exact request payloads with `ChatAnthropic(..., output_config={"effort": "low"})`:

| Model | Method | temperature | tool_choice | output_config |
|---|---|---|---|---|
| claude-haiku-5-5 | `json_schema` | absent | absent | `effort: low` + `format: json_schema` |
| claude-haiku-5-5 | default (`function_calling`) | absent | **`{"type": "tool", "name": "TriageResult"}` (forced)** | `effort: low` |
| claude-sonnet-5-5 | `json_schema` | absent | absent | `effort: low` + `format: json_schema` |
| claude-sonnet-5-5 | default (`function_calling`) | absent | absent: the tool is offered but not forced | `effort: low` |

Library facts behind this:
- `ChatAnthropic.temperature` defaults to `None`, so it is never sent.
- `_supports_forced_tool_choice()` returns `False` for `claude-fable-5-1`, `claude-opus-5-5` and `claude-sonnet-5-5`. For those models the default method skips forcing. LangChain itself warns: *"You are attempting to use structured output via forced tool calling, which is not supported when `thinking` is enabled or on claude-sonnet-5-5. This method will raise an OutputParserException if tool calls are not generated. Consider `method='json_schema'`…"*
- For Haiku 5.5 the default method **does** force `tool_choice`, even though Haiku 5.5 runs adaptive thinking by default. Only the live call can show whether the API accepts that.
- `method="json_schema"` binds `output_config.format`, and LangChain **merges** it with the model-level `output_config.effort`. This is the same request shape the SDK path sends.

**Correction to the Step 2 report.** Decision 1 said LangChain's structured output relies on forced `tool_choice`. That is true **only for the default method**. With `method="json_schema"`, langchain-anthropic 1.7.10 uses native structured outputs with no `tool_choice`. So the main reason for leaving LangChain may not hold. I'm not switching anything until the live calls answer it. The rule you set will decide:
- If `json_schema` works on both models → switch the `anthropic` provider back to LangChain (`ChatAnthropic` + `with_structured_output(method="json_schema")`), add `langchain-anthropic` as a dependency, and remove `AnthropicLLM`.
- If it genuinely fails → keep the SDK path and record the error text here.
- Either way, the default-method results are recorded as evidence.

## 3. Neon dev branch
**Not run: `NEON_DEV_DATABASE_URL` is absent.** One command once it's saved in `backend/.env`:
```
cd backend
uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL
```
It applies migrations 0001–0002, compares the schema with the models, boots the app with both workers off and `DEMO_MODE=false`, and calls `/readyz`. It prints the host only.

Reference run against local Postgres: `migrations: at revision 0002; schema diff vs models: none` and `/readyz: HTTP 200 {"status":"ok","db":"ok",...}`.

## 15-sample eval
**Not run (no key).** Once the key is saved, and after item 2 settles which path the `anthropic` provider uses:
```
LLM_PROVIDER=anthropic LLM_DRAFT_MODEL=claude-sonnet-5-5 uv run python -m scripts.agent_eval --budget 0.05 --max-drafts 4
```

## Answers recorded
- Drafts for LinkedIn and X: **keep** (current behaviour; Step 3 shows "Copy draft").

## Files changed in this addendum (uncommitted)
- `backend/scripts/langchain_probe.py` (new): offline payload inspection plus live probe with a spend cap.
- `reports/step-2-report.md`: this addendum.

## Next actions, in order, once you've saved `backend/.env`
1. `verify_db` against Neon, then report.
2. `langchain_probe --live` (≤ $0.02), then apply the switch-or-keep rule above and re-run the tests.
3. `agent_eval` (≤ $0.05), then add the triage table and cost here.

---

# Addendum 2 (2026-11-09): the $0 requirement

**Status:** code, plan and docs are done. All tests and checks pass, and the changes are committed.

**Live verification is blocked on keys and a URL:**
- `GROQ_API_KEY` and `GOOGLE_API_KEY` are not in `backend/.env` (checked by name only), so no live Groq or Gemini probe ran.
- `NEON_DEV_DATABASE_URL` is now present, but malformed (see §6).
- `ANTHROPIC_API_KEY` is now present, but **no Anthropic call was made**. Under the $0 rule, and with your instruction to move everything to LangChain, the earlier "LangChain vs SDK" live probe is moot.

## 1. LLM: Groq free tier through LangChain, Gemini as the second free provider
**Model choice: `openai/gpt-oss-120b` on Groq.** It is the best free Groq model that supports structured output.
- It is a **production** model; `qwen/qwen3.8-27b` and `meta-models/muse-spark-open` are preview models that may disappear at short notice.
- It supports **strict** structured outputs (constrained decoding: `strict: true`, every field required, `additionalProperties: false`). Our `TriageResult` and `DraftResult` schemas meet that (checked with LangChain's `convert_to_json_schema(strict=True)`).
- The other production models have no strict support: `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` only have JSON-object mode or tool calling.
- `gpt-oss-20b` has the same free limits but is the weaker sibling.
- Free limits for gpt-oss-120b: **30 RPM, 1K RPD, 8K TPM, 200K TPD**. Groq's docs table is labelled with base limits, and several third-party sources quote these same numbers as the free tier. **To be confirmed** in the console limits page / response headers once the key exists.
- Groq docs used: console.groq.com/docs/structured-outputs, /docs/rate-limits, /docs/models, /docs/reasoning.

**Gemini (`LLM_PROVIDER=google_genai`, `GOOGLE_API_KEY`):** the default model is `gemini-3.5-flash-lite`, listed as stable in AI Studio's model list.
- Free limits are shown only in AI Studio. Unofficial sources say ~10 RPM and ~500 RPD for Flash-Lite, with RPD resetting at midnight Pacific.
- Structured output: langchain-google-genai's default `json_schema` (`response_json_schema`).
- README warning, quoting Google's terms for unpaid services: content is used to "provide, improve, and develop" Google products, "human reviewers may read, annotate, and process your API input and output", and "Do not submit sensitive, confidential, or personal information". So Gemini is for **demo accounts only**.

**Code changes:**
- **`app/agent/llm.py`:** `AnthropicLLM` and the direct `anthropic` dependency are **removed**.
  - `build_llm()` uses `init_chat_model` for every provider: `groq`, `google_genai`, `anthropic` (optional extra) and any other.
  - The structured-output method is chosen per provider: Groq strict `json_schema` on strict-capable models, `function_calling` otherwise; `json_schema` for Gemini and Anthropic.
  - Per-provider options: `reasoning_effort=low` for gpt-oss; `output_config.effort` for Anthropic, which never gets `temperature`; `LLM_TEMPERATURE` sent only when set.
  - Construction errors become a clear `LLMConfigError`, e.g. *"could not create groq model 'openai/gpt-oss-120b' (is GROQ_API_KEY set in backend/.env?)"*.
- **Dependencies:** added `langchain`, `langchain-groq`, `langchain-google-genai`; removed `anthropic`. `langchain-anthropic` moved to the optional extra `anthropic` (`uv sync --extra anthropic`).
- **`LLM_PROVIDER=fake` stays the default** for local dev and tests.
- **Scripts:**
  - `scripts/llm_probe.py` (new) makes one structured call against any provider.
  - `scripts/agent_eval.py` now has a **token** cap instead of a USD cap, uses the pacer, and retries a 429 once after `retry-after`.
  - `scripts/langchain_probe.py` (Anthropic-specific) is deleted.

## 2. Rate limits
- **Pacer:** a per-model sliding 60-second window (`LLM_RPM=30`, `LLM_TPM=8000`).
  - Before each call it estimates the tokens (prompt characters/3 + half of `max_tokens`) and waits up to 20 s for room.
  - If it would need longer, it raises `LLMRateLimitError(retry_after)`. The estimate is replaced by the real `usage_metadata` total afterwards.
  - Triage and draft share one limiter when they use the same model.
- **A provider 429 defers the job and never fails it.** `classify_error()` walks the exception chain and finds the status in `status_code`, `code` or `response.status_code`. The wait comes from, in order:
  1. `retry-after`
  2. Groq's `x-ratelimit-reset-tokens` / `-requests` (e.g. `2m59.5s`)
  3. Gemini's `retryDelay` / "try again in …"
  4. a 60 s default

  The worker then:
  - puts the job back in `pending` with `run_after = now() + retry_after`;
  - **gives the attempt back** (`attempts - 1`);
  - records the tokens used so far;
  - shows "rate limited by the LLM provider; retrying in Ns" on the message;
  - **pauses itself** until then, so later jobs don't hit the same limit.

  Tests cover a Groq 429 with `retry-after`, a Groq reset-header duration, a Gemini `RESOURCE_EXHAUSTED` with `retryDelay`, a 429 wrapped in another exception, five 429s in a row (the job still has 0 attempts used), and the pause.
- `LLM_MAX_RETRIES=0`, so 429s reach the worker straight away instead of being slept on inside the client.
- **Daily budget:** migration `0003` adds `agent_jobs.input_tokens`, `output_tokens` and `llm_calls`, accumulated on **every** attempt: success, failure and deferral, through a per-job `contextvar`. The worker stops claiming when the **rolling 24 h** sum reaches `AGENT_MAX_TOKENS_PER_DAY`.
- **Prompt trimming:**
  - The message body sent to the LLM is clipped at 2,000 characters (`AGENT_BODY_CHARS`).
  - Thread context is 3 messages × 300 characters (was 8 × 1,500).
  - The previous draft is clipped at 1,500 characters, and timestamps are shorter.
  - Output caps are `max_tokens` 400 (triage) and 600 (draft); they were 2,000 / 4,000.
  - Prompt version bumped to `triage-v2/draft-v2`.

**Per-message token cost** (measured prompt sizes from the eval dry run; outputs are the caps or estimates):

| Call | Input | Output (gpt-oss reasoning at `low` + JSON) | Typical | Worst case (clipped) |
|---|---|---|---|---|
| Triage | ≈ 320 tokens measured (system ≈ 210 + message) | ≈ 150–300 | ≈ 550 | ≈ 1,000 in + 400 cap = 1,400 |
| Draft (only when a reply is needed or P≥3; ≈ 60% of messages) | ≈ 330–380 | ≈ 200–450 | ≈ 700 | ≈ 1,200 in + 600 cap = 1,800 |
| **Per message** | | | **≈ 550 + 0.6×700 ≈ 1.0K** (≈ 1.3K when drafted) | **≈ 3.2K** |

**Budget arithmetic for `AGENT_MAX_JOBS_PER_HOUR`:**
- Groq free TPD for gpt-oss-120b is **200,000 tokens/day**.
- Keep 10% (20K) for `llm_probe`, `agent_eval` and the odd parse retry: **`AGENT_MAX_TOKENS_PER_DAY = 180,000`**.
- Budget each job at **2,500 tokens**. That is conservative: above the ≈1.0–1.3K typical, it covers a parse retry or a long message, and it sits below the 3.2K hard worst case.
- Jobs per day = 180,000 / 2,500 = **72**. Jobs per hour = 72 / 24 = **3.0**, so **`AGENT_MAX_JOBS_PER_HOUR = 3`**.
- Check: 3 jobs/h × 24 h × 2,500 = **180,000 ≤ 180,000** ✔.
- The rolling 24 h guard (real token counts from the DB) is the backstop if real usage runs above the estimate.

The other free limits:

| Limit | Worst case | Free limit |
|---|---|---|
| Requests per day | 72 jobs × ≤ 2 calls = 144 (288 with a parse retry on every call) | 1,000 ✔ |
| Tokens per minute | `AGENT_CONCURRENCY=1` and ≤ 3.2K per job; the pacer enforces it | 8,000 ✔ |
| Requests per minute | ≤ 2 calls per job; the pacer enforces it | 30 ✔ |

- The demo drip (1 message / 20 min = 3/h) matches the cap exactly.
- **On a demo day,** temporarily raise `AGENT_MAX_JOBS_PER_HOUR` (e.g. 20). The 24 h token guard still stops at 180K, so the free TPD is never exceeded.

## 3. Hosting: Render Free, with tick + on-demand work instead of loops
- **`POST /internal/tick`** (`app/api/routes/internal.py`) checks the `X-Tick-Secret` header in constant time against `TICK_SECRET`. It returns 404 when the secret is unset and 401 when it's wrong.
  - It runs **one scheduler pass**, then **drains agent jobs for at most `TICK_BUDGET_S` = 60 s** (`app/workers/runner.py`, `WorkRunner.tick`).
  - It is single-flight: an overlapping call returns `"busy"`.
  - A drain stops for one of four reasons: `drained`, `time_limit`, `rate_limited` or `budget`.
  - The time limit is checked between jobs, so one in-flight job can run a little past 60 s.
  - `?wait=true` returns `{polled, agent_jobs, elapsed_s, stopped}`; otherwise it answers 202 and runs in the background.
- **`TICK_ON_STARTUP`** runs a tick when the process boots. On Render Free a cold start (~1 min) can outlast the waking request, so the work still happens.
- **`WORK_ON_DEMAND`:** with the loops off, the following run one background pass, so the demo stays responsive between cron ticks:
  - **Enable connector / sync now:** one scheduler tick.
  - **Generate sample** (and later webhooks/approvals): one agent drain.
  - A kick that arrives during a running pass sets a re-run flag rather than being lost.
- **Production env:** `ENABLE_SCHEDULER=false`, `ENABLE_AGENT_WORKER=false`.
- **Cron: GitHub Actions** (`.github/workflows/tick.yml`) is the primary, running every 30 min from 06:30 to 23:00 IST, with `curl --retry 3 --max-time 150` and `?wait=true`.
  - It is inert until the `TICK_URL` and `TICK_SECRET` repo secrets exist.
  - Why not cron-job.org (documented as an alternative): cron-job.org's FAQ says requests time out **after 30 s** and jobs are deactivated **after more than 25 failures**. Render's ~1 min spin-up would turn most cold-start calls into "failures", even though the tick still runs via `TICK_ON_STARTUP`.
- **Slack retries** (from Slack's Events API docs, now in PLAN §8):
  - A 2xx is required **within 3 s**.
  - Up to **3 retries**: nearly immediately, after 1 min, after 5 min, with `X-Slack-Retry-Num` and `X-Slack-Retry-Reason`. Optional Delayed Events add hourly retries for 24 h.
  - Auto-disable happens only above **95% failures within 60 min**, and **apps receiving fewer than 1,000 events/hour are exempt**.
  - So an event hitting a sleeping instance arrives 1–5 min late through a retry, and dedup makes repeated deliveries harmless.
- **Bug found and fixed while testing:** "enable connector" and "sync now" set `next_poll_at` from the **app host's clock**, but the scheduler compares it with the **DB's** `now()`.
  - With the host clock slightly ahead (Docker vs Windows here; Render vs Neon in production), the immediate poll was silently skipped.
  - The failure was intermittent and showed up only under full-suite timing.
  - Those writes, the scheduler's failure backoff, and the worker's retry/defer `run_after` now all use `func.now()`. A rule was added to CLAUDE.md.

## 4. Neon Free (100 CU-h/month, 5-min suspend)
- `SCHEDULER_IDLE_MAX_S` and `WORKER_IDLE_MAX_S` now **default to 600 s** (> 300). Production goes further: **no loops**, so nothing queries the DB between ticks and requests.
- **Estimate** (Neon compute set to a fixed **0.25 CU**; 30-day month):

| Source | Assumption | Awake | CU-h/month |
|---|---|---|---|
| Cron ticks | 36/day; boot + migrations + tick ≈ 1 min of queries, then 5 min until suspend ≈ 6 min | 3.6 h/day | 0.25 × 3.6 × 30 = **27.0** |
| Dashboard | ≈ 1 h/day open (presenting/testing) | 1.0 h/day | **7.5** |
| Webhooks / on-demand outside tick windows | ≈ 20 isolated events/day × 5 min (pessimistic: most overlap) | 1.7 h/day | **12.5** |
| **Total** | | | **≈ 47 CU-h ≈ 47% of 100** |

  - Variants: 24/7 ticks (48/day) ≈ **56**; hourly ticks ≈ **33**.
  - For comparison, the earlier design with loops querying every ≤ 60 s while Render was awake would have kept Neon up roughly 37–50% of the time: ≈ 66–90 CU-h.
- **Render instance hours:** 36 wakes × (1 min boot + 15 min idle) ≈ 9.6 h/day ≈ 290 h + ≈ 60 h of dashboard/webhook wakes ≈ **350 of 750** free hours.

## 5. Plan and docs
- **`docs/PLAN.md`:**
  - New revisions **R6–R10** ($0, LangChain-only LLM, rate limits, Render Free work model, Neon budget).
  - Decisions 4, 5 and 11 rewritten.
  - §6 LLM section rewritten, with a provider table and a pacing/budget subsection.
  - **§8 deployment rewritten**: free-tier table, Render config, tick/startup/on-demand work model, GitHub Actions vs cron-job.org, Slack retry facts, Neon CU-h estimate, Render hours, Vercel Hobby cold-backend note, env vars.
  - **§9 Step 4 checklist rewritten** for Render Free + Vercel Hobby + Neon Free + the tick cron.
  - §11 risks rewritten.
  - Old §12 questions 4 and 5 marked as superseded.
  - **Every Railway / Render Starter / Fly.io / dollar figure was removed.** What remains: Anthropic is listed only as an optional code path "not used by the plan", which you asked to keep, and §11 notes as a fact that X's DM API is paid, which is why X stays read-only demo data.
- **`CLAUDE.md`:** a $0 hard requirement at the top; LLM, rate-limit, work-model and hosting rules updated; the DB-clock rule; a ban on periodic DB queries in production.
- **`README.md`:** the $0 stack, the free-provider instructions, the **Gemini data-use warning**, the `/internal/tick` docs, the cron note and the new config rows.
- **`.env.example`:** new defaults, plus a Neon URL example showing the `ep-…` host.

## 6. Live checks: status
| Check | Result |
|---|---|
| Groq probe (`scripts.llm_probe --provider groq`) | **Not run: `GROQ_API_KEY` absent.** The script exits with a clear config error instead of a traceback. |
| Gemini probe | **Not run: `GOOGLE_API_KEY` absent.** |
| 15-sample eval on Groq | **Not run (no key).** Offline dry run: all 15 samples ran, with per-sample token counts (≈ 320–720 incl. drafts; fake 50-token outputs). |
| Neon dev (`scripts.verify_db`) | **Failed before connecting: the URL's host is literally `...ap-southeast-1.aws.neon.tech`.** The `ep-xxxx-xxxx` endpoint part was replaced by `...`, probably copied from the console's abbreviated display. DNS rejects the empty label (`UnicodeError: label empty or too long`). Nothing was migrated. Fix: copy the full connection string (Neon console → Connect → copy), whose host starts with `ep-`. |
| Local live run of the production work model | uvicorn with loops off and a tick secret: `healthz` showed both workers `disabled`; a wrong secret got **401**; the startup tick and `POST /internal/tick?wait=true` both returned `{"stopped": "drained"}`; a demo sample was drained on demand and came out as **P4, done, with a draft**. |

## 7. Tests and checks
```
uv run ruff check .            → All checks passed!
uv run ruff format --check .   → 91 files already formatted
uv run mypy                    → Success: no issues found in 48 source files (strict)
uv run alembic check           → No new upgrade operations detected. (0003 round-trips)
uv run pytest                  → 193 passed   (was 161)
uv run pytest -m "not db"      → 100 passed, 93 deselected
```
New tests:
- `tests/agent/test_llm.py`, rewritten:
  - the LangChain adapter with a stub chat model, and the structured-output method per provider
  - real `groq.RateLimitError` / `AuthenticationError` / `NotFoundError` and `google.genai` `ClientError` mapping, with `retry-after` parsing
  - Groq/Gemini/Anthropic wiring built offline (`reasoning_effort`, `max_tokens`, `max_retries=0`, no `temperature` for Anthropic)
  - the missing-key error
  - pacer behaviour: RPM wait, TPM with actual tokens, deferral of long waits, an oversized single request
- `tests/workers/test_free_tier_guards.py`: 429 deferral, repeated 429s, per-job tokens (including on failure), the 24 h budget, body clipping.
- `tests/api/test_internal_tick.py`:
  - auth (404 / 401), `wait=true` / `202`, time limit, single-flight
  - on-demand sample and enable without loops, startup tick, `WORK_ON_DEMAND=false`

## 8. Manual steps for you
1. **Groq (free):** create a key at https://console.groq.com → API Keys, then add `GROQ_API_KEY=gsk_…` to `backend/.env` and **save the file**. Then:
   `uv run python -m scripts.llm_probe --provider groq` → expected `OK … priority=5`.
   `uv run python -m scripts.agent_eval --provider groq` → triage table, about 15–25K tokens, $0.
2. **Gemini (optional, free):** create a key at https://aistudio.google.com → Get API key, add `GOOGLE_API_KEY=…`, then run `uv run python -m scripts.llm_probe --provider google_genai`. Demo data only.
3. **Neon:** replace `NEON_DEV_DATABASE_URL` with the **full** connection string, whose host starts with `ep-`. Then run `uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL`. Also set the branch's compute autoscaling **max to 0.25 CU**.
4. **Your local `backend/.env`** still has `SCHEDULER_IDLE_MAX_S=60` and `WORKER_IDLE_MAX_S=60` from Step 1. That's fine locally (Docker), but copy the new keys from `.env.example` when convenient.
5. **Optional:** remove `ANTHROPIC_API_KEY` from `backend/.env`. Nothing uses it under the $0 plan.

## 9. Open questions
1. **Throughput vs the free budget:** 3 messages/hour sustained (72/day) is what fits 180K tokens/day at a conservative 2.5K/job. Do you want a higher hourly cap with the 24 h token guard as the only hard limit? The cap then only shapes bursts.
2. **Tick window:** keep 06:30–23:00 IST (≈ 47 CU-h/month), or go 24/7 (≈ 56)?

---

# Addendum 3 (2026-11-09): live checks, measured tokens, backfill limits

**Status:** everything requested is done and verified live. All three checks ran in the order you gave: Neon `verify_db`, then the Groq probe, then the Groq eval.
- **Eval:** passed **15/15**.
- **Cost:** **$0** (Groq free tier).
- **No Anthropic call was made.**

## Changes
1. **`AGENT_MAX_JOBS_PER_HOUR` raised to 30.** It now only shapes bursts. The rolling 24h token budget (`AGENT_MAX_TOKENS_PER_DAY=180000`) and the per-minute pacer (30 RPM / 8K TPM) are the real limits.
2. **Ticks stay at 06:30–23:00 IST.** The README now has a callout: GitHub Actions schedules can be delayed, and they are disabled after 60 days without repository activity. It also says how to re-enable them, and why a missed tick only delays polling.
3. **Backfill can't drain the budget.**
   - **First sync is stored in full.** A connector's first poll (no cursor yet) inserts every message, across all pages, with `agent_status='skipped'` and an explanatory `agent_error` ("Not auto-triaged: older backfilled message … Use Triage").
   - **Only the newest 10 are triaged.** When the poll finishes, `promote_newest_backfill` moves the newest `BACKFILL_AUTO_TRIAGE` (default **10**) inbound messages to `pending` and queues them at **priority 0**. The promotion also runs if a later page fails, so pages already committed still get their newest 10.
   - **The rest wait for the Triage button.** `services.jobs.enqueue(message_id, "triage")` already re-triages a skipped message at dashboard priority; the button arrives in Step 3.
   - **Priority (migration `0004`):** a new `agent_jobs.priority` column, with claims made `ORDER BY priority DESC, run_after, id` and the ready index rebuilt to match. Live messages from regular polls (and webhooks later) queue at **10**. Dashboard actions (Triage/Redraft/Retry, generated samples) queue at **20**. So live messages and dashboard actions always go before backfill.
4. **Reasoning tokens are recorded.**
   - **Effort:** `reasoning_effort=low` is set on the Groq model; the probe prints it from the live model object.
   - **Source:** reasoning tokens come from LangChain's `usage_metadata.output_token_details.reasoning`, falling back to Groq's raw `completion_tokens_details.reasoning_tokens`.
   - **Storage:** the per-job total is stored in the new `agent_jobs.reasoning_tokens` column.
   - **Reporting:** the probe and the eval print input, output and reasoning tokens per message. `output_tokens` *includes* reasoning (provider convention).

## 1. Neon dev branch: `scripts.verify_db`
```
target host: ep-<redacted>.c-2.ap-southeast-1.aws.neon.tech
migrations: at revision 0004; schema diff vs models: none
/readyz: HTTP 200 {"status":"ok","db":"ok","workers":{"scheduler":"disabled","agent_worker":"disabled"}}
```
✔ This closes the Step 1 done-when item. All four migrations apply cleanly on Neon Free (Postgres in Singapore), the schema exactly matches the models, and the app boots against it.

**Note:** the URL is the **pooled** endpoint (`-pooler`, PgBouncer). It worked here: Neon's pooler supports protocol-level prepared statements. But PLAN §8 specifies the **direct** endpoint for production, because asyncpg relies on prepared statements and we run one process with our own pool. For Render (Step 4), use the direct connection string: the same host without `-pooler`.

## 2. Groq probe: `scripts.llm_probe --provider groq`
```
provider=groq model=openai/gpt-oss-120b method={'method': 'json_schema', 'strict': True} reasoning_effort=low max_tokens=400
OK in 0.8s: priority=5 category=action_required
   summary: Priya Raman: rollback decision for checkout API 500s (30 min)
   tokens: in=507 out=100 (of which reasoning=17, answer=83) total=607
```
✔ Key, model, strict structured output and `reasoning_effort=low` all work. One request.

## 3. Groq eval: `scripts.agent_eval --provider groq --max-drafts 15 --max-tokens 40000`
Drafts were uncapped, so the draft rate is the real one. Columns are input/output/reasoning tokens per call; output includes reasoning.

| id | want | got | ok | by | triage in/out/rsn | draft in/out/rsn | total |
|---|---|---|---|---|---|---|---|
| prod-outage | 5 | 5 | ✔ | llm | 528/135/51 | 474/174/39 | 1311 |
| manager-today | 4-5 | 4 | ✔ | llm | 495/160/76 | 438/145/33 | 1238 |
| client-friday | 3-4 | 3 | ✔ | llm | 517/154/75 | 459/146/42 | 1276 |
| pr-review-week | 2-3 | 3 | ✔ | llm | 492/139/56 | 431/77/34 | 1139 |
| standup-move | 3-4 | 4 | ✔ | llm | 494/121/38 | 434/90/42 | 1139 |
| recruiter | 1-3 | 3 | ✔ | llm | 506/139/62 | 440/161/44 | 1246 |
| friend-meetup | 2-3 | 3 | ✔ | llm | 978/250/98 *(incl. 1 structured-output retry)* | 428/117/29 | 1773 |
| newsletter | 1 | 1 | ✔ | rules | – | – | 0 |
| marketing | 1 | 1 | ✔ | llm | 492/89/16 | – | 581 |
| invoice | 1-2 | 2 | ✔ | llm | 501/112/28 | – | 613 |
| ci-failure-main | 2-4 | 2 | ✔ | llm | 485/136/52 | – | 621 |
| security-alert | 3-5 | 5 | ✔ | llm | 510/148/69 | 452/162/39 | 1272 |
| fyi-docs | 1-2 | 2 | ✔ | llm | 487/102/24 | – | 589 |
| thanks | 1-2 | 2 | ✔ | llm | 483/114/36 | – | 597 |
| injection | 1-2 | 1 | ✔ | llm | 495/118/51 | – | 613 |

```
passed 15/15 (ran 15); LLM calls 23 (triage 15, draft 8); tokens in 11019 out 2989 (reasoning 1034) total 14008; cost $0
triage call  input     mean 533  median 495  max 978  n=14
triage call  output    mean 137  median 136  max 250  n=14
triage call  reasoning mean  52  median  52  max  98  n=14
draft call   input     mean 444  median 440  max 474  n=8
draft call   output    mean 134  median 146  max 174  n=8
draft call   reasoning mean  38  median  39  max  44  n=8
per message (all)      mean 1001  median 1139  max 1773  n=14
per message (drafted)  mean 1299  median 1272  max 1773  n=8
draft rate             8/14 LLM-triaged messages
```

**Observations:**
- The prompt-injection sample was triaged as priority 1 with the summary "request API keys"; the injection did not steer the agent.
- The bulk newsletter took the free rule-based path (0 tokens).
- One triage needed the structured-output retry, which doubled its input to 978.
- Reasoning at `low` is small: **≈ 7% of all tokens** (1,034 of 14,008).
- Groq counts the JSON schema and chat format as input. That is why measured input (≈ 495 per triage call) is higher than my chars/4 estimate (≈ 320).

**Draft quality issues (not fixed in this addendum; candidates for prompt tuning):**
- `manager-today`: the draft **invented** three status bullets. The prompt forbids inventing facts, so it should have listed the gap in `open_questions` instead.
- `security-alert`: it drafted a reply to an automated GitHub notice. The cause is the "draft whenever priority ≥ 3" rule; a better rule is "draft only if `needs_reply`", or "skip drafts for `automated` senders".

## 4. Budget arithmetic, redone from the measured numbers
**Measured per message:** mean **1,001** tokens (all LLM-triaged). **1,299** when a draft is written. Max **1,773**, including one structured-output retry. Draft rate 8/14 = 57%. Reasoning is ≈ 90 tokens per drafted message.

**Daily token budget (the real limit):** `AGENT_MAX_TOKENS_PER_DAY = 180,000`, which is 90% of the 200K TPD. That leaves 20K for probes and evals; today's eval used 14,008.
- At the mean, 180,000 / 1,001 ≈ **180 messages/day**.
- At the measured worst case, 180,000 / 1,773 ≈ **100 messages/day**.
- So the free tier covers **≈ 100–180 messages/day**. The previous planning figure of 2.5K/job (72/day) was about 2× too pessimistic.

**Hourly cap 30 (burst shaping):** 30 messages/hour × 1,001 ≈ 30K tokens/hour. A sustained full-rate burst would hit the 24h budget after ≈ 6 hours (≈ 3.4 h at the worst-case size). Then the guard stops claiming and jobs wait until the rolling window frees. In practice volume is far lower: the demo drip is 3/hour and the demo Slack/Gmail accounts are quiet.

**Per-minute limits:**
- **Tokens per minute:** a message is ≤ 1,773 tokens across ≤ 2–3 calls, and `AGENT_CONCURRENCY=1`. So at most ≈ 4–6 messages/minute fit in 8K TPM, and the pacer enforces it. 30/hour is 0.5/minute ✔.
- **Requests per minute:** 1.64 calls/message (23 calls / 14 messages) × 0.5 messages/minute ≈ 0.8 RPM, against the 30 RPM limit ✔.
- **Requests per day:** at the token-bounded maximum of ≈ 180 messages/day × 1.64 ≈ **295 requests/day**, against 1,000 RPD ✔. Even 30/hour for 24h (720 messages, ≈ 1,180 requests) can't happen, because the 180K token guard stops at ≈ 100–180 messages.

**Backfill:**
- One connector's first sync auto-triages ≤ 10 messages × 1,001 ≈ **10K tokens**, or ≤ 18K at the worst case. That is ≈ 6–10% of the daily budget.
- Demo + Slack + Gmail together ≈ 30K, or ≈ 17%.
- Older history costs nothing until you press Triage.

## 5. Tests and checks
```
uv run ruff check .            → All checks passed!
uv run mypy                    → Success: no issues found in 48 source files (strict)
uv run alembic check           → No new upgrade operations detected. (0004 round-trips locally; applied on Neon)
uv run pytest                  → 201 passed   (was 193)
```
New tests in `tests/workers/test_backfill_priority.py`:
- **Newest 10 of a 25-message backfill:** a two-page backfill auto-triages exactly the newest 10. The other 15 are skipped with the note, the jobs get priority 0, the agent is kicked, and the next live poll queues at priority 10.
- **`BACKFILL_AUTO_TRIAGE=0`:** nothing is queued.
- **Triage button on a skipped message:** queues at priority 20 and is processed.
- **Claim order:** dashboard first, then live, then backfill, even though the backfill job is the oldest.
- **Reasoning tokens:** persisted on the job.

Other new tests:
- the reasoning-token extraction paths, for LangChain metadata and for the raw Groq usage (`tests/agent/test_llm.py`)
- samples queued at dashboard priority (`tests/api/test_sample.py`)

## 6. Plan and docs
- **`docs/PLAN.md`:**
  - Decision 4 updated: the token budget is the real limit, the hourly cap is 30, backfill auto-triages 10, and jobs are claimed in priority order.
  - §6 daily-budget bullet rewritten with the measured numbers, plus a new "Backfill and priority" bullet.
  - §8 env tables updated.
  - §11 Groq-limits risk rewritten (≈ 100–180 messages/day).
- **`CLAUDE.md`:** the budget, backfill and priority rules.
- **`README.md`:** the `AGENT_MAX_JOBS_PER_HOUR=30` and `BACKFILL_AUTO_TRIAGE=10` rows, and the GitHub Actions caveat callout.
- **`.env.example`:** the new defaults.

## 7. Manual steps for you
1. When you set up Render (Step 4), use Neon's **direct** connection string (host without `-pooler`) for `DATABASE_URL`.
2. Optionally remove `ANTHROPIC_API_KEY` from `backend/.env`; nothing uses it.

## 8. Open questions
1. **Draft rule:** change "draft when `needs_reply` OR priority ≥ 3" to "draft only when `needs_reply`"? That would have avoided the `security-alert` draft, and it saves ≈ 450 tokens per affected message.
2. **Fixing invented facts:** add a stricter "no invented facts" line to the draft prompt, and a validator check that rejects bullet lists the incoming message didn't ask for? Or leave prompt tuning to a later step?
