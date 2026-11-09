# Step 3 report: full API + Next.js dashboard (local)

**Status:** complete.
**Date:** 2026-11-10
**Constraints held:**
- **$0:** the eval used the Groq free tier; everything else ran locally.
- **GitHub on hold:** local commits only, no remote, CI not run.
- **Password login:** GitHub OAuth is not configured.

## Summary
The full human-in-the-loop flow runs locally against the real backend:
1. You log in with a password.
2. You see a prioritised feed, and create demo messages that the agent triages within seconds.
3. You open a message and review the agent's summary, reason, draft and **open questions beside the draft**.
4. You then approve and send (into a demo outbox), copy (for read-only apps), redraft, dismiss or triage.

When the backend is asleep, every page shows a "Waking the backend…" state and recovers by itself.

A Playwright smoke test (login → new demo message → approve) passes, and it saved real 1440×900 PNGs of every page to [`reports/step-3-screenshots/`](step-3-screenshots/).

The draft prompt now writes open questions as items **you** must supply. The Groq eval re-run passed 15/15, and **8/8** open questions were correctly addressed to you.

## Screenshots (Playwright, 1440×900, from the e2e run)
| File | Shows |
|---|---|
| `01-login.png` | Password login |
| `02-feed-empty.png` | Empty feed: "No messages yet" with the New demo message button |
| `03-feed.png` | Feed with P5/P4/P1 (colour + label), DEMO badges, "draft ready", filters, Refresh |
| `04-detail-draft-open-questions.png` | Thread, agent summary and reason, draft v1, the "Before sending, answer:" checklist |
| `05-confirm-dialog.png` | Send confirmation: destination, final text, the **unticked open question inside the dialog**, "Send anyway" |
| `06-detail-replied.png` | After sending: "Replied via Demo data", your reply in the thread |
| `07-connectors.png` | Connectors page: status, enable toggle, last sync, cadence, Sync now, Settings |
| `08-waking.png` | "Waking the backend…" with the elapsed timer (dashboard pointed at a backend that isn't running) |

## Files changed
**Backend (commit `b0686f2`, plus later fixes):**
- `migrations/versions/0005_outbound_replies.py` and `app/models/reply.py`: human-approved replies, with idempotency key and status.
- `app/services/replies.py`: approve and send (at-most-once), human draft versions, dismiss/reopen, and the stale-`sending` reaper (`→ unknown`, never resent).
- `app/api/routes/messages.py`: detail, stats, draft, approve, triage, redraft, dismiss, reopen.
- `app/api/routes/connectors.py` / `app/schemas/connectors.py`: `GET /connectors/{key}/scenarios`.
- `app/services/feed.py`, `app/schemas/messages.py`: the detail and stats queries and models.
- `app/connectors/base.py`, `app/connectors/demo.py`: demo `Capability.SEND` (the demo outbox) and `sample_scenarios()`.
- `app/main.py`: the reaper runs at startup.
- `app/agent/prompts.py` (`draft-v4`) and `app/agent/schemas.py`: open questions are a checklist for the approver; added `is_sender_directed()`.
- `app/agent/fake.py`: the offline model now emits an owner-directed open question when a message asks for something.
- `scripts/agent_eval.py` (open-question check, which fails the run on a sender-directed question) and `scripts/export_openapi.py`.
- Tests: `tests/api/test_message_actions.py` (12 new) and `test_rules.py` (open-question checker), plus updates for the new table and capability.

**Frontend (new, `frontend/`):**
- **Setup:** `create-next-app` 16.4.5. `package.json` with `engines.node: "26.x"`, `.nvmrc` (`26`), the scripts, and pinned `next-auth@5.0.0-beta.32`, Vitest 5.1, Playwright 1.65, `openapi-typescript`.
- **Next config:** `next.config.ts` with Cache Components off.
- **Auth:**
  - `auth.ts`: providers from env; Credentials with a constant-time check, failure delay and rate limit; GitHub behind the env switch; the actor sent to the backend.
  - `lib/password.ts`, `lib/auth-config.ts`, `proxy.ts` (route guard), `app/api/auth/[...nextauth]/route.ts`, `types/next-auth.d.ts`.
- **Backend access:**
  - `lib/backend.ts`: server-only; bearer token and `X-Actor`; a 90 s data timeout and 3 s health timeout; `BackendUnavailable` drives the waking state.
  - `lib/api-types.ts` is generated from `openapi.json`. `lib/format.ts` holds the priority styles, filters ↔ URL, and time formatting.
- **Server actions** (the only mutation path; each re-checks the session): `app/actions/index.ts`.
- **Pages:** `app/login`, `app/(app)/layout.tsx` (sidebar + backend gate), `app/(app)/inbox` (feed and empty state), `app/(app)/inbox/[id]` (detail), `app/(app)/connectors`, `app/api/backend-status`.
- **Components:**
  - `WakingScreen`, `AutoRefresh` (15 s / 3 s / idle stop + manual Refresh)
  - `FeedToolbar`, `MessageRow`, `NewDemoMessageButton`, `TriageButton`
  - `DraftPanel`, `OpenQuestions`, `ConfirmSendDialog`, `RedraftDialog`
  - `ConnectorCard`, `Badges`, `Toast`, `LoginForm`
- **Styling:** `app/globals.css`, one indigo accent with shared `@utility` classes.
- **Tests:**
  - `tests/unit/*`: 25 Vitest tests.
  - `tests/e2e/smoke.spec.ts` with `playwright.config.ts`, which starts the backend on `hubline_e2e` plus two dashboard instances.
- **Docs and config:** `frontend/.env.example`, `frontend/README.md`, `frontend/.gitignore` (`!.env.example`).
- **`frontend/.env.local`:** created, git-ignored, not committed.

**Repo:**
- `.github/workflows/ci.yml` gains a frontend job; not run while GitHub is on hold.
- `docker/postgres-init` creates `hubline_e2e`; `.gitignore` ignores Playwright output.
- README (dashboard section, tests, API), CLAUDE.md (frontend commands and rules), this report, and the screenshots.

## Decisions I made myself
1. **Node pin `26.x`.** Vercel's docs list **26.x (default), 24.x and 22.x**, and the local install is v26.13.0, so the pin matches both the build and production.
2. **Cache Components and partial prefetching are off.** `create-next-app` enables them, but every page here is per-request, behind a login and live. A prerendered shell would gain nothing and would force a `<Suspense>` boundary around every cookie read. The pages use plain dynamic rendering, and the build shows every app route as `ƒ` (on demand).
3. **Auth.js v5 is still a beta** (`5.0.0-beta.32`, the `beta` dist-tag; it supports Next 16). It is pinned exactly. Better Auth remains the documented fallback.
4. **Password hardening:**
   - `DASHBOARD_PASSWORD` must be at least 16 characters, or the server refuses to start.
   - Both values are SHA-256-hashed before `timingSafeEqual`, so length doesn't leak through timing.
   - A failed login waits 1 s, and attempts are limited to 5 per 15 minutes per IP (in memory).
   - Auth.js's expected `CredentialsSignin` log is filtered out, so real errors stand out.
5. **The waking state** has two triggers. The layout checks `/healthz` (3 s timeout; never touches the DB) before rendering. Each page also catches `BackendUnavailable`, raised on a network error, a timeout, or Render's 502/503/504 without our error body, and renders the same screen. A client poller checks `/api/backend-status` every 3 s and refreshes when the backend is up. The poller is keyed only on the Retry counter, after a test showed it could start twice.
6. **Paging:** "Load more →" moves to the next keyset page instead of appending rows, which keeps the server components simple. "← Newest" goes back.
7. **Open questions:**
   - Checkbox state is local to the page and never sent anywhere.
   - Unticked ones are listed **inside the existing send confirmation**, as you asked, and the button then reads "Send anyway".
   - The idempotency key is generated when the dialog opens.
8. **Failed send:**
   - The approve flow stores the edited text as a human draft version *before* calling the app.
   - The panel shows the error, and the textarea keeps your text. After a reload, the server-side `failed` reply is shown too.
   - A deliberate new click (a new key) may retry.
   - An `unknown` result (the app stopped mid-send) blocks further sends, with a "check the app first" banner.
9. **Messages with no draft** (`needs_reply=false`) show "No reply needed (the agent's assessment)" with a **Write a draft** action, which runs a redraft.
10. **The e2e test uses its own DB (`hubline_e2e`)**, reset on every run, plus a second dashboard instance pointed at a closed port for the waking screenshot. Your dev data is never touched.
11. **The fake model emits open questions**, so the offline demo and the e2e screenshots show the checklist without any LLM key.
12. **Endpoints and connector API:**
    - New: `GET /api/connectors/{key}/scenarios` and `Connector.sample_scenarios()`. The dropdown needs the scenario list, and it stays connector-agnostic.
    - `POST /triage` re-runs triage for `done` messages too (shown as "Re-triage" in the agent panel).

## Test results
```
backend   uv run ruff check . / ruff format --check . → clean (98 files)
          uv run mypy                                → no issues in 50 source files (strict)
          uv run alembic check                       → no new operations (0005 round-trips locally)
          uv run pytest                              → 225 passed, 1 skipped
frontend  npm run lint                               → clean
          npm run typecheck                          → clean
          npm test (Vitest)                          → 25 passed
          npm run build                              → compiled; app routes dynamic (ƒ), proxy active
          npm run e2e (Playwright, chromium)         → 2 passed
          npm audit --omit=dev                       → 0 vulnerabilities in production dependencies
```
- **Skipped test:** the read-only contract check no longer applies to the demo connector, which can now send. Read-only behaviour is covered by `test_read_only_source_cannot_be_approved`.
- **New backend tests:**
  - detail with thread, draft and open questions
  - stats
  - human draft versions (no-op when unchanged)
  - **approve sends once**: same key → same reply; a second key → 409 already replied
  - **3 concurrent approvals with different keys → exactly 1 send**
  - read-only → 409 and `can_send=false`
  - **failed send** → 502, message stays open, edited draft kept, reply `failed`, a deliberate retry allowed
  - Triage button on a skipped message (priority 20, 409 while active)
  - redraft with instructions
  - dismiss/reopen, including edits blocked while dismissed
  - scenarios
  - stale `sending` → `unknown`, which blocks further sends
  - the open-question direction checker (9 cases)
- **Vitest:**
  - constant-time compare: `timingSafeEqual` always on 32-byte digests
  - password length guard and limiter
  - provider selection from env (password / github / both, missing vars, allow-list)
  - filters ↔ URL, P-labels
  - DraftPanel: Approve vs Copy, open questions beside the draft, unticked questions inside the confirmation, failed send keeps the text, no-draft state
  - one idempotency key per dialog opening
  - AutoRefresh: 15 s, paused after 10 idle minutes, manual refresh
  - WakingScreen: polls, refreshes, stops
- **Playwright:** a wrong password is rejected → login → empty feed → 3 demo messages (scenario menu) triaged on demand → detail with open question → edit → Approve → dialog lists the unticked question → Send anyway → "Replied", reply in the thread → Connectors. Separately, the waking state against a stopped backend.

### Groq eval re-run (`--provider groq --max-drafts 15`, $0)
**15/15 in band.** Open-question check: **8/8 written as items for the approver** (0 addressed to the sender).

| id | want | got | triage in/out/rsn | draft in/out/rsn | total |
|---|---|---|---|---|---|
| prod-outage | 5 | 5 | 563/114/36 | 654/137/34 | 1468 |
| manager-today | 4-5 | 4 | 530/135/47 | 624/90/14 | 1379 |
| client-friday | 3-4 | 4 | 552/124/47 | 641/103/16 | 1420 |
| pr-review-week | 2-3 | 3 | 527/120/39 | 612/101/32 | 1360 |
| standup-move | 3-4 | 4 | 529/125/42 | 621/101/31 | 1376 |
| recruiter | 1-3 | 3 | 1082/283/120 *(1 retry)* | 629/108/18 | 2102 |
| friend-meetup | 2-3 | 3 | 524/160/78 | 614/95/12 | 1393 |
| newsletter | 1 | 1 | rules | – | 0 |
| marketing / invoice / ci / security / fyi / thanks / injection | ✔ | 1 / 2 / 3 / 5 / 2 / 2 / 1 | ≈ 520–545 in | – | 613–716 |

Open questions as written by the model:
- "Decision on whether to roll back the checkout API (needed for the reply)"
- "Current Hubline launch blockers (needed for the reply)"
- "The exact webhook retry count and time window (needed for the reply)"
- "Your availability to review the PR this week (needed to confirm timing)"
- "Whether the standup can be moved to 10:30 tomorrow (needed for the reply)"
- "Your availability for a quick chat next week (needed to propose times)"
- "Whether Dhruv can attend the PyCon meetup on Saturday"
- "Whether Dhruv can join the dinner after the meetup"

Totals: 22 calls; 12,400 in / 2,712 out (976 reasoning) = **15,112 tokens**.
- Per message: mean **1,079**, max **2,102**.
- The longer draft prompt (v4) adds about 110 input tokens per draft.
- At this rate the 180K daily budget still covers ≈ 167 messages/day (≈ 86 in the worst case).

## Deviations from the plan
- **Cache Components off** (decision 2), and **"Load more" pages rather than appends** (decision 6).
- **Added beyond the plan:** `GET /connectors/{key}/scenarios`, triage for `done` messages ("Re-triage"), and a "Write a draft" action when no draft exists.
- **Playwright** runs a production build (`next build && playwright test`) against three local servers. There's no separate CI e2e job, because GitHub is on hold.
- **`@types/node` was raised to `^26`**, because Vitest 5 needs ≥ 22 and we run Node 26.

## Known issues
- **`npm audit`: 8 moderate advisories** in **dev-only** tooling (`minimatch` 7–11, pulled in by lint and test tooling). Production dependencies have **0**. I didn't run `npm audit fix --force`, which would make breaking downgrades; I'll recheck after the next dependency bump.
- **Auth.js v5 is a beta release**; it's pinned exactly.
- **The login rate limit is in memory** and so best effort on serverless; the 24-character random password is the real protection.
- **The fake model's summaries are long** (they reuse the first sentence), so the feed rows in the screenshots are wordier than with Groq. Real summaries are one short line (see the eval).
- **The Connectors page shows only the demo connector:** the only one registered so far. Slack and Gmail cards appear in Step 5.
- **Node PATH in this session:** the Node install updated the machine PATH, but long-running shells opened earlier won't see it until reopened.

## Manual steps for you
1. **Dashboard password:** it was shown once when `frontend/.env.local` was created. The file is git-ignored and holds `AUTH_PROVIDERS=password`, `DASHBOARD_PASSWORD`, `AUTH_SECRET`, `AUTH_TRUST_HOST`, `BACKEND_URL` and `BACKEND_API_TOKEN` (copied from `backend/.env`). If you didn't save the password, generate a new one and replace the `DASHBOARD_PASSWORD` line.
2. **Open a new terminal** so `node` and `npm` are on PATH. Then:
   ```
   docker compose up -d --wait db
   cd backend  && uv run alembic upgrade head && uv run uvicorn app.main:create_app --factory --port 8000
   cd frontend && npm install && npm run dev        # http://localhost:3000
   ```
3. Nothing on GitHub, Vercel or Render for this step.

## How to verify
1. With the servers running, open http://localhost:3000. You should be redirected to `/login`.
2. A wrong password shows "Incorrect password." The real one shows the inbox.
3. **New demo message ▾** → `client-question`. It appears as "Agent queued/working…", and after a few seconds becomes P3/P4 with "draft ready".
4. Open it: the thread, summary and why, the draft (v1), and "Before sending, answer:" with a checkbox.
5. Edit the text → **Approve and send**. The dialog names the destination and lists the unticked question → **Send anyway**. You should see the toast "Sent via Demo data", the "Replied" banner, and your reply in the thread.
6. **Connectors** → toggle Demo on, then **Sync now**. Six demo messages arrive and are triaged.
7. Stop uvicorn and reload a page. "Waking the backend…" appears. Start uvicorn again, and the page continues by itself.
8. Automated: `cd frontend && npm run e2e`. Expect 2 passed, with the screenshots rewritten in `reports/step-3-screenshots/`.

## Open questions
1. **Feed summaries:** should the feed row show the agent summary (current) or the subject first? With real Groq summaries the summary reads well. With the fake model it's long.
2. **Demo outbox visibility:** add a small "Demo outbox" view listing the replies sent there, or is the thread view enough for presenting?
3. **Step 4 timing:** deploying needs GitHub (Render and Vercel deploy from the repo). Step 4 is ready to start once the account is back. Should I prepare a `render.yaml` and the deploy docs before then?
