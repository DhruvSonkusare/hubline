# Slack setup (local first, $0)

Hubline reads your Slack **DMs, group DMs and @mentions of you**. It posts the replies you approve **as you**, using a user token. Locally, Slack's events reach your PC through a free **cloudflared quick tunnel**; no Cloudflare account is needed.

| What | Value |
|---|---|
| App | "Hubline Dev" in the **Hubline Demo** workspace, created from [`slack-app-manifest.yaml`](slack-app-manifest.yaml) |
| Token | **User OAuth Token** (`xoxp-…`). You paste it into the dashboard once; it is stored encrypted and never shown again. |
| Backend env | `SLACK_SIGNING_SECRET` (Basic Information → App Credentials → Signing Secret) and `CREDENTIALS_ENCRYPTION_KEY` |
| Request URL | `https://<random>.trycloudflare.com/webhooks/slack`. **Changes every run.** |
| User events | `message.im`, `message.mpim`, `message.channels`, `message.groups` |

**Why paste a token instead of "Connect with Slack"?**
- Slack only allows **HTTPS** OAuth redirect URLs, and a localhost redirect is plain HTTP. The tunnel URL also changes every run.
- Installing from the app's own page gives exactly the same user token, so token paste is used locally and, for now, in production too.
- The OAuth redirect flow is a later option once there's a stable HTTPS URL.

## One-time setup

1. Create the app at <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
   - Pick the **Hubline Demo** workspace.
   - On the **YAML** tab, paste `slack-app-manifest.yaml`, then **Next** → **Create**.
   - The manifest has no `event_subscriptions`, because Slack wants a verified Request URL first. The events are added in the first tunnel session below.
2. **Basic Information → App Credentials → Signing Secret → Show.** Copy it into `backend/.env`:
   ```
   SLACK_SIGNING_SECRET=<paste>
   ```
3. **Install App** (or **OAuth & Permissions**) → **Install to Hubline Demo** → **Allow**. The page now shows the **User OAuth Token** (`xoxp-…`). Leave it there until step 6.
4. Invite a **second Slack account** to the workspace. You DM yourself from that account; messages you send yourself are ignored as "self".
5. Install cloudflared (free):
   ```powershell
   winget install --id Cloudflare.cloudflared -e
   ```
6. With the backend and dashboard running, go to Dashboard → **Connectors → Slack**.
   - Paste the `xoxp-` token and click **Connect**. Hubline checks it with `auth.test` and lists any missing scopes.
   - Then switch Slack **on**.

## Every session: tunnel + Event Subscriptions

1. Start the backend (port 8000), then the tunnel:
   ```powershell
   cd backend
   uv run python -m scripts.dev_tunnel
   # or by hand: cloudflared tunnel --url http://localhost:8000
   ```
   The helper waits for the tunnel, checks `/healthz` through it, and prints the exact Request URL:
   ```
   Paste this into Slack app -> Event Subscriptions -> Request URL:

       https://some-random-words.trycloudflare.com/webhooks/slack
   ```
2. Slack app page → **Event Subscriptions** → **Enable Events: On**.
3. **Request URL:** paste the URL. Slack sends a challenge immediately and must show **Verified ✓**. If it says "Your URL didn't respond":
   - check that the backend is running
   - check that `SLACK_SIGNING_SECRET` is set (the backend log shows `webhook_rejected` when the secret is wrong)
   - check that the tunnel answers `/healthz`
   - then click **Retry**
4. **First session only:** under **Subscribe to events on behalf of users**, click **Add Workspace Event** and add `message.im`, `message.mpim`, `message.channels` and `message.groups`.
5. Click **Save Changes**.
   - If a yellow banner asks you to **reinstall your app**, do it. Slack may issue a new `xoxp-` token; if it does, Disconnect and paste the new token on the Connectors page.
   - Changing only the Request URL never needs a reinstall.

**Facts to remember:**
- **The tunnel URL changes every time cloudflared starts**, and it dies when the window closes or the PC sleeps. Paste the new one into Event Subscriptions each session.
- While the tunnel is down, Slack retries each event three times (immediately, +1 min, +5 min) and then drops it. The **safety-net poll** (below) still picks up DMs within minutes.
- Close the tunnel when you're not testing. While it runs, the backend is reachable from the internet, but:
  - `/api/*` still needs the bearer token
  - `/webhooks/slack` checks Slack's signature
  - `/oauth/*` needs a single-use state
- If `~/.cloudflared/config.yaml` exists, quick tunnels refuse to start; rename it. On strict networks, add `--protocol http2`.

## What gets captured

- **Captured:**
  - 1:1 DMs and group DMs (both directions; yours are stored as context, never triaged)
  - channel and private-channel messages that **@mention you**
  - other channel chatter is dropped
- **Ignored:** edits, deletions, joins and other subtypes, bot messages, and hidden events.
- **Threads:**
  - A DM without a thread is one Hubline conversation, and the reply goes into the DM.
  - A threaded message, or a mention in a channel, gets the reply in its thread.
- **Echoes:** when you approve a reply, Slack sends it back as an event with the same `ts`. Dedup on `(source, external_id)` drops it, so nothing is duplicated or triaged.

## Rate limits: the safety-net poll stays under 1 history call per minute

Slack limits `conversations.history` for non-Marketplace apps to about **1 request per minute**, at 15 messages per call. Internal apps may be exempt, but Hubline stays under the stricter limit anyway:

- Real-time delivery is the Events API, which isn't rate limited this way.
- The poll runs **every 5 minutes** (`poll_interval_s=300`) and makes **at most one** `conversations.history` call per poll, with `limit=15`.
- If the previous history call was **less than 60 s ago** (for example after a "Sync now" click), the poll skips history entirely. The time is kept in the poll cursor in the database, so it survives restarts.
- With several DMs open, the poll **rotates**: each poll checks the least recently checked DM or group DM. With N conversations, each is checked about every 5·N minutes, while the total stays at ≤1 history call per 5 minutes.
- `conversations.list` (Tier 2), `auth.test`, `users.info` (cached) and `chat.postMessage` aren't covered by the history limit.
- The first check of a conversation reads its last 24 h; later checks read only what is newer than the newest message seen.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Connectors page: "not configured" | `SLACK_SIGNING_SECRET` or `CREDENTIALS_ENCRYPTION_KEY` missing in `backend/.env`; restart the backend. |
| "Paste the User OAuth Token (it starts with xoxp-)" | You copied the bot (`xoxb-`) or app-level (`xapp-`) token. |
| "the token lacks …" | Add those user scopes under OAuth & Permissions, reinstall, and paste the new token. |
| DMs don't appear | Is Slack **on**? Is the Request URL **Verified**, with the 4 events saved? Are you DMing from the second account? |
| Status `needs re-login` | The token was revoked (app uninstalled or reinstalled). Disconnect, then paste the current token. |
