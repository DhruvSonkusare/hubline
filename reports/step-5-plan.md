# Step 5 plan — Slack + Gmail live, LOCAL first

**Status:** plan only, not built. Waiting for your approval and answers to the open questions at the end.
**Date:** 2026-11-10
**Why local first:** deploying needs GitHub, which is on hold. Per revision R13, Step 5 is built and verified on this PC before Step 4 deploys. Everything stays $0:
- cloudflared quick tunnel: free, no account
- Slack free workspace
- Gmail API: free quota
- Groq free tier

## 1. Goal and "done when"

**Goal:**
- A real Slack DM (or @mention) in "Hubline Demo" shows up in the local dashboard **within about 10 s**, with priority, summary and draft.
- An email to the demo Gmail shows up **within about 90 s**.
- Approving a reply posts it **into the same Slack conversation** or **the same Gmail thread** ("Re: …"), as you.

**Done when (all verified locally):**
1. Slack connects on the Connectors page via **pasted user token**, and shows `connected` with `Hubline Demo / <you>`.
2. Gmail connects via **Connect Gmail**. The Google consent screen returns to `http://localhost:8000/oauth/gmail/callback`, then to the dashboard, which shows `connected` with the demo address.
3. A DM from your second Slack account appears in under 10 s. It is triaged by Groq (free), and the draft is shown.
4. Approving posts into that DM, as you. Slack's echo of that reply is **not** duplicated and **not** triaged.
5. An email from another address appears within 90 s. Approving sends a reply in the same Gmail conversation, to the sender only.
6. A forged Slack request (bad signature or stale timestamp) gets **401**, and nothing is stored.
7. An expired Gmail access token refreshes by itself. A revoked one (myaccount.google.com → Security → Third-party connections) shows **needs_reauth** with a Reconnect button.
8. No token appears in any API response, log line or the DB in plaintext. A test checks this.
9. All checks are green:
   - backend: pytest, ruff, format and mypy
   - frontend: lint, typecheck, Vitest, build and e2e
   - every Slack and Gmail HTTP call is mocked with respx and recorded fixtures
   - no real external API is called in tests

## 2. How the local setup fits together

```
Slack ──HTTPS event──► https://<random>.trycloudflare.com ──cloudflared──► localhost:8000/webhooks/slack
                                                                                │ verify HMAC, ingest, 200 in <3 s
Gmail ◄──poll every 60 s (history.list)── scheduler loop (local: loops ON) ─────┤
                                                                                ▼
Browser ─► localhost:3000 (dashboard) ─► localhost:8000/api/* ─► Postgres (docker) ─► agent (Groq)
   │ "Connect Gmail"
   └─► accounts.google.com ─► http://localhost:8000/oauth/gmail/callback ─► 302 localhost:3000/connectors?connected=gmail
```

- **Only Slack needs the tunnel.** Gmail polls outward, and Google allows an `http://localhost` redirect for Web clients.
- **`PUBLIC_BASE_URL` stays `http://localhost:8000`.** The Gmail redirect URI is built from it and must match exactly what you registered. The tunnel URL is used only in the Slack app.
- Local loops stay on (`ENABLE_SCHEDULER=true`, `ENABLE_AGENT_WORKER=true`), so Gmail is polled every 60 s and new messages are triaged straight away.

## 3. cloudflared quick tunnel (Slack webhooks to your PC)

**Install once** (Windows, free; `Cloudflare.cloudflared`, currently 2026.10.7). This needs your OK (see open question 4):

```powershell
winget install --id Cloudflare.cloudflared -e
# open a NEW terminal so PATH updates, then:
cloudflared --version
```

**Every session**, with the backend already running on :8000:

```powershell
cloudflared tunnel --url http://localhost:8000
```

It prints a box like this. Copy the `https://…trycloudflare.com` line:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://some-random-words.trycloudflare.com                                               |
+--------------------------------------------------------------------------------------------+
```

Check it from another terminal (it can take 10–30 s to become reachable):

```powershell
curl.exe -s https://some-random-words.trycloudflare.com/healthz    # {"status":"ok",...}
```

The Slack Request URL is that address plus **`/webhooks/slack`**.

Step 5 adds a helper, `uv run python -m scripts.dev_tunnel`. It starts cloudflared, waits for the URL, checks `/healthz` through it, and prints the exact Request URL to paste.

**Facts to remember:**
- **The URL changes every time cloudflared starts.** It stops working the moment you close the window or the PC sleeps. **Each session you must paste the new URL into the Slack app** (§4, "Every session"). Slack re-verifies it on the spot.
- While the tunnel is down, Slack's deliveries fail. Slack retries 3 times (immediately, +1 min, +5 min) and then gives up on that event.
  - **Safety net:** the Slack connector also polls recent DM/group-DM history every 5 minutes while connected, so missed DMs still arrive, just later.
  - Slack only disables event delivery for apps receiving over 1,000 events/hour with more than 95% failing, which can't happen here.
- Quick tunnels are for development: no uptime guarantee, at most 200 requests in flight. They need no Cloudflare account.
  - If `~/.cloudflared/config.yaml` exists, quick tunnels refuse to start; rename that file.
  - If your network blocks QUIC, add `--protocol http2`.
- **While running, the tunnel makes the backend reachable from the internet.**
  - `/api/*` still needs the bearer token.
  - `/webhooks/slack` checks Slack's signature.
  - `/oauth/*/callback` needs a single-use state.
  - `/internal/tick` needs its secret, or returns 404 when no secret is set.
  - The local interactive API docs (`/docs`) are also visible. They show the schema but can't call anything without the token. Close the tunnel when you're not testing Slack.

## 4. Slack app: manifest and first-timer steps

### Manifest (YAML)

This uses **user scopes only**: replies post **as you**, and no bot user is created. Token rotation is off, so the user token doesn't expire.

```yaml
display_information:
  name: Hubline Dev
  description: Personal unified inbox. Reads your DMs and mentions; posts only replies you approve.
  background_color: "#1f2937"
oauth_config:
  scopes:
    user:
      - im:history        # read 1:1 DMs
      - im:read
      - mpim:history      # read group DMs
      - mpim:read
      - channels:history  # read public channels you are in (only @mentions are kept)
      - channels:read
      - groups:history    # read private channels you are in (only @mentions are kept)
      - groups:read
      - users:read        # show sender names
      - chat:write        # post the replies you approve, as you
settings:
  event_subscriptions:
    user_events:
      - message.im
      - message.mpim
      - message.channels
      - message.groups
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

There is no `request_url` in the manifest on purpose. The tunnel URL changes every run, so you paste it on the Event Subscriptions page instead. If Slack rejects the manifest because `event_subscriptions` has no request URL:
1. Delete the whole `event_subscriptions:` block (its four event lines included) and create the app.
2. Add the four events by hand in the first session (step 13 below).

The name "Hubline Dev" keeps this app separate from a later production app, whose Request URL never changes (PLAN §8).

### Steps (first time)

1. Open <https://api.slack.com/apps> and sign in to **Hubline Demo** (top right, if you have several workspaces).
2. Click **Create New App**, then **From a manifest**.
3. **Pick a workspace:** choose **Hubline Demo**, then **Next**.
4. Choose the **YAML** tab, delete the sample, paste the manifest above, then **Next**.
5. Review the summary. It should list 10 user scopes and 4 user events. Click **Create**.
6. You land on **Basic Information**. Under **App Credentials**, next to **Signing Secret**, click **Show**. Copy the value into `backend/.env`:
   ```
   SLACK_SIGNING_SECRET=<paste here>
   ```
   Don't paste it into chat. It is not the Client Secret or the Verification Token.
7. In the left sidebar, open **Install App** (or **OAuth & Permissions**). Click **Install to Hubline Demo**, review the permissions, then **Allow**.
8. The page now shows **User OAuth Token**, starting with `xoxp-`. **Leave it there for now.** After Step 5 is built, you'll copy it into the Hubline dashboard (Connectors → Slack → Paste token). It is not a secret to put in `.env`, and never in chat. You can always come back to this page to copy it again.
9. **Second account for testing:** in Slack, go to workspace name → **Invite people to Hubline Demo** and invite a second email address you own. Accept it in another browser profile or on your phone. You send DMs to yourself from that account; messages you send yourself are ignored as "self".

Optional: on **Basic Information → Display Information**, add an app icon.

### Steps (every session, after Step 5 is built)

10. Start the backend (§8), then the tunnel (§3), and copy the tunnel URL.
11. Slack app page → left sidebar **Event Subscriptions** → **Enable Events: On**.
12. In **Request URL**, enter `https://<random>.trycloudflare.com/webhooks/slack`. Slack sends a challenge right away, and it must show **Verified ✓**. If it says "Your URL didn't respond":
    - check that the backend is running
    - check that `SLACK_SIGNING_SECRET` is set
    - check that the tunnel URL is reachable (`/healthz`)
    - click **Retry**
13. First session only: check that **Subscribe to events on behalf of users** lists `message.im`, `message.mpim`, `message.channels` and `message.groups`. Add them with **Add Workspace Event** if they are missing (this happens when the manifest fallback in §4 was used).
14. Click **Save Changes** at the bottom. If a yellow banner asks you to **reinstall your app**, do it.
    - Reinstalling may issue a new `xoxp-` token. If it does, paste the new one into the dashboard.
    - Changing only the Request URL never needs a reinstall.

**Why paste a token instead of "Connect with Slack"?** Slack's OAuth redirect URLs must be **HTTPS**, so it can't redirect to `http://localhost`. A changing tunnel URL would also break the redirect every run. Installing from the app page gives exactly the same user token, with no redirect needed. See open question 1 for production.

## 5. Gmail (polling + OAuth to localhost)

**Already done by you:**
- the Google Cloud project "hubline"
- the Gmail API enabled
- the External consent screen in **Testing**
- the demo Gmail added as a test user
- scopes `gmail.readonly` and `gmail.send`
- a Web client with redirect URI `http://localhost:8000/oauth/gmail/callback`
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `backend/.env`

**Connect flow:**
1. Dashboard → Connectors → **Connect Gmail**. A server action calls `POST /api/connectors/gmail/oauth/start`, and the backend returns the Google URL.
   - `redirect_uri=http://localhost:8000/oauth/gmail/callback`, byte for byte, with a test asserting it
   - `scope=…/gmail.readonly …/gmail.send`
   - `access_type=offline`, `prompt=consent`
   - PKCE `S256`
   - a single-use `state` stored in `oauth_states`, valid for 10 minutes
2. Google shows the account chooser, then **"Google hasn't verified this app"**. That's normal in Testing mode: click **Continue**.
3. **Tick both permission boxes** (read and send). Google's consent screen lets you untick scopes. If one is missing, Hubline returns to the dashboard with `missing_scope` and asks you to reconnect.
4. Google redirects to the backend callback. The backend:
   - consumes the state atomically and rejects reused or expired state
   - exchanges the code, sending the PKCE verifier and the client secret, server side only
   - reads your address (`users.getProfile`)
   - stores the tokens **encrypted** with Fernet
   - redirects to `http://localhost:3000/connectors?connected=gmail`
5. Turn the connector **on**. The first sync backfills the last 48 h from the inbox, excluding Promotions and Social. At most 50 are stored, and only the newest 10 are auto-triaged (R8).

**Polling (every 60 s while enabled):**
- **First run:**
  1. `users.getProfile` gives the `historyId` (captured first, so nothing is missed).
  2. `messages.list q="in:inbox newer_than:2d -category:promotions -category:social"`.
  3. `messages.get format=full` for each.
- **Afterwards:** `history.list startHistoryId=<cursor> historyTypes=messageAdded labelId=INBOX`, then fetch each new message.
  - Messages labelled Promotions, Social, Spam or Draft are skipped.
  - Your own messages are stored as outbound context, not triaged.
- **History expired** (HTTP 404, after about a week offline): fall back to a `newer_than:1d` list and reset the cursor.
- **Body:**
  - prefers `text/plain`, otherwise HTML converted to text with the stdlib HTML parser (no new dependency)
  - quoted replies are stripped ("On … wrote:" blocks and `>` lines)
  - clipped to 50k characters
- **Quota:** Gmail API calls are free. A poll with nothing new is one `history.list` call (2 quota units).

**Sending:**
- `users.messages.send`, with `threadId` set to the Gmail thread, builds an RFC 2822 message:
  - `To:` the original `Reply-To` if present, else `From` (sender only, no CC or reply-all)
  - `Subject: Re: <original>`
  - `In-Reply-To` and `References` set to the original `Message-ID`
- At-most-once, exactly like the demo connector: an idempotency key, no automatic retry, and a stale send becomes `unknown`.
- The sent message is stored as an outbound row with the Gmail id. It has the `SENT` label, not `INBOX`, so polling never brings it back.

**Tokens:**
- The access token lives about 1 h. The connector refreshes it 60 s before expiry, or once on a 401, and saves the new one encrypted.
- `invalid_grant` means revoked, or the **Testing-mode 7-day refresh-token expiry**. That sets `needs_reauth`, stops polling, and shows a **Reconnect** button.
- **Plan on reconnecting Gmail at least once a week**, and always just before a demo.

## 6. What gets built (in this order)

**1. Credentials + schema**
- Settings:
  - `CREDENTIALS_ENCRYPTION_KEY` (Fernet)
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
  - `SLACK_SIGNING_SECRET`
- Dependencies:
  - `cryptography` (already installed indirectly; made explicit)
  - `respx` (dev only)
- `app/services/credentials.py`: encrypt and decrypt JSON with a single Fernet key (R2), load and save.
- Migration **`0006_connector_credentials.py`**:
  - `connector_credentials` (account id PK/FK cascade, `ciphertext bytea`, `scopes text[]`, `access_expires_at`, `updated_at`)
  - `oauth_states` (`state` PK, `source`, `code_verifier`, `created_at`, `expires_at`, `consumed_at`, plus an index on `expires_at`)
- Expired `oauth_states` are cleaned by the existing maintenance pass. There is no new periodic query.

**2. Generic connect machinery** (no connector names in core code)
- `base.py` gains small optional mixins:
  - `OAuthConnectMixin`: `authorize_url()`, `exchange_code()`
  - `TokenConnectMixin`: `token_fields`, `connect_with_token()`
  - an optional `revoke()`
  - all returning `ConnectResult(credentials, account_identity, self_ids, scopes, access_expires_at)`
- `connector_runtime.build_connector()` decrypts credentials into `ConnectorContext.credentials` and wires `save_credentials`.
- `AuthExpiredError` sets status `needs_reauth`.
- New endpoints:

  | Method | Path | Purpose |
  |---|---|---|
  | POST | `/api/connectors/{key}/credentials` | `{fields: {token: "xoxp-…"}}`. Validated live by the connector (Slack `auth.test`), then encrypted. Returns `ConnectorOut` and never echoes the token. 422 for an invalid token. |
  | POST | `/api/connectors/{key}/oauth/start` | Returns `{authorize_url}`. 409 if not OAuth, 503 if not configured. |
  | GET | `/oauth/{key}/callback` | Public. Uses `?code&state` or `?error`, then 302 to `FRONTEND_URL/connectors?connected=…` or `?error=…`. |
  | POST | `/api/connectors/{key}/disconnect` | Deletes credentials, best-effort revoke, `enabled=false`. History is kept. |

- `ConnectorOut` gains `connect_method` (`oauth` / `token` / `none`) and `account_identity`.

**3. Generic webhook route**
- `POST /webhooks/{key}`:
  1. Raw body → `WebhookRequest` → `connector.parse_webhook()`, which verifies first.
  2. Ingest the messages in one short transaction and answer.
  3. Then kick the agent in the background (`WorkRunner`).
- If the connector is configured but not connected or disabled, it still answers verification challenges. Events are acknowledged with 200 and dropped, so Slack doesn't retry.
- Unknown key → 404. Verification failure → 401, logged without the body.

**4. Slack connector** (`app/connectors/slack.py`)
- Capabilities: WEBHOOK + POLL (5-min safety net) + SEND. Connect method: token. `required_settings`: signing secret + encryption key. `max_reply_chars` 4000.
- **Verify:**
  - `X-Slack-Signature` = `v0=` HMAC-SHA256 of `v0:{X-Slack-Request-Timestamp}:{raw body}`, compared in constant time
  - timestamp within ±5 min
  - `url_verification` → echo the `challenge`
- **Keep:**
  - `message` events in IMs and group DMs
  - in channels, only messages that **@mention you**, or channels listed in the connector's `channels` setting (empty by default)
- **Skip:**
  - subtypes (`message_changed`, `message_deleted`, `channel_join`, `bot_message`, …)
  - `hidden` events
  - edits
- **IDs:**
  - `external_id = "{team}:{channel}:{ts}"`
  - **thread:** for DMs/group DMs without a thread, the whole conversation is one thread, `"{team}:{channel}"`. Threaded replies and channel messages use `"{team}:{channel}:{thread_ts or ts}"`.
- **Sender names:** `users.info`, cached in memory, 1 s timeout. On failure the name falls back to the user id, so the 3 s deadline is never at risk.
- **Echo:** messages whose `user` is you (from `auth.test` → `self_ids`) are stored as **outbound**. The echo of an approved reply has the same `ts` as the stored reply, so dedup drops it.
- **Send:** `chat.postMessage(channel, text, thread_ts=…)` with the user token.
  - DMs not in a thread: posted in the DM itself.
  - Threaded and channel messages: posted in the thread.
- **Poll (safety net):**
  - `conversations.list types=im,mpim` (most recent 20), then `conversations.history oldest=<cursor>` per conversation
  - first connect backfills 24 h
  - the cursor is opaque JSON `{channel: latest_ts}`
- **Errors:**
  - `invalid_auth` / `token_revoked` / `account_inactive` → `needs_reauth`
  - HTTP 429 → `RateLimitedError(Retry-After)`
  - internal custom apps keep the normal tier limits (Slack's 2025 history limit applies only to non-Marketplace apps distributed to other workspaces)

**5. Gmail connector** (`app/connectors/gmail.py`, plus helpers in `app/connectors/_util/` for MIME → text and quote stripping)
- As in §5. Capabilities: POLL (60 s) + SEND. Connect method: OAuth. `required_settings`: Google client id/secret + encryption key.
- Settings: an optional extra `query` filter; the default is the inbox minus Promotions and Social.

**6. Dashboard** (`frontend/`)
- `ConnectorCard`:
  - **Connect Gmail** (server action → `redirect(authorize_url)`; the Next 16 redirect docs are checked first)
  - **Paste Slack token**: a password-type field, sent once via a server action, never shown again
  - Disconnect (with confirm), Reconnect when `needs_reauth`, and the connected identity
- The Connectors page shows a toast for `?connected=` and `?error=`.
- New Vitest tests for these states. The e2e smoke stays on demo data, so it needs no network.

**7. Dev tooling + docs**
- `scripts/dev_tunnel.py` (§3)
- `docs/setup/slack.md` (§4, with screenshots described step by step) and `docs/setup/slack-app-manifest.yaml`
- `docs/setup/gmail.md` (§5)
- README "Live connectors (local)" section
- `.env.example` gains the new names

**8. Live local check with you**, then `reports/step-5-report.md`.

## 7. Tests (all offline)

| Area | Tests |
|---|---|
| Credentials | Round trip; ciphertext contains no plaintext; wrong key → clear error; the API never returns credential values (response JSON grep). |
| Migration | `0006` up/down; models match (`alembic check`); table list updated. |
| Slack verify | Valid; bad signature; stale and future timestamps (±5 min); missing headers; `url_verification` challenge; retry header (`X-Slack-Retry-Num`) deduped. |
| Slack parse | Recorded fixtures: DM, group DM, thread reply, channel @mention kept, channel chatter dropped, each skipped subtype, self echo → outbound. |
| Slack HTTP (respx) | `auth.test` token connect (ok / `invalid_auth` → 422); `chat.postMessage` payload incl. `thread_ts`; 429 → `RateLimitedError`; `token_revoked` → `needs_reauth`; safety-net poll pagination and cursor. |
| Gmail OAuth (respx) | Exact `redirect_uri`, scopes, `access_type`, `prompt`, PKCE S256; state single-use, expired, unknown, `?error=access_denied`; missing scope → `missing_scope`; tokens stored encrypted. |
| Gmail fetch (respx) | First-run list + get; `history.list` incremental; 404 fallback; category, spam and draft skip; self message → outbound; access-token refresh before expiry and on 401; `invalid_grant` → `needs_reauth`. |
| Gmail MIME | text/plain; HTML-only; multipart/alternative; base64url; non-UTF-8 charset; quoted-reply stripping; `Reply-To` vs `From`. |
| Gmail send | Decoded raw message asserts `To`, `Subject: Re:`, `In-Reply-To`, `References` and `threadId`; at-most-once on 5xx (`failed`, never retried). |
| Webhook route | Unknown key 404; forged 401 with nothing stored; disabled connector → 200 and dropped; ingest + job enqueued in one transaction; response under 3 s with a slow `users.info` (timeout path). |
| Contract suite | Slack and Gmail join the parametrised suite automatically, with respx fixtures. |
| Frontend | ConnectorCard: OAuth button, token form (value cleared after submit), needs_reauth → Reconnect, callback toasts. |

## 8. Running it locally (after the build)

Four terminals:

```powershell
# 1. database
docker compose up -d --wait db
# 2. backend   (backend/.env: LLM_PROVIDER=groq, PUBLIC_BASE_URL=http://localhost:8000)
cd backend; uv run alembic upgrade head; uv run uvicorn app.main:create_app --factory --port 8000
# 3. dashboard
cd frontend; npm run dev
# 4. tunnel (Slack only) - prints the Request URL to paste into the Slack app
cd backend; uv run python -m scripts.dev_tunnel      # or: cloudflared tunnel --url http://localhost:8000
```

Then open http://localhost:3000 and log in.

## 9. What you must still do by hand, in order

**Now (before the build; none of it needs Step 5 code):**
1. **Answer the open questions** below, including whether I may install cloudflared with winget or you'll do it.
2. **Create the Slack app** from the manifest (§4 steps 1–5).
3. **Copy the Signing Secret** into `backend/.env` as `SLACK_SIGNING_SECRET` (§4 step 6).
4. **Install the app** to Hubline Demo (§4 step 7). Leave the `xoxp-` token on that page for now (step 8).
5. **Invite your second Slack account** and log in with it somewhere (§4 step 9).
6. **Have a second email address ready** to send test mail to the demo Gmail. Optional: send one or two mails now, so the first sync has something to backfill.
7. **Google Cloud check** (already done, just confirm): Clients → your Web client → **Authorized redirect URIs** contains exactly `http://localhost:8000/oauth/gmail/callback`, with no trailing slash, `http` and port 8000. Audience → **Test users** contains the demo Gmail.

**I do during the build** (you don't need to):
- Generate `CREDENTIALS_ENCRYPTION_KEY` into `backend/.env` without printing it.
- Set `LLM_PROVIDER=groq` there, if you agree (open question 3).

**After the build (first live session):**
8. Start the database, backend and dashboard (§8). Start the tunnel (§3) and copy its URL.
9. In the Slack app's **Event Subscriptions**, paste `https://<random>.trycloudflare.com/webhooks/slack`, wait for **Verified ✓**, check the 4 user events, and **Save Changes** (§4 steps 11–14).
10. Dashboard → **Connectors → Slack → Paste token**: copy the `xoxp-` token from the Slack app's **Install App** page, paste, **Connect**, then switch Slack **on**.
11. Dashboard → **Connectors → Connect Gmail** → choose the demo account → **Continue** past "Google hasn't verified this app" → **tick both boxes** → Continue. Back on the dashboard, switch Gmail **on**.
12. Test:
    - From the second Slack account, DM yourself, e.g. *"Can you send me the Q3 deck by 3pm?"*. It should appear within about 10 s.
    - From the second email address, send a question to the demo Gmail. It should appear within about 90 s.
    - Approve both drafts, then check the Slack DM and the Gmail thread.

**Every later session:**
- Start the tunnel and paste its new URL into Event Subscriptions (steps 8–9).
- Reconnect Gmail if it shows **needs_reauth** (at least every 7 days in Testing mode).

## 10. Deviations from the plan (for your approval)

1. **Order:** Step 5 runs locally before Step 4 deploys. This is your decision, recorded as R13 in `docs/PLAN.md`.
2. **Slack connects with a pasted user token**, not the OAuth redirect flow. Locally this is forced: Slack needs HTTPS redirects, and the tunnel URL keeps changing. I recommend the same in production (open question 1), which drops `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET`.
3. **Slack DM threading:** a DM or group DM without a thread is one Hubline thread per conversation, and replies go into the DM itself. Plan §9 had one thread per top-level message, with replies in a Slack thread. That splits a DM conversation and loses context, and threaded replies in a DM look odd.
4. **Slack also polls** recent DMs every 5 minutes while connected, as a safety net for events missed while the tunnel or a sleeping Render instance was down. PLAN §8 already mentions this for production.
5. **Migration number:** `0006` (Step 3 used `0005`). R1's table said `0005` for Step 5.
6. **Gmail replies go to `Reply-To` when present**, else `From`. Still sender only, no CC.

## 11. Risks

- **Tunnel URL churn:** events are missed while the URL isn't updated; the 5-minute DM poll covers DMs. Quick tunnels have no uptime guarantee.
- **Gmail Testing mode:** the refresh token expires after 7 days, and the "unverified app" warning is unavoidable. `gmail.readonly` and `gmail.send` are restricted scopes, so publishing would need Google verification. We stay in Testing, with at most 100 test users.
- **Privacy:** message text is stored in local Postgres and sent to Groq for triage. Use the demo Slack workspace and demo Gmail only (decision 2).
- **PC clock:** Slack signatures are rejected if the clock is off by more than 5 minutes. Windows time sync must be on.
- **Slack install without a redirect URL:** app-page install of a user-scopes-only app is expected to work. If Slack insists on a bot user, the fallback is to add a minimal `bot_user` to the manifest. We'd still use only the user token.
- **Echo races:** if Slack's echo of a reply arrives before the send call returns, dedup on `ts` still catches it (unique key), and the outbound row keeps the reply's id.

## 12. Open questions

1. **Slack in production:** keep **token paste** there too (recommended: simpler, no client secret, single user), or build the Slack OAuth redirect flow for Step 4?
2. **Slack scope of capture:** DMs + group DMs + @mentions of you in channels (recommended)? Or also every message in channels you pick (the `channels` setting)?
3. **Local LLM:** may I set `LLM_PROVIDER=groq` in `backend/.env` during the build (free tier; tests always use the fake model)? Or do you prefer to switch it yourself?
4. **cloudflared:** may I run `winget install --id Cloudflare.cloudflared -e` during the build, or will you install it?
5. **Gmail reply recipient:** `Reply-To` when present, else `From` (recommended)? Or always `From`?
