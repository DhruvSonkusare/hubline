# Gmail setup (local first, $0)

Hubline polls the demo Gmail inbox every 60 s and replies in the same thread, to the sender only (`Reply-To` if present, else `From`). It uses OAuth 2 with PKCE. The Gmail API is free.

## Google Cloud (one time)

1. Create the project ("hubline") and **enable the Gmail API**.
2. **OAuth consent screen / Audience:** External, publishing status **Testing**, and the demo Gmail added under **Test users**.
3. **Data access / scopes:** `https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/gmail.send`.
4. **Clients → Create client → Web application.** Set **Authorized redirect URIs** to exactly:
   ```
   http://localhost:8000/oauth/gmail/callback
   ```
   `http`, port 8000, no trailing slash. Hubline builds this URI from `PUBLIC_BASE_URL`, so keep `PUBLIC_BASE_URL=http://localhost:8000` locally (never the tunnel URL).
5. Put the client's values into `backend/.env`:
   ```
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ```
   `CREDENTIALS_ENCRYPTION_KEY` must be set too, because the tokens are stored encrypted.

## Connect

1. Dashboard → **Connectors → Gmail → Connect Gmail**.
2. Pick the demo account. Google shows **"Google hasn't verified this app"**, which is normal in Testing mode: click **Continue**.
3. **Tick both permission boxes** (read and send). If one is unticked, Hubline returns with "Both permissions are needed".
4. You land back on the Connectors page with "Gmail connected". Switch Gmail **on**.
   - The first sync backfills the last 48 h of the inbox (Promotions and Social excluded), at most 50 messages.
   - Only the newest 10 are auto-triaged; the rest can be triaged on request.

## How it works

- **First sync:** reads the mailbox `historyId` first (so nothing is missed), then lists `in:inbox -category:promotions -category:social newer_than:2d`.
- **Every 60 s:** `history.list` (messageAdded, INBOX) from the saved history id. An idle poll is one cheap call.
- **History id expired** (after about a week offline): resync the last day and continue.
- **Bodies:**
  - prefer `text/plain`, otherwise HTML converted to text
  - quoted replies are stripped
  - attachments are ignored
- **Bulk mail:** newsletters carrying `List-Unsubscribe`/`Precedence: bulk` from a no-reply sender get priority 1 without an LLM call.
- **Replies:** RFC 2822 with `In-Reply-To`/`References`, `Subject: Re: …` and the Gmail `threadId`.
  - Sent at most once (an idempotency key, never retried automatically).
  - The sent mail has the `SENT` label, not `INBOX`, so polling never brings it back.
- **Tokens:**
  - The access token is refreshed 60 s before it expires.
  - **In Testing mode the refresh token expires after 7 days.** The card then shows **needs re-login**; click **Reconnect Gmail**. Reconnect before a demo.
  - **Disconnect** revokes the token at Google and deletes it locally. Message history stays.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Google error `redirect_uri_mismatch` | The client's redirect URI must be exactly `http://localhost:8000/oauth/gmail/callback`, and `PUBLIC_BASE_URL` must be `http://localhost:8000`. |
| `access_denied` / "403: access_denied" | The account isn't a **Test user** of the consent screen. |
| "That sign-in link expired" | The consent page was open for more than 10 minutes, or the link was used twice. Click Connect again. |
| `needs re-login` after a week | Testing-mode refresh tokens last 7 days. Click Reconnect. |
