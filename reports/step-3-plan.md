# Step 3 plan: full API + Next.js dashboard (local)

**Status:** PLAN ONLY, for your review. Nothing in this file is built yet.
**Date:** 2026-11-09
**Constraints:**
- **$0** (R6).
- **GitHub on hold** (R11): local commits only. CI isn't run, and there's no GitHub OAuth.
- **Password login** (R12).

## 1. Goal and "done when"
**Goal:** run the complete human-in-the-loop flow locally on demo data. You log in, see a prioritised feed, open a message, review the agent's summary, draft and open questions, then approve and send, copy, redraft, dismiss or triage. All of it runs against the real backend.

**Done when:**
1. **Login:** `http://localhost:3000` redirects to `/login`. The correct `DASHBOARD_PASSWORD` logs in; a wrong one is rejected (1 s delay, rate-limited). The session is an `httpOnly` cookie, and the browser never sees `BACKEND_API_TOKEN`.
2. **Waking-up state:** with the backend stopped or slow, the dashboard shows "Waking the backend…" and recovers on its own when the backend answers. No error page.
3. **Feed:**
   - Inbound messages appear, filterable by app, priority and status, and sortable by newest or priority.
   - Rows show priority and agent status badges, and a **DEMO** badge where it applies.
   - The feed auto-refreshes every 15 s while the tab is visible.
4. **New demo message** (feed toolbar): creates a demo message, optionally for a chosen scenario. It shows as "Agent working…" and becomes triaged within seconds, without a page reload.
5. **Triage button:** on `skipped` backfill messages and `failed` messages it queues the agent at dashboard priority. The badge goes to pending/running, then done.
6. **Message detail:**
   - Thread, summary, priority reason, and an editable draft showing its version.
   - **Open questions sit next to the draft.**
   - Editing saves a new human version.
   - **Approve and send**, for sources that can send (demo writes to a demo outbox): a confirm dialog names the destination, and a double click still sends once (idempotency key). The message becomes `replied`, and the outbound reply appears in the thread.
   - **Copy draft**, for read-only sources: copies to the clipboard and shows "Paste this in <App>; <App> is read-only".
   - **Redraft** with optional instructions produces a new version.
   - **Dismiss** and **Reopen** work.
7. **Connectors page:** each connector shows status, last sync and errors. You can enable/disable it, **Sync now**, and edit demo settings (cadence, cap).
8. **Checks green:**
   - backend: `pytest`, `ruff`, `mypy`, `alembic check`
   - frontend: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`
   - README updated, and `reports/step-3-report.md` written

## 2. Backend work (FastAPI)
### Migration `0005_outbound_replies`
Migrations continue sequentially, so this is `0005`, not the plan's original `0003`. The table follows PLAN §3: `id`, `message_id` (the message being replied to), `connector_account_id`, `external_thread_id`, `body`, `status` (`sending` / `sent` / `failed` / `unknown`), `idempotency_key` (UNIQUE), `external_id`, `error`, `approved_by`, `created_at`, `sent_at`, plus an index on `message_id`.

### Demo connector can "send"
The demo connector gains `Capability.SEND`. Its `send()` records nothing externally: it returns a synthetic `external_id`, and the reply lives in `outbound_replies` plus an outbound `messages` row (the **demo outbox**), so the full approve flow can be shown at $0.

A test-only read-only fake connector exercises the "Copy draft" path. The real read-only sources (LinkedIn, X) arrive in Step 6.

### New and changed endpoints (all `/api/*`, bearer token, `X-Actor`)
| Method | Path | Purpose / notes |
|---|---|---|
| GET | `/api/messages/{id}` | Detail. It includes:<ul><li>everything in the summary row, plus `body`, `permalink`, `priority_reason` and agent info (`status`, `error`, `model`, `prompt_version`, `triaged_at`)</li><li>`thread`: the last 20 messages, including outbound ones</li><li>the current `draft` (`version`, `body`, `author`, `instructions`, **`open_questions`**) and `draft_versions`</li><li>`replies` and `can_send`</li></ul> |
| GET | `/api/messages/stats` | Counts per source, status and priority, plus `agent_pending`, for the sidebar badges |
| PUT | `/api/messages/{id}/draft` | `{body}` → saves a new `human` version (locks the message row, like the worker) |
| POST | `/api/messages/{id}/approve` | `{body, idempotency_key}`. At-most-once send:<ul><li>Inserts an `outbound_replies` row as `sending` (a repeated key returns the existing row), calls `connector.send()`, then marks it `sent`, adds an outbound message and sets the message to `replied`.</li><li>A failure marks the reply `failed` and returns 502.</li><li>409 for read-only sources or already replied; 503 if the connector is unavailable.</li></ul> |
| POST | `/api/messages/{id}/redraft` | `{instructions?}` → job at **priority 20**, 202. 409 if a job is already active. |
| POST | `/api/messages/{id}/triage` | **The Triage button** (for skipped backfill, failed, or re-triage) → job at **priority 20**, 202. 409 if a job is already active. |
| POST | `/api/messages/{id}/dismiss` / `/reopen` | Status changes |

Every mutation **kicks the work runner** (R9). With the loops off, the job is processed in the background straight away.

**Crash recovery:** `sending` replies older than 2 minutes become `unknown` at startup. The UI then shows "may or may not have been sent; check the app", and nothing is auto-retried.

**Tests (pytest):** each endpoint's happy path and error codes; approve idempotency (concurrent double approve sends once); read-only approval rejected; human draft versioning racing the agent; the triage endpoint on skipped and failed messages; the stale-`sending` reaper.

## 3. Frontend (Next.js, `frontend/`)
**Stack:** Next.js (App Router, current stable), React, TypeScript (strict), Tailwind CSS v4, Auth.js v5, and `openapi-typescript` to generate types from the backend OpenAPI.
- Tests: Vitest + Testing Library. Playwright is optional (question Q1).
- npm on Node 26.13.0, which is installed.
- I'll ask before adding anything beyond these.

```
frontend/
├── package.json  tsconfig.json  next.config.ts  postcss.config.mjs  eslint.config.mjs
├── vitest.config.ts  .env.example                     (.env.local is git-ignored)
├── auth.ts               # providers from AUTH_PROVIDERS: password (now) | github (later), JWT cookie
├── lib/password.ts       # sha256 + timingSafeEqual; ≥16-char check; failed-attempt limiter
├── proxy.ts              # route guard -> /login
├── app/
│   ├── layout.tsx  globals.css  page.tsx (-> /inbox)
│   ├── login/page.tsx                    # password form (server action)
│   ├── (app)/layout.tsx                  # sidebar + <BackendGate> (waking-up state)
│   ├── (app)/inbox/page.tsx              # feed
│   ├── (app)/inbox/[id]/page.tsx         # detail
│   ├── (app)/connectors/page.tsx
│   ├── api/auth/[...nextauth]/route.ts
│   ├── api/backend-status/route.ts       # server-side ping of backend /healthz (for the gate)
│   └── actions/ messages.ts connectors.ts   # server actions = the only mutation path
├── components/
│   ├── BackendGate.tsx  WakingScreen.tsx  AutoRefresh.tsx
│   ├── FeedToolbar.tsx (filters, sort, New demo message)  FeedList.tsx  MessageRow.tsx
│   ├── PriorityBadge.tsx  AgentStatusBadge.tsx  SourceIcon.tsx  DemoBadge.tsx
│   ├── ThreadView.tsx  AgentPanel.tsx  DraftEditor.tsx  OpenQuestions.tsx
│   ├── ActionBar.tsx (Approve and send | Copy draft | Redraft | Dismiss | Triage)
│   ├── ConfirmSendDialog.tsx  RedraftDialog.tsx  Toast.tsx
│   └── ConnectorCard.tsx
├── lib/ backend.ts (import "server-only"; bearer + X-Actor; timeouts)  api-types.ts  format.ts
└── tests/ unit/*.test.ts(x)
```

**Secrets stay out of the browser (PLAN §7):**
- `lib/backend.ts` is server-only; the build fails if a client component imports it.
- Reads happen in server components, and mutations in server actions.
- No `NEXT_PUBLIC_*` secrets.
- The backend's CORS stays closed.

### Login (R12)
- `AUTH_PROVIDERS=password` (the default): an Auth.js `Credentials` provider whose `authorize()` calls `verifyPassword(input)`. That function compares `sha256(input)` with `sha256(DASHBOARD_PASSWORD)` using `crypto.timingSafeEqual`, so the result takes the same time regardless of the password's length or content.
- On failure: about 1 s delay, and 5 attempts per 15 minutes per IP (in memory, best effort).
- `DASHBOARD_PASSWORD` must be at least 16 characters, or the server refuses to start with a clear error.
- **Session:** JWT strategy, `httpOnly` cookie, `SameSite=Lax`, `Secure` on HTTPS, 7 days. The actor sent to the backend is `password:owner`.
- **GitHub later:** set `AUTH_PROVIDERS=github` (or `password,github`), plus `AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET` and `ALLOWED_GITHUB_LOGINS`. `auth.ts` already contains both providers behind the env switch, and unit tests cover both selections. **No code change is needed.**

### Waking-up state
On Render Free the backend sleeps after 15 idle minutes, and the first request takes about a minute.
- `(app)/layout.tsx` renders `<BackendGate>`. It first checks backend `/healthz` server-side with a **3 s timeout**. `/healthz` never touches the DB, so it doesn't wake Neon by itself.
- **If healthy:** render the page as usual.
- **If slow or unreachable:** render `<WakingScreen>` immediately, with this text: *"Waking the backend… The free server sleeps when idle and takes about a minute to start."* It shows an elapsed-seconds counter.
  - A client poller calls `/api/backend-status` (a Next route that pings `/healthz` server-side) every 3 s.
  - On the first 200 it runs `router.refresh()`.
  - The first ping is the request that starts Render's spin-up.
- **After 120 s:** show "Still waking… check the Render dashboard", with a Retry button.
- **Data calls:** `lib/backend.ts` uses a 90 s timeout. If a data call fails after the gate passed (for example a restart mid-session), the page's error boundary shows the same waking screen rather than a stack trace.
- Locally you can test this by stopping uvicorn, or with `BACKEND_URL` pointing at a port with a delayed proxy.

### Feed behaviour
- Filters are URL `searchParams` (`source`, `priority`, `min_priority`, `status`, `agent_status`, `sort`), so they're shareable and survive a refresh.
- Keyset paging uses a "Load more" button.
- **`AutoRefresh`:** `router.refresh()` every 15 s **only while the tab is visible**, and it stops after 10 minutes without user input. A forgotten open tab must not keep Neon awake (R10); the "1 h/day dashboard" figure in the CU estimate depends on this.
- While any visible message has `agent_status` `pending` or `running`, the refresh interval is 3 s, for up to 2 minutes.

### New demo message button
- **Placement:** the feed toolbar, visible only when the `demo` connector exists and is connected. It works even while the demo drip is disabled.
- **Interaction:** a split button. The main action is a random scenario. The dropdown lists the scenario names, taken from the backend's 422 hint, or better from a small `GET /api/connectors/demo/scenarios` added in this step.
- **Action:** a server action calls `POST /api/connectors/demo/sample`, then a toast says "Demo message created — agent working…". It switches to fast refresh until the message is `done`.

### Triage button
- Shown on rows and in the detail view when `agent_status` is `skipped` (backfill beyond the newest 10, with the note as a tooltip) or `failed` (labelled **Retry agent**).
- In the detail view's agent menu it also appears as "Re-triage" for `done` messages.
- It calls `POST /api/messages/{id}/triage` (priority 20), then shows "Queued" and refreshes quickly.
- 409 (job already active) → toast "Already in progress".

### Draft panel, open questions, and actions
- **Draft editor:** a `textarea` showing `vN · agent|you · model`. Edits are saved as a human version when you click **Save** or Approve. "Unsaved changes" is shown when you have them.
- **Open questions:** a panel **beside the draft** (below it on narrow screens), titled **"Before sending, answer:"**.
  - Each item has a checkbox ("handled").
  - If any are unchecked, Approve asks: "There are open questions — send anyway?"
  - Open questions are never sent, only shown.
- **Approve and send** (only when `can_send`): a confirm dialog shows the destination ("Reply in Demo outbox · thread *URGENT: payments API…*") and the final text. The idempotency key is generated when the dialog opens, so a double submit sends once.
- **Copy draft** (when `can_send` is false: read-only sources, LinkedIn/X in Step 6, the test fake now) copies to the clipboard, with the note "*LinkedIn is read-only — paste this reply in LinkedIn.*"
- **Redraft:** a dialog with optional instructions ("shorter", "more formal", …), at priority 20. The panel shows "Agent rewriting…" until the new version lands.
- **Dismiss / Reopen.**
- **Messages with no draft** (`needs_reply=false`) show "No reply needed (agent)", with a **Write a draft** action that runs a Redraft.

## 4. Page sketches
**Login**
```
┌──────────────────────────────────────────────┐
│                  Hubline                     │
│        One inbox. You approve every reply.   │
│                                              │
│   Password  [••••••••••••••••••••]           │
│             [        Log in        ]         │
│   (wrong password → "Incorrect password")    │
└──────────────────────────────────────────────┘
```

**Waking the backend** (any app page)
```
┌──────────────────────────────────────────────┐
│  ◐  Waking the backend…                 0:23 │
│  The free server sleeps when idle and takes  │
│  about a minute to start. This page will     │
│  continue on its own.                        │
│  (after 2 min: "Still waking…"  [Retry])     │
└──────────────────────────────────────────────┘
```

**Inbox (feed)**
```
┌ Hubline ───────────┬─────────────────────────────────────────────────────────────┐
│ Inbox          12  │ [App ▾] [Priority ▾] [Open ▾] [Sort: Priority ▾]            │
│  Demo   DEMO   12  │                         [+ New demo message ▾]  ⟳ 15s       │
│  Slack          –  ├─────────────────────────────────────────────────────────────┤
│  Gmail          –  │ P5 ● DEMO Priya Raman · Email      URGENT: payments 500s  2m│
│ Connectors         │    Priya: rollback go/no-go before 4pm call        [draft]  │
│                    │ P4 ● DEMO Neha Kapoor · Slack DM                         9m │
│ ───────            │    Neha: 3 blockers for Hubline launch before 1:1  [draft]  │
│ Agent: 1 working   │ ◌  DEMO Arjun Mehta · #backend       Agent working…      1m │
│ Logout             │ –  DEMO CI Bot · Email   skipped (backfill)  [Triage]   3d  │
│                    │ P1 ● DEMO Python Weekly · Email   newsletter (rules)    1h  │
│                    │                        [ Load more ]                        │
└────────────────────┴─────────────────────────────────────────────────────────────┘
```

**Message detail**
```
┌ ← Inbox ─────────────────────────────────────────────────────────────────────────┐
│ DEMO · Email · Priya Raman <priya@acme.example>          P5  action_required     │
│ "URGENT: payments API 500s in prod"                      received 14:05 IST      │
├──────────────────────────────────────┬───────────────────────────────────────────┤
│ THREAD                               │ AGENT                                     │
│ 14:05 Priya: the /payments/charge…   │ Summary: Priya: rollback go/no-go before  │
│ 14:40 Priya: error rate now 12%…     │          the 4pm leadership call          │
│ 14:52 You: (sent replies appear here)│ Why P5: outage + hard deadline today      │
│                                      │ gpt-oss-120b · triage-v3/draft-v3  [⋯ Re-triage]│
│                                      ├───────────────────────────────────────────┤
│                                      │ DRAFT  v2 · agent       ┌ Before sending, ┐│
│                                      │ ┌───────────────────┐   │ answer:         ││
│                                      │ │Hi Priya, I'm on it│   │ ☐ Roll back or  ││
│                                      │ │and will confirm…  │   │   hotfix?       ││
│                                      │ └───────────────────┘   │ ☐ Which metrics ││
│                                      │ [Save]  unsaved         │   to share?     ││
│                                      │                         └─────────────────┘│
│                                      │ [Approve and send] [Redraft…] [Dismiss]   │
│                                      │  read-only source → [Copy draft] instead  │
└──────────────────────────────────────┴───────────────────────────────────────────┘
```

**Connectors**
```
┌ Connectors ──────────────────────────────────────────────────────────────────────┐
│ ┌ Demo data  DEMO ─────────────────┐  ┌ Slack ─────────────────────────────────┐ │
│ │ ● connected   [Enabled ◉]        │  │ not configured (Step 5)                │ │
│ │ last sync 14:52 · next 15:12     │  │ missing: SLACK_CLIENT_ID, …            │ │
│ │ every 20 min · cap 100           │  └────────────────────────────────────────┘ │
│ │ [Sync now] [Settings…]           │  ┌ Gmail ─────────────────────────────────┐ │
│ └──────────────────────────────────┘  │ not configured (Step 5)                │ │
│                                        └────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Tests (frontend)
- **Vitest unit tests:**
  - `verifyPassword`: correct and wrong passwords, and different lengths, all go through `timingSafeEqual` with no early return.
  - The minimum-length guard and the failed-attempt limiter.
  - Provider selection from `AUTH_PROVIDERS`.
  - Filter ↔ URL mapping and formatters.
- **Component tests:**
  - `PriorityBadge`, `AgentStatusBadge`, `DemoBadge`.
  - `ActionBar`: Approve vs Copy depending on `can_send`; Triage only for skipped/failed.
  - `OpenQuestions`: the confirm dialog when items are unchecked.
  - `WakingScreen`: polling, then refresh.
  - `DraftEditor`: unsaved state.
- **`npm run build`** proves no server-only module leaks into client bundles.
- **Backend:** pytest for every new endpoint (section 2). The end-to-end test grows into sample → triage → approve → replied, on the fake LLM.
- **E2E (Playwright):** **optional**; see Q1. Without it, the full flow is verified manually by a scripted checklist in the report, against backend + fake LLM.
- **CI:** `.github/workflows/ci.yml` gains a frontend job, but it can't run until GitHub is back (R11). The same commands run locally.

## 6. Build order inside Step 3
1. Backend: migration `0005` and the demo `send()`, the detail/stats/draft/approve/redraft/triage/dismiss/reopen endpoints, the stale-`sending` reaper, and tests. Then export OpenAPI.
2. Frontend scaffold: Next.js + Tailwind + strict TS + ESLint + Vitest, plus `lib/backend.ts` and `api-types.ts`.
3. Auth: `auth.ts`, `lib/password.ts`, `proxy.ts`, the login page and tests.
4. The `BackendGate` waking-up state.
5. Feed: filters, auto-refresh, New demo message, Triage.
6. Detail: thread, agent panel, draft editor, open questions, Approve, Copy, Redraft, Dismiss/Reopen.
7. Connectors page.
8. README, a manual walkthrough with screenshots-as-text in the report, `reports/step-3-report.md`, and a local commit.

## 7. Manual steps for you (before or at the start of Step 3)
1. Create `frontend/.env.local`; I'll generate the values with `scripts/gen_keys.py` and print them for you to paste.
   - `DASHBOARD_PASSWORD`: at least 16 random characters.
   - `AUTH_SECRET`
   - `AUTH_PROVIDERS=password`
   - `BACKEND_URL=http://localhost:8000`
   - `BACKEND_API_TOKEN`: the same value as `BACKEND_API_TOKENS` in `backend/.env`
2. Nothing on GitHub, Vercel or Render for this step.

## 8. Open questions
1. **Playwright:** OK to add it as a dev dependency? It downloads a Chromium build (a few hundred MB, $0) for one smoke test (login → new demo message → approve). Or should I rely on Vitest plus a manual checklist until Step 4?
2. **Feed refresh:** is 15 s while visible, stopping after 10 minutes idle, acceptable? A shorter interval keeps Neon awake longer while the dashboard is open.
3. **Open questions as checkboxes:** keep the "send anyway?" confirm when unchecked questions remain, or just show them without blocking?
4. **Demo send:** OK that the demo connector "sends" into a local **demo outbox** (stored, clearly labelled, never leaving the app), so Approve and send can be presented end to end before Slack/Gmail exist?
