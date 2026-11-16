"""Slack connector: real-time user events (Events API) + a rate-limit-safe DM poll + replies as you.

Connect: paste the app's **User OAuth Token** (xoxp-...). It is checked with auth.test, stored
encrypted, and never shown again. Slack's OAuth redirect needs HTTPS, so locally (and for now in
production) the token comes from the app's own install page instead.

Captured: 1:1 DMs, group DMs, and messages in channels you are in that @mention you. Edits,
deletions, joins and bot messages are ignored. Your own messages are stored as outbound context.

Rate limits: Slack limits conversations.history for non-Marketplace apps to about 1 request per
minute (15 messages per call). The safety-net poll makes AT MOST ONE history call per poll,
polls every 5 minutes, skips history entirely if the previous history call was less than 60 s ago
(e.g. "Sync now" clicks), and rotates through your DMs least-recently-checked first. So the
connector never exceeds 1 history request per minute, however many DMs are open.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.connectors.base import (
    AuthExpiredError,
    AuthType,
    Capability,
    ConnectError,
    Connector,
    ConnectorDescriptor,
    ConnectResult,
    PermanentConnectorError,
    RateLimitedError,
    TokenConnectMixin,
    TokenField,
    TransientConnectorError,
    WebhookVerificationError,
)
from app.connectors.models import (
    FetchResult,
    NormalizedMessage,
    ReplyContext,
    Sender,
    SendResult,
    WebhookRequest,
    WebhookResponse,
    WebhookResult,
)
from app.connectors.registry import register

API = "https://slack.com/api/"
SIGNATURE_TOLERANCE_S = 300  # Slack's recommendation: reject requests older than 5 minutes
REQUIRED_SCOPES = frozenset(
    {
        "im:history",
        "im:read",
        "mpim:history",
        "mpim:read",
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "users:read",
        "chat:write",
    }
)
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
)
_KEPT_SUBTYPES = frozenset({None, "thread_broadcast", "file_share"})
_CHANNEL_TYPES = frozenset({"im", "mpim", "channel", "group"})

# History pacing (see the module docstring)
HISTORY_MIN_INTERVAL_S = 60.0
HISTORY_LIMIT = 15  # the non-Marketplace cap per call
BACKFILL_HOURS = 24
NAME_LOOKUP_TIMEOUT_S = 1.0

_EMPTY_CURSOR = json.dumps({"v": 1, "latest": {}, "checked": {}, "last_history_at": 0.0})

_name_cache: dict[tuple[str, str], str] = {}
_NAME_CACHE_MAX = 1000


def verify_signature(
    headers: Mapping[str, str], body: bytes, signing_secret: str, *, now: float
) -> None:
    """Slack v0 signing: HMAC-SHA256 over "v0:{timestamp}:{raw body}". Raises on mismatch."""
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        raise WebhookVerificationError("missing Slack signature headers")
    try:
        ts_int = int(ts)
    except ValueError:
        raise WebhookVerificationError("bad Slack timestamp") from None
    if abs(now - ts_int) > SIGNATURE_TOLERANCE_S:
        raise WebhookVerificationError("stale Slack timestamp")
    base = b"v0:" + ts.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), sig.encode()):
        raise WebhookVerificationError("bad Slack signature")


async def slack_call(
    http: httpx.AsyncClient,
    method: str,
    token: str,
    params: Mapping[str, Any] | None = None,
    *,
    http_timeout: float = 15.0,
) -> dict[str, Any]:
    """POST a Web API method (form-encoded) and map Slack's errors to connector errors."""
    data = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = await http.post(
            API + method,
            data=data,
            headers={"Authorization": f"Bearer {token}"},
            timeout=http_timeout,
        )
    except httpx.TimeoutException as exc:
        raise TransientConnectorError(f"Slack {method} timed out") from exc
    if resp.status_code == 429:
        retry = float(resp.headers.get("retry-after", "60") or 60)
        raise RateLimitedError(f"Slack rate limit on {method}", retry_after=retry)
    if resp.status_code >= 500:
        raise TransientConnectorError(f"Slack {method}: HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise PermanentConnectorError(f"Slack {method}: HTTP {resp.status_code}")
    payload: dict[str, Any] = resp.json()
    if not payload.get("ok"):
        error = str(payload.get("error", "unknown_error"))
        if error in _AUTH_ERRORS:
            raise AuthExpiredError(f"Slack token no longer works ({error}); paste a new one")
        if error == "ratelimited":
            raise RateLimitedError(f"Slack rate limit on {method}", retry_after=60.0)
        raise PermanentConnectorError(f"Slack {method} failed: {error}")
    payload["_scopes"] = resp.headers.get("x-oauth-scopes", "")
    return payload


def ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_MENTION = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)[^>]*>")


def to_plain_text(text: str, self_ids: frozenset[str]) -> str:
    """Slack mrkdwn -> readable text (mentions, links, entities)."""
    text = _USER_MENTION.sub(lambda m: "@you" if m.group(1) in self_ids else "@" + m.group(1), text)
    text = _CHANNEL_MENTION.sub(lambda m: "#" + m.group(1), text)
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), text)
    text = _SPECIAL.sub(lambda m: "@" + m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def thread_key(team: str, channel: str, channel_type: str, ts: str, thread_ts: str | None) -> str:
    """A DM without a thread is one conversation; threads and channel messages are per thread."""
    if channel_type in ("im", "mpim") and not thread_ts:
        return f"{team}:{channel}"
    return f"{team}:{channel}:{thread_ts or ts}"


@register
class SlackConnector(Connector, TokenConnectMixin):
    descriptor = ConnectorDescriptor(
        key="slack",
        display_name="Slack",
        auth_type=AuthType.OAUTH2,  # a user OAuth token, pasted from the app's install page
        capabilities=frozenset({Capability.WEBHOOK, Capability.POLL, Capability.SEND}),
        poll_interval_s=300,  # safety net only; real-time delivery is the Events API
        required_settings=("slack_signing_secret", "credentials_encryption_key"),
        max_reply_chars=4000,
    )
    token_fields = (
        TokenField(
            name="token",
            label="User OAuth Token",
            placeholder="xoxp-…",
            help="Slack app page → OAuth & Permissions → User OAuth Token",
        ),
    )

    # ------------------------------------------------------------------ connect

    @classmethod
    async def connect_with_token(
        cls, fields: Mapping[str, str], *, http: httpx.AsyncClient, app_settings: Settings
    ) -> ConnectResult:
        token = (fields.get("token") or "").strip()
        if not token.startswith("xoxp-"):
            raise ConnectError(
                "invalid_token",
                "Paste the User OAuth Token (it starts with xoxp-), not the bot or app token",
            )
        try:
            info = await slack_call(http, "auth.test", token)
        except AuthExpiredError:
            raise ConnectError("invalid_token", "Slack rejected this token") from None
        granted = {s.strip() for s in info["_scopes"].split(",") if s.strip()}
        missing = sorted(REQUIRED_SCOPES - granted) if granted else []
        if missing:
            raise ConnectError(
                "missing_scope",
                f"the token lacks {', '.join(missing)}; add them to the app and reinstall it",
            )
        team, user = str(info["team_id"]), str(info["user_id"])
        return ConnectResult(
            credentials={
                "token": token,
                "team_id": team,
                "team": info.get("team"),
                "user_id": user,
                "user": info.get("user"),
                "url": info.get("url"),
            },
            account_identity=f"{info.get('team') or team} / {info.get('user') or user}",
            self_ids=[user],
            scopes=sorted(granted),
        )

    async def revoke(self) -> None:
        if self.ctx.credentials:
            await slack_call(self.ctx.http, "auth.revoke", self._token())

    # ------------------------------------------------------------------ helpers

    def _token(self) -> str:
        creds = self.ctx.credentials or {}
        token = creds.get("token")
        if not token:
            raise AuthExpiredError("Slack is not connected")
        return str(token)

    def _self_ids(self, extra: str | None = None) -> frozenset[str]:
        ids = set(self.ctx.self_ids)
        user = (self.ctx.credentials or {}).get("user_id")
        if user:
            ids.add(str(user))
        if extra:
            ids.add(extra)
        return frozenset(ids)

    async def _user_name(self, team: str, user: str) -> str | None:
        key = (team, user)
        if key in _name_cache:
            return _name_cache[key]
        if not self.ctx.credentials:
            return None
        try:
            info = await asyncio.wait_for(
                slack_call(self.ctx.http, "users.info", self._token(), {"user": user}),
                timeout=NAME_LOOKUP_TIMEOUT_S,
            )
        except Exception:  # never let a name lookup cost us the 3-second webhook deadline
            return None
        u = info.get("user") or {}
        profile = u.get("profile") or {}
        name = profile.get("display_name") or u.get("real_name") or u.get("name")
        if name:
            if len(_name_cache) >= _NAME_CACHE_MAX:
                _name_cache.clear()
            _name_cache[key] = str(name)
        return str(name) if name else None

    def _permalink(self, channel: str, ts: str) -> str | None:
        base = (self.ctx.credentials or {}).get("url")
        return (
            f"{str(base).rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}" if base else None
        )

    async def _normalize(
        self,
        event: Mapping[str, Any],
        *,
        team: str,
        channel: str,
        channel_type: str,
        self_ids: frozenset[str],
    ) -> NormalizedMessage | None:
        """One Slack message -> NormalizedMessage, or None if it isn't something we keep."""
        if event.get("subtype") not in _KEPT_SUBTYPES or event.get("hidden"):
            return None
        user, ts = event.get("user"), event.get("ts")
        if not user or not ts:
            return None
        if channel_type not in _CHANNEL_TYPES:
            return None
        raw_text = str(event.get("text") or "")
        mentioned = any(f"<@{sid}" in raw_text for sid in self_ids)
        if channel_type in ("channel", "group") and not mentioned:
            return None  # channels: only messages that @mention you (never your own chatter)
        body = to_plain_text(raw_text, self_ids)
        if not body:
            return None
        # a thread's root (thread_ts == ts) belongs with the conversation; replies have their own
        raw_thread = event.get("thread_ts")
        thread_ts = str(raw_thread) if raw_thread and raw_thread != ts else None
        is_self = user in self_ids
        name = "You" if is_self else await self._user_name(team, str(user))
        reply_thread = thread_ts or (str(ts) if channel_type in ("channel", "group") else None)
        title = {
            "im": "Slack · Direct message",
            "mpim": "Slack · Group DM",
        }.get(channel_type, "Slack · Mention")
        return NormalizedMessage(
            source="slack",
            external_id=f"{team}:{channel}:{ts}",
            thread_id=thread_key(team, channel, channel_type, str(ts), thread_ts),
            sender=Sender(id=str(user), name=name),
            body=body[:50_000],
            received_at=ts_to_datetime(str(ts)),
            direction="outbound" if is_self else "inbound",
            thread_title=title,
            permalink=self._permalink(channel, str(ts)),
            reply_meta={"channel": channel, "thread_ts": reply_thread, "team": team},
            raw={"channel_type": channel_type, "subtype": event.get("subtype")},
        )

    # ------------------------------------------------------------------ webhook

    async def parse_webhook(self, request: WebhookRequest) -> WebhookResult:
        secret = self.ctx.app_settings.slack_signing_secret
        if secret is None or not secret.get_secret_value():
            raise WebhookVerificationError("SLACK_SIGNING_SECRET is not set")
        verify_signature(request.headers, request.body, secret.get_secret_value(), now=time.time())
        try:
            payload = json.loads(request.body)
        except ValueError:
            return WebhookResult(response=WebhookResponse(400, b"bad json"))
        kind = payload.get("type")
        if kind == "url_verification":
            challenge = str(payload.get("challenge", "")).encode()
            return WebhookResult(response=WebhookResponse(200, challenge, "text/plain"))
        if kind != "event_callback":
            return WebhookResult()
        event = payload.get("event") or {}
        if event.get("type") != "message":
            return WebhookResult()
        team = str(payload.get("team_id") or (self.ctx.credentials or {}).get("team_id") or "")
        installer = next(
            (a.get("user_id") for a in payload.get("authorizations") or [] if a.get("user_id")),
            None,
        )
        message = await self._normalize(
            event,
            team=team,
            channel=str(event.get("channel", "")),
            channel_type=str(event.get("channel_type", "")),
            self_ids=self._self_ids(installer),
        )
        return WebhookResult(messages=[message] if message else [])

    # ------------------------------------------------------------------ poll (safety net)

    async def fetch_new(self, since: str | None) -> FetchResult:
        """Check ONE DM / group DM per poll for messages the webhook may have missed."""
        token = self._token()
        state: dict[str, Any] = json.loads(since) if since else {}
        convs: dict[str, str] = dict(state.get("latest") or {})  # channel -> newest ts seen
        checked: dict[str, float] = dict(state.get("checked") or {})  # channel -> epoch
        now = self.ctx.clock()
        now_s = now.timestamp()
        last = float(state.get("last_history_at") or 0.0)
        if now_s - last < HISTORY_MIN_INTERVAL_S:
            return FetchResult(cursor=json.dumps(state) if state else _EMPTY_CURSOR)

        listing = await slack_call(
            self.ctx.http,
            "conversations.list",
            token,
            {"types": "im,mpim", "exclude_archived": "true", "limit": 200},
        )
        channels = [
            (str(c["id"]), "im" if c.get("is_im") else "mpim")
            for c in listing.get("channels") or []
            if not c.get("is_user_deleted")
        ]
        new_state: dict[str, Any] = {
            "v": 1,
            "latest": convs,
            "checked": {cid: checked.get(cid, 0.0) for cid, _ in channels},
            "last_history_at": last,
        }
        if not channels:
            return FetchResult(cursor=json.dumps(new_state))

        channel, channel_type = min(channels, key=lambda c: (checked.get(c[0], 0.0), c[0]))
        oldest = convs.get(channel) or f"{(now - timedelta(hours=BACKFILL_HOURS)).timestamp():.6f}"
        history = await slack_call(
            self.ctx.http,
            "conversations.history",
            token,
            {"channel": channel, "oldest": oldest, "limit": HISTORY_LIMIT, "inclusive": "false"},
        )
        new_state["last_history_at"] = now_s
        new_state["checked"][channel] = now_s
        team = str((self.ctx.credentials or {}).get("team_id") or "")
        self_ids = self._self_ids()
        messages: list[NormalizedMessage] = []
        newest = oldest
        for item in history.get("messages") or []:
            ts = str(item.get("ts", ""))
            if ts and float(ts) > float(newest):
                newest = ts
            m = await self._normalize(
                item, team=team, channel=channel, channel_type=channel_type, self_ids=self_ids
            )
            if m is not None:
                messages.append(m)
        new_state["latest"][channel] = newest
        return FetchResult(messages=messages, cursor=json.dumps(new_state), has_more=False)

    # ------------------------------------------------------------------ send

    async def send(self, thread_id: str, text: str, *, reply_to: ReplyContext) -> SendResult:
        meta = reply_to.reply_meta
        channel = meta.get("channel")
        if not channel:
            raise PermanentConnectorError("this Slack message has no channel to reply to")
        result = await slack_call(
            self.ctx.http,
            "chat.postMessage",
            self._token(),
            {"channel": channel, "text": text, "thread_ts": meta.get("thread_ts")},
            http_timeout=20.0,
        )
        ts = str(result["ts"])
        team = str(meta.get("team") or (self.ctx.credentials or {}).get("team_id") or "")
        return SendResult(
            external_id=f"{team}:{result.get('channel') or channel}:{ts}",
            thread_id=thread_id,
            sent_at=ts_to_datetime(ts),
        )
