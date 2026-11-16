"""Gmail connector: OAuth 2 (PKCE) + polling via history.list + replies in the same thread.

Scopes: gmail.readonly (read the inbox) and gmail.send (send replies you approve).
Inbox only, minus Promotions/Social/Spam; first sync backfills BACKFILL_HOURS (48 h).
In Google's "Testing" publishing mode refresh tokens expire after 7 days, which surfaces as
needs_reauth (Reconnect on the Connectors page).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.addresses import is_no_reply_address
from app.config import Settings
from app.connectors._util.mail import (
    build_reply,
    gmail_body_text,
    header_map,
    parse_sender,
    reply_recipient,
)
from app.connectors.base import (
    AuthExpiredError,
    AuthType,
    Capability,
    ConnectError,
    Connector,
    ConnectorDescriptor,
    ConnectResult,
    OAuthConnectMixin,
    PermanentConnectorError,
    RateLimitedError,
    TransientConnectorError,
)
from app.connectors.models import FetchResult, NormalizedMessage, ReplyContext, Sender, SendResult
from app.connectors.registry import register

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me/"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)
INBOX_QUERY = "in:inbox -category:promotions -category:social"
SKIP_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH", "DRAFT"})
REFRESH_MARGIN_S = 60
MAX_PER_POLL = 50
FALLBACK_DAYS = 1


class _NotFoundError(PermanentConnectorError):
    """Gmail answered 404 (an expired history id, a deleted message or thread)."""


def _google_error(resp: httpx.Response) -> str:
    """`NOT_FOUND Requested entity was not found.` from Google's JSON error body, else ''."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return ""
    if not isinstance(err, dict):
        return str(err)[:200]
    return " ".join(str(x) for x in (err.get("status"), err.get("message")) if x)[:200]


def _expires_at(expires_in: Any, now: datetime) -> datetime:
    return now + timedelta(seconds=int(expires_in or 3600))


@register
class GmailConnector(Connector, OAuthConnectMixin):
    descriptor = ConnectorDescriptor(
        key="gmail",
        display_name="Gmail",
        auth_type=AuthType.OAUTH2,
        capabilities=frozenset({Capability.POLL, Capability.SEND}),
        poll_interval_s=60,
        required_settings=(
            "google_client_id",
            "google_client_secret",
            "credentials_encryption_key",
        ),
        max_reply_chars=20_000,
    )

    # ------------------------------------------------------------------ OAuth

    @classmethod
    def authorize_url(
        cls,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        app_settings: Settings,
        login_hint: str | None = None,
    ) -> str:
        params = {
            "client_id": app_settings.google_client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",  # we need a refresh token
            "prompt": "consent",  # ...every time, so reconnecting always yields a new one
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if login_hint and "@" in login_hint:
            # Reconnect: Google preselects the connected account (the user can still switch).
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    @classmethod
    async def exchange_code(
        cls,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        http: httpx.AsyncClient,
        app_settings: Settings,
    ) -> ConnectResult:
        secret = app_settings.google_client_secret
        resp = await http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": app_settings.google_client_id or "",
                "client_secret": secret.get_secret_value() if secret else "",
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=15.0,
        )
        if resp.status_code >= 500:
            raise TransientConnectorError(f"Google token endpoint: HTTP {resp.status_code}")
        token = resp.json()
        if resp.status_code != 200:
            raise ConnectError("token_exchange_failed", str(token.get("error", "unknown error")))
        granted = set(str(token.get("scope", "")).split())
        if not set(SCOPES) <= granted:
            raise ConnectError(
                "missing_scope", "Tick both Gmail permissions (read and send) on Google's screen"
            )
        if not token.get("refresh_token"):
            raise ConnectError("no_refresh_token", "Google sent no refresh token; connect again")
        now = datetime.now(UTC)
        access = str(token["access_token"])
        profile = await http.get(
            API + "profile", headers={"Authorization": f"Bearer {access}"}, timeout=15.0
        )
        if profile.status_code != 200:
            raise ConnectError("profile_failed", f"Gmail profile: HTTP {profile.status_code}")
        email = str(profile.json()["emailAddress"]).lower()
        expires = _expires_at(token.get("expires_in"), now)
        return ConnectResult(
            credentials={
                "access_token": access,
                "refresh_token": str(token["refresh_token"]),
                "expires_at": expires.isoformat(),
                "email": email,
            },
            account_identity=email,
            self_ids=[email],
            scopes=sorted(granted),
            access_expires_at=expires,
        )

    async def revoke(self) -> None:
        creds = self.ctx.credentials or {}
        token = creds.get("refresh_token") or creds.get("access_token")
        if token:
            await self.ctx.http.post(REVOKE_URL, data={"token": token}, timeout=5.0)

    # ------------------------------------------------------------------ tokens + HTTP

    def _creds(self) -> dict[str, Any]:
        if not self.ctx.credentials:
            raise AuthExpiredError("Gmail is not connected")
        return self.ctx.credentials

    async def _refresh(self) -> str:
        creds = self._creds()
        settings = self.ctx.app_settings
        secret = settings.google_client_secret
        resp = await self.ctx.http.post(
            TOKEN_URL,
            data={
                "client_id": settings.google_client_id or "",
                "client_secret": secret.get_secret_value() if secret else "",
                "refresh_token": creds.get("refresh_token", ""),
                "grant_type": "refresh_token",
            },
            timeout=15.0,
        )
        if resp.status_code >= 500:
            raise TransientConnectorError(f"Google token refresh: HTTP {resp.status_code}")
        body = resp.json()
        if resp.status_code != 200:
            if body.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise AuthExpiredError(
                    "Google access expired or was revoked (Testing mode lasts 7 days); "
                    "reconnect Gmail"
                )
            raise TransientConnectorError(f"Google token refresh failed: {body.get('error')}")
        updated = {
            **creds,
            "access_token": str(body["access_token"]),
            "expires_at": _expires_at(body.get("expires_in"), self.ctx.clock()).isoformat(),
        }
        if body.get("refresh_token"):
            updated["refresh_token"] = str(body["refresh_token"])
        self.ctx.credentials = updated
        await self.ctx.save_credentials(updated)
        return str(updated["access_token"])

    async def _access_token(self) -> str:
        creds = self._creds()
        expires = creds.get("expires_at")
        if not creds.get("access_token") or not expires:
            return await self._refresh()
        if datetime.fromisoformat(str(expires)) - self.ctx.clock() < timedelta(
            seconds=REFRESH_MARGIN_S
        ):
            return await self._refresh()
        return str(creds["access_token"])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token()
        for attempt in range(2):
            try:
                resp = await self.ctx.http.request(
                    method,
                    API + path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20.0,
                )
            except httpx.TimeoutException as exc:
                raise TransientConnectorError(f"Gmail {path} timed out") from exc
            if resp.status_code == 401 and attempt == 0:
                token = await self._refresh()  # access token expired early or was rotated
                continue
            break
        if resp.status_code >= 400:
            # Google's error status/message never contain the token; they say what went wrong.
            reason = _google_error(resp)
            self.ctx.log.warning(
                "gmail_api_error",
                method=method,
                url=API + path,
                status=resp.status_code,
                reason=reason,
            )
        if resp.status_code == 401:
            raise AuthExpiredError("Gmail rejected the access token; reconnect Gmail")
        if resp.status_code == 404:
            raise _NotFoundError(f"Gmail {method} {path}: HTTP 404 {reason}".rstrip())
        if resp.status_code == 429 or (
            resp.status_code == 403 and "rateLimitExceeded" in resp.text
        ):
            retry = float(resp.headers.get("retry-after", "60") or 60)
            raise RateLimitedError("Gmail rate limit", retry_after=retry)
        if resp.status_code >= 500:
            raise TransientConnectorError(f"Gmail {path}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentConnectorError(
                f"Gmail {method} {path}: HTTP {resp.status_code} {reason}".rstrip()
            )
        data: dict[str, Any] = resp.json()
        return data

    # ------------------------------------------------------------------ fetch

    def _self(self) -> str:
        return str(self._creds().get("email", "")).lower()

    def _normalize(self, item: Mapping[str, Any]) -> NormalizedMessage | None:
        labels = set(item.get("labelIds") or [])
        if labels & SKIP_LABELS:
            return None
        payload = item.get("payload") or {}
        headers = header_map(payload)
        sender_id, sender_name = parse_sender(headers.get("from", ""))
        if not sender_id:
            return None
        subject = headers.get("subject") or None
        body = gmail_body_text(payload) or str(item.get("snippet") or "")
        received = datetime.fromtimestamp(int(item.get("internalDate", 0)) / 1000, UTC)
        thread = str(item.get("threadId") or item["id"])
        return NormalizedMessage(
            source="gmail",
            external_id=str(item["id"]),
            thread_id=thread,
            sender=Sender(id=sender_id, name=sender_name),
            body=body,
            received_at=received,
            direction="outbound" if sender_id == self._self() else "inbound",
            subject=subject,
            thread_title=subject or "(no subject)",
            permalink=f"https://mail.google.com/mail/u/0/#all/{thread}",
            reply_meta={
                "mailbox": self._self(),  # which Gmail account received it (see send())
                "message_id": headers.get("message-id"),
                "references": headers.get("references"),
                "reply_to": reply_recipient(headers),
                "subject": subject,
            },
            raw={
                "labelIds": sorted(labels),
                "headers": {
                    k: headers[k] for k in ("list-unsubscribe", "precedence") if k in headers
                },
            },
        )

    async def _get_messages(self, ids: list[str]) -> list[NormalizedMessage]:
        out: list[NormalizedMessage] = []
        for mid in ids[:MAX_PER_POLL]:
            try:
                item = await self._request("GET", f"messages/{mid}", params={"format": "full"})
            except _NotFoundError:
                continue  # deleted between listing and fetching
            m = self._normalize(item)
            if m is not None:
                out.append(m)
        return out

    async def _list(self, query: str, limit: int) -> list[str]:
        data = await self._request(
            "GET", "messages", params={"q": query, "maxResults": min(limit, 100)}
        )
        return [str(m["id"]) for m in data.get("messages") or []]

    async def _fresh_start(self, days: int, hours: int | None = None) -> FetchResult:
        profile = await self._request("GET", "profile")  # history id first: nothing is missed
        history_id = str(profile["historyId"])
        settings = self.ctx.app_settings
        ids = await self._list(f"{INBOX_QUERY} newer_than:{days}d", settings.backfill_max)
        messages = await self._get_messages(ids)
        if hours is not None:
            cutoff = self.ctx.clock() - timedelta(hours=hours)
            messages = [m for m in messages if m.received_at >= cutoff]
        return FetchResult(messages=messages, cursor=json.dumps({"history_id": history_id}))

    async def fetch_new(self, since: str | None) -> FetchResult:
        hours = self.ctx.app_settings.backfill_hours
        if since is None:
            return await self._fresh_start(max(1, math.ceil(hours / 24)), hours)
        start = str(json.loads(since)["history_id"])
        ids: list[str] = []
        page: str | None = None
        latest = start
        try:
            for _ in range(3):
                data = await self._request(
                    "GET",
                    "history",
                    params={
                        "startHistoryId": start,
                        "historyTypes": "messageAdded",
                        "labelId": "INBOX",
                        "maxResults": 100,
                        **({"pageToken": page} if page else {}),
                    },
                )
                latest = str(data.get("historyId") or latest)
                for record in data.get("history") or []:
                    for added in record.get("messagesAdded") or []:
                        msg = added.get("message") or {}
                        mid = str(msg.get("id") or "")
                        if mid and "INBOX" in (msg.get("labelIds") or []) and mid not in ids:
                            ids.append(mid)
                page = data.get("nextPageToken")
                if not page:
                    break
        except _NotFoundError:
            # history id too old (about a week offline): resync the last day
            return await self._fresh_start(FALLBACK_DAYS)
        messages = await self._get_messages(ids)
        return FetchResult(messages=messages, cursor=json.dumps({"history_id": latest}))

    # ------------------------------------------------------------------ send

    @classmethod
    def reply_blocked_reason(cls, *, sender_id: str, reply_meta: Mapping[str, Any]) -> str | None:
        """Never reply to no-reply or app-notification mail, or to a no-reply Reply-To."""
        if is_no_reply_address(sender_id):
            return f"{sender_id} is an automated no-reply sender; Hubline never replies to it"
        to_addr = str(reply_meta.get("reply_to") or "")
        if to_addr and is_no_reply_address(to_addr):
            return f"the reply address {to_addr} is a no-reply address; Hubline never sends to it"
        return None

    async def send(self, thread_id: str, text: str, *, reply_to: ReplyContext) -> SendResult:
        meta = reply_to.reply_meta
        blocked = self.reply_blocked_reason(sender_id=reply_to.recipient.id, reply_meta=meta)
        if blocked:
            raise PermanentConnectorError(blocked)
        to_addr = str(meta.get("reply_to") or reply_to.recipient.id)
        if "@" not in to_addr:
            raise PermanentConnectorError("this email has no address to reply to")
        if is_no_reply_address(to_addr):
            raise PermanentConnectorError(f"{to_addr} is a no-reply address; not sending")
        mailbox = str(meta.get("mailbox") or "").lower()
        me = self._self()
        if mailbox and me and mailbox != me:
            raise PermanentConnectorError(
                f"this email was received by {mailbox}, but Gmail is now connected as {me}; "
                f"reconnect Gmail as {mailbox} to reply"
            )
        raw = build_reply(
            from_addr=self._self(),
            to_addr=to_addr,
            subject=meta.get("subject") or reply_to.subject,
            body=text,
            in_reply_to=meta.get("message_id"),
            references=meta.get("references"),
        )
        try:
            sent = await self._request(
                "POST", "messages/send", json_body={"raw": raw, "threadId": thread_id}
            )
        except _NotFoundError as exc:
            raise PermanentConnectorError(
                f"Gmail can't find this conversation in {me or 'the connected account'} ({exc}). "
                "It was deleted, or Gmail is connected to a different account than the one "
                "that received it."
            ) from exc
        return SendResult(
            external_id=str(sent["id"]),
            thread_id=str(sent.get("threadId") or thread_id),
            sent_at=self.ctx.clock(),
        )
