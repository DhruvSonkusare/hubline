# Step 5 report — Slack and Gmail live (local first)

**Status:** complete. Everything is built and all checks pass. Slack and Gmail were both verified live end to end: receive, triage, draft, approve and send into the same conversation.
**Date:** 2026-11-16

**Constraints held:**
- **$0:** Groq free tier, cloudflared quick tunnel (no account), Gmail API, Slack free workspace.
- **GitHub on hold:** local commit only, no remote, nothing deployed.
- **Order (R13):** this step ran locally before Step 4 deploys.

## Summary
Slack and Gmail connect from the dashboard's Connectors page:
- **Slack:** you paste a user token.
- **Gmail:** OAuth with PKCE.

The tokens are stored Fernet-encrypted. They are never returned by the API and never logged.

**Slack:**
- DMs, group DMs and @mentions of you arrive through the Events API over a cloudflared tunnel.
- Groq triaged each one in about a second.
- An approved reply posted into the right DM, and its own echo was deduped.

**Gmail:**
- Polled every 60 s with `history.list`.
- A test email arrived within 63 s, got priority 4 and a draft.
- Bulk mail was set to priority 1 without an LLM call.
- An approved reply to a real email (from your personal address to the demo inbox) went out through Hubline on 2026-11-16 at 20:16 IST and arrived **in the same Gmail conversation**.
- Live testing also found and fixed a wrong-account send (404), and no-reply senders are now blocked (below).

The 5-minute Slack safety-net poll stays under Slack's non-Marketplace limit of about 1 `conversations.history` call per minute, however many DMs are open (explained below).

## Live results (local, Groq `openai/gpt-oss-120b`)
Taken from the local database and backend logs; message text is left out.

| Check | Result |
|---|---|
| Slack Request URL verification (signed `url_verification`) | ✅ Verified in the Slack app (2026-11-10) |
| Slack DM from the second account → stored | ✅ 4 events via `/webhooks/slack`, each stored **≤ 1 s** after Slack's `ts` |
| Slack triage | ✅ "send Q3 deck by 3pm today" → **P4**, needs reply, draft v1. Job took 1.4 s and 1,534 tokens. "yo" → P1, no draft. |
| Slack approve and send | ✅ `reply_sent` 2026-11-10 16:30 IST, `outbound_replies.status=sent`, Slack `ts` stored as `external_id`. You confirmed it landed in the DM. |
| Slack echo of our own reply | ✅ Arrived 0.4 s later as a webhook with `inserted=0`, so it wasn't duplicated or triaged |
| Your own messages in the DM | ✅ Stored as `outbound`, `agent_status=skipped` (thread context only) |
| Gmail connect (OAuth, both scopes) | ✅ 2026-11-10 as the demo account. ⚠️ The 2026-11-16 reconnect signed in to your personal account by mistake. You then revoked Hubline's access there, and I disconnected Gmail and deleted everything from that mailbox (below). **Gmail must be reconnected as the demo account.** |
| Gmail receive | ✅ 11 inbox mails stored, Promotions/Social excluded. The live test email was stored **63 s** after it arrived. The others came from the backfill, or arrived while the backend was off and were picked up by the next poll. |
| Gmail triage | ✅ All 11 `done`. The test email → **P4**, needs reply, draft v1 (2,660 tokens). Six bulk mails → P1 by the prefilter with **0 tokens**. |
| Gmail token refresh | ✅ The access token was refreshed automatically after 6 days offline (2026-11-16 00:52 UTC) |
| Gmail approve and send | ✅ **2026-11-16 20:16:14 IST:** `reply_sent`, approve returned 200, `outbound_replies.status=sent` with Gmail's message id stored. The email (from your personal address, P3, draft v1) moved to **replied**, and one outbound row was added to its thread. You confirmed the reply arrived in the same Gmail conversation. The first attempt that morning had failed with a 404; root cause and fix below. |
| Forged or unsigned Slack webhook through the tunnel | ✅ 401 `webhook_rejected` |
| `/api/*` without the token, through the tunnel | ✅ 401 |

## Fix: Gmail send 404 and no-reply senders (2026-11-16)
**What happened:** approving a Gmail message failed with `_NotFoundError: messages/send`.

**The URL was correct.** The connector builds `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send` with `{raw, threadId}`. A new respx test now pins the exact URL, method, auth header, JSON keys and the decoded RFC 2822 message.

**Root cause:** outgoing requests weren't logged and Google's error body was discarded, so I ran read-only checks against the live Gmail API (no send):
- `profile` returned 200 with scopes `gmail.readonly gmail.send`.
- `threads/{id}` and `messages/{id}` both returned `404 NOT_FOUND "Requested entity was not found."`.
- Gmail had been reconnected that morning **as a different Google account**. 9 of the 11 stored Gmail ids belong to the demo mailbox and don't exist in the connected one.
- Sending with a `threadId` from another mailbox is a 404.

**Fixes:**
- **Account guard:**
  - Each Gmail message now records which mailbox received it (`reply_meta.mailbox`).
  - `send()` refuses before any network call when Gmail is connected as someone else: "this email was received by X, but Gmail is now connected as Y; reconnect Gmail as X".
- **Clear 404:** "Gmail can't find this conversation in <account> (… Requested entity was not found.). It was deleted, or Gmail is connected to a different account…". The internal `_NotFoundError` name is no longer shown.
- **Logs:** every Gmail API error logs `gmail_api_error` with the method, the full URL, the status and Google's own reason. The token is never logged.
- **No-reply and app-notification senders** (`notification@`, `notifications@`, `no-reply@`, `noreply@`, `do-not-reply@`, `mailer-daemon@`, `bounces@`; matched on the part before `@` only, in `app/addresses.py`):
  - **Prefilter:** priority 1, category `automated`, **no draft, no LLM call**, whether or not the mail has bulk headers.
  - **Never sent to:** a new connector hook `reply_blocked_reason()` (default: none) is checked by:
    - the message detail (`can_send=false` plus `send_blocked_reason`; the dashboard shows "Sending is off for this message: …")
    - approve (**409 `reply_blocked`**, with no reply row and no send)
    - Gmail's `send()` itself, as a backstop
  - Gmail blocks a reply when **either** the From sender or the Reply-To address is no-reply.
  - The Slack notification email (From `notification@slack.com`) is blocked because of its sender, even though its Reply-To is Slack's reply-by-email address.

**Existing rows:** the 9 messages from the previous Gmail triage keep their old priority. The new rule applies to new mail and to a manual **Triage**. The failed Slack-notification email now shows "Sending is off". I didn't re-triage it or send from it.

## Cleanup and Reconnect safeguards (2026-11-16, evening)
- **Personal-mailbox data deleted** (your answer 1):
  - **3 messages** from your personal Gmail: the 2 pulled in right after the mistaken reconnect, plus 1 later mail tagged `mailbox=<personal>`.
  - Their **3 agent jobs** (cascade) and **3 threads** left empty.
  - There were no drafts or replies.
  - **Gmail disconnected**, which deleted the personal account's encrypted token and cleared the identity and `self_ids`. You had already revoked access at Google.
  - 9 Gmail messages remain, all from the demo mailbox. 0 rows reference the personal address.
- **Reconnect preselects the connected account** (your answer 2):
  - `OAuthConnectMixin.authorize_url()` takes an optional `login_hint`.
  - The core passes the connected identity when the connector is `connected` or `needs_reauth`. Gmail adds `login_hint=<address>` to Google's URL, so a 7-day re-login opens on the same account.
  - First connect, or a connect after Disconnect, sends no hint, so you choose.
- **The Gmail card shows the address:** "Connected as **x@gmail.com**", or "Last connected as …" when the login expired, next to the status. Under the button: "x@gmail.com is preselected on the sign-in screen" (Reconnect) or "Pick the account Hubline should read on the sign-in screen" (Connect).
- **Disconnect clears the old account's traces:** `self_ids`, `last_error` on the account and on `sync_state`. Before this, the disconnected card still showed "Google access expired…" from the personal account.

## Slack history rate limit: how the poll stays at ≤ 1 call per minute
Slack limits `conversations.history` for non-Marketplace apps to about 1 request per minute, 15 messages per call. Real-time delivery is the Events API, which isn't covered by that limit. The history call is only the safety net. In `app/connectors/slack.py`:

1. **One history call per poll at most.** The poll runs every 5 minutes (`poll_interval_s=300`) and makes at most one `conversations.history` call (`HISTORY_LIMIT=15`).
2. **A 60 s guard that survives restarts.** The time of the last history call (`last_history_at`) is kept in the poll cursor, which lives in `sync_state` in the database. If a poll starts less than `HISTORY_MIN_INTERVAL_S=60` after it, for example after a "Sync now" click or a restart, history is skipped entirely.
3. **Rotation instead of fan-out.** With N open DMs or group DMs, each poll checks the one checked longest ago (the `checked` map in the cursor). Every conversation is still checked about every 5·N minutes, but the total stays at ≤ 1 history call per 5 minutes.
4. **Small windows.** A conversation's first check reads its last 24 h; later checks read only what's newer than the newest message seen.
5. **The other calls aren't covered by the limit:** `conversations.list` (Tier 2), `auth.test`, `users.info` (cached, 1 s timeout) and `chat.postMessage`.

Tests: `test_poll_makes_at_most_one_history_call_and_rotates` (three DMs, three polls, one call each, a different DM each time) and `test_poll_skips_history_within_a_minute_of_the_last_call`.

## Files changed
**Added: backend**
- `app/connectors/slack.py`: Slack connector.
  - Paste-token connect (checks the `xoxp-` prefix, runs `auth.test`, checks scopes) and v0 HMAC webhook verification (±300 s), including `url_verification`.
  - Normalises DMs, MPIMs and @mentions; skips edits, bots and hidden events.
  - The rate-limited poll described above; `chat.postMessage` send; `auth.revoke`.
- `app/connectors/gmail.py`: Gmail connector.
  - OAuth (PKCE, `access_type=offline`, `prompt=consent`); token refresh 60 s before expiry and once on 401; `invalid_grant` → needs re-login.
  - First sync captures the profile `historyId`, then backfills 48 h; polls with `history.list`; a 404 falls back to 1 day.
  - RFC 2822 reply to `Reply-To`, else `From`, with `In-Reply-To`/`References`/`threadId`; revoke.
- `app/connectors/_util/mail.py`: HTML → text, quoted-reply stripping, header parsing, reply recipient/subject, building the reply MIME.
- `app/services/credentials.py`: Fernet encrypt/decrypt; load/store/delete credentials; a `save_credentials` callback for refreshed tokens.
- `app/services/connect.py`:
  - token connect and OAuth start (single-use state + PKCE S256)
  - OAuth finish (atomic state consume)
  - disconnect (best-effort revoke, delete creds, switch off, reset cursor)
- `app/api/routes/oauth.py`: public `GET /oauth/{key}/callback` → redirect to the Connectors page with `?connected=` / `?error=`.
- `app/api/routes/webhooks.py`: public `POST /webhooks/{key}`.
  - 1 MB body cap, signature check (401), 2.5 s parse timeout (503).
  - Ingests only when the connector is active.
  - Kicks the agent, and nudges a poll when asked.
- `migrations/versions/0006_connector_credentials.py`: `connector_credentials`, `oauth_states`.
- `scripts/dev_tunnel.py`: starts a cloudflared quick tunnel, checks `/healthz` through it, and prints the Slack Request URL.

**Added: tests**
- `tests/connectors/test_slack.py` (40), `tests/connectors/test_gmail.py` (16), `tests/connectors/payloads.py` (payload builders and respx mocks)
- `tests/api/test_connect.py` (21), `tests/api/test_webhooks.py` (12)
- `tests/unit/test_credentials.py`, `tests/unit/test_mail.py`

**Added: frontend and docs**
- `frontend/components/ConnectResultToast.tsx`: shows the OAuth result once, in plain words, then cleans the URL.
- `frontend/tests/unit/connectors.test.tsx`: 9 tests for token connect, OAuth connect, needs re-login, not configured, and disconnect.
- `docs/setup/slack.md`, `docs/setup/slack-app-manifest.yaml`, `docs/setup/gmail.md`.
- `reports/step-5-report.md` (this file).

**Modified: backend**
- `app/connectors/base.py`: `TokenConnectMixin`, `OAuthConnectMixin`, `ConnectResult`, `TokenField`, `ConnectError`, optional `revoke()`, `connect_method()`.
- `app/connectors/models.py`: `WebhookResponse.headers`.
- `app/services/connector_runtime.py`: `open_connector()` loads and decrypts credentials and wires `save_credentials`; `SecretStr`-aware configured check.
- `app/workers/scheduler.py`, `app/services/replies.py`: use `open_connector`. An expired or missing credential sets the account to needs re-login. The outbound sender is the connector's own id.
- `app/api/routes/connectors.py`, `app/api/router.py`, `app/schemas/connectors.py`: the credentials, oauth/start and disconnect endpoints; `connect_method` and `token_fields` on `ConnectorOut`.
- `app/models/connector.py`, `app/models/__init__.py`: the `ConnectorCredential` and `OAuthState` models.
- `app/config.py`: `CREDENTIALS_ENCRYPTION_KEY`, `OAUTH_STATE_TTL_S`, `GOOGLE_CLIENT_ID/SECRET`, `SLACK_SIGNING_SECRET`.
- `app/logging.py`: **redacts `code`, `state` and token query parameters in uvicorn's access log** (found during the live test; see Known issues).
- `.env.example`; `pyproject.toml` / `uv.lock` (`cryptography`; dev `respx`).
- `tests/conftest.py`, `tests/connectors/test_contract.py`, `tests/test_migrations.py`, `tests/unit/test_logging.py` (+2 tests).

**Modified: frontend**
- `components/ConnectorCard.tsx`:
  - the connected account; a needs-re-login note
  - Connect/Reconnect via OAuth; a token form (password input, cleared after submit)
  - Sync only when connected; Disconnect with a confirm
- `app/actions/index.ts`, `lib/backend.ts`: `connectWithToken`, `startOAuth`, `disconnectConnector`, each re-checking the session.
- `app/(app)/connectors/page.tsx`: the result toast; `app/globals.css`: `btn-danger`.
- `lib/api-types.ts`, `openapi.json`: regenerated.

**Modified: root**
- `README.md`: status, step table (plus Step 7), the "Live connectors" section, new endpoints and env vars.
- `docs/PLAN.md`, `CLAUDE.md`: **R14 / Decision 13: Step 7 (self-hosting and a public read-only demo) added, planned only.** Hubline stays single-user per deployment.

## Decisions I made myself
- **One generic connect surface** (`/credentials`, `/oauth/start`, `/disconnect`, `/oauth/{key}/callback`, `/webhooks/{key}`) driven by mixins on the connector. Teams (Step 6) can then connect with no core changes.
- **A Slack DM is one Hubline thread** (`{team}:{channel}`), and replies go into the DM itself. Threaded messages and channel mentions use `{team}:{channel}:{thread_ts or ts}`, and replies go into that thread. Replying in a sub-thread of a DM would hide the reply in Slack.
- **The Slack poll is a 5-minute safety net**, not the main path; the Events API is.
- **The webhook stores the message inline** and only kicks the agent in the background. A webhook for a connector that is switched off, disconnected or disallowed is answered 200 and dropped, so Slack stops retrying.
- **Gmail backfill uses the profile `historyId` captured before listing**, so no mail is missed between the list and the first `history.list`.
- **Forwarded mail keeps its content.** "From:" is not treated as a quote marker.
- **Disconnect resets the poll cursor** and clears the identity. Reconnecting as a different account (identity changed) also resets the cursor.
- **Access-log redaction** of `code`, `state` and token query parameters, added after the live test showed the callback URL in uvicorn's log.

## Test results
Run on 2026-11-16, after the last code change.

| Command | Result |
|---|---|
| `cd backend && uv run pytest` | **372 passed, 6 skipped** (84.9 s, real Postgres, no external APIs). Includes the Gmail send fix (exact send URL, 404 message and log, account guard, no-reply block, prefilter rule, address rule, approve 409), `login_hint` and the Disconnect cleanup. |
| `uv run ruff check .` / `uv run ruff format --check .` | clean / clean |
| `uv run mypy` (strict, `app/`) | Success: no issues found in 59 source files |
| `cd frontend && npm run lint` / `npm run typecheck` | clean / clean |
| `npm test` (Vitest) | **36 passed** (4 files) |
| `npm run build` | success |
| `npm run e2e` (Playwright: `next build` + smoke) | **2 passed** (29.4 s, 2026-11-16, after the last frontend change). Its run overwrote the Step 3 screenshots, which I restored with `git checkout`. |

The 6 skips are contract cases that don't apply: the webhook-only path is covered by the Slack signature tests, and connectors without the `sample` capability skip the sample case.

## Deviations from the plan
- **Slack connects by pasting a token, not OAuth.** Slack only allows HTTPS redirect URLs, and a localhost redirect is plain HTTP. Installing from the app page gives the same user token. As you decided, token paste is used in production too for now. **"Connect with Slack" (OAuth) is a later option once there's a stable HTTPS URL.**
- **No Slack event subscriptions in the manifest.** Slack wants a verified Request URL first, so the four user events are added during the first tunnel session ([docs/setup/slack.md](../docs/setup/slack.md)).
- **Slack scopes add `im:read`, `mpim:read`, `channels:read` and `groups:read`** (10 in total), needed for `conversations.list` in the safety-net poll.
- **Migration number:** the plan says `0005` for Step 5's tables. It is `0006`, because Step 3 had already used `0005`.
- **Gmail replies go to `Reply-To` if present, else `From`** (your answer 5). The plan said "sender only".
- **Test fixtures are built by hand from the documented API shapes**, not recorded from the live APIs as plan §10 suggests. Recording would have needed live traffic before the code existed.
- **No `_util/oauth.py` or `_util/http.py`:** PKCE lives in `services/connect.py`, and each connector uses the shared `httpx` client with its own error mapping.
- **Verified locally, not "in production":** Step 4 isn't deployed (R11/R13).
- **No-reply rule widened (your instruction, 2026-11-16):**
  - The plan's prefilter needed bulk headers **and** a no-reply sender. Now a no-reply or notification sender alone is automated (P1, no draft, no LLM call).
  - Replies to no-reply senders or no-reply Reply-To addresses are blocked. This uses the new optional connector hook `reply_blocked_reason()`.

## Known issues
- **Fixed during the live test: the OAuth code appeared in uvicorn's access log.** The line was `GET /oauth/gmail/callback?code=…&state=…`. The code was single-use and already spent, so it couldn't be reused, but it broke the "no secrets in logs" rule. uvicorn's access log now shows `code=[REDACTED]&state=[REDACTED]`. That was checked against the live backend and is covered by 2 new tests. Old local log files from 2026-11-10 to 2026-11-16, outside the repo in the Claude temp folder, still contain those spent codes.
- **The tunnel URL changes every run**, and the tunnel dies when the PC sleeps. While it's down, Slack retries 3 times and then drops the event; the safety-net poll picks the DM up within 5·N minutes.
- **Gmail's Testing-mode refresh token lasts 7 days.** The demo account was connected on 2026-11-16 at 20:08 IST, so the login ends around **2026-11-23, 20:08 IST**. Reconnect preselects the same account.
- **Two mistaken connections to your personal Gmail happened during testing.** Everything they brought in was deleted (6 messages in total, their jobs and threads), and the guards above stop it happening silently again.
- **Threads that started before connecting have partial context** in Hubline.
- **Slack `users.info` names** are cached in memory per process, so a renamed user shows the old name until a restart.

## Manual steps for you
1. Each Slack session: run the tunnel helper, then paste the new URL into **api.slack.com/apps → Hubline Dev → Event Subscriptions → Request URL**. Wait for **Verified ✓**, then **Save Changes**.
2. Before 2026-11-23, 20:08 IST: **Connectors → Gmail → Reconnect Gmail**. `dhruv.sonkusare.dev@gmail.com` is preselected; tick both boxes, then switch it on.

## How to verify
```powershell
docker compose up -d --wait db
cd backend; uv run uvicorn app.main:create_app --factory --port 8000    # LLM_PROVIDER=groq in backend/.env
cd backend; uv run python -m scripts.dev_tunnel                         # second window; prints .../webhooks/slack
cd frontend; npm run dev                                                # third window
```
1. **Slack:** paste the printed URL into Event Subscriptions (→ Verified ✓). DM yourself from the second account, for example "Can you send me the Q3 deck by 3pm?".
   - Expected: it appears in the Inbox within a few seconds with P4–5, a one-line summary and a draft.
   - **Approve and send** → the reply is in the Slack DM, and the message moves to Replied. No duplicate row appears.
2. **Forged webhook:** `curl -X POST https://<tunnel>/webhooks/slack` → `401`.
3. **Gmail:**
   - From another address, email the demo Gmail with a question. Within about 90 s it's in the Inbox with a draft. (The P4 test email from 2026-11-10 can be used too.)
   - Open it → **Approve and send**.
   - Expected: the reply arrives in the **same Gmail conversation** ("Re: …"), addressed to the sender, and the message moves to Replied.
   - In SQL: `select status, external_id from outbound_replies order by created_at desc limit 1;` → `sent` with a Gmail id.
4. **Disconnect:** Connectors → Slack → Disconnect → Yes. The card shows the token form again, and history stays in the Inbox.
5. **Checks:** `cd backend; uv run pytest; uv run ruff check .; uv run mypy` and `cd frontend; npm run lint; npm run typecheck; npm test; npm run build`.

## Open questions
1. **Next step:** Step 4 (deploy) still waits for GitHub. Is Step 6 (Teams, LinkedIn/X on demo data, hardening, demo script) next, or do you want something else first?
2. **Step 7 demo login:** with no password at all (anyone with the URL), or a password published in the README? This matters for rate-limiting viewers and keeping Neon's compute under the limit.
