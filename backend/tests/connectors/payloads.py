"""Slack and Gmail API payloads for tests, shaped like the providers' documented responses
(fields trimmed to what Hubline reads; all names, ids and addresses are fictional)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import respx
from httpx import Response

TEAM = "T0HUBLINE"
ME = "U0DHRUV"
ALICE = "U0ALICE"
DM = "D0ALICE"
GROUP_DM = "G0MPIM"
CHANNEL = "C0GENERAL"
SIGNING_SECRET = "test-signing-secret-0123456789abcdef"
SLACK_TOKEN = "xoxp-1111-2222-3333-test"
SLACK_SCOPES = (
    "im:history,im:read,mpim:history,mpim:read,channels:history,channels:read,"
    "groups:history,groups:read,users:read,chat:write"
)

GMAIL_ME = "demo.hubline@gmail.com"
FIXED_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------- Slack


def slack_credentials() -> dict[str, Any]:
    return {
        "token": SLACK_TOKEN,
        "team_id": TEAM,
        "team": "Hubline Demo",
        "user_id": ME,
        "user": "dhruv",
        "url": "https://hublinedemo.slack.com/",
    }


def slack_message(
    text: str = "Can you send me the Q3 deck by 3pm?",
    *,
    user: str = ALICE,
    ts: str = "1762689600.000100",
    channel: str = DM,
    channel_type: str = "im",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "message",
        "user": user,
        "text": text,
        "ts": ts,
        "channel": channel,
        "channel_type": channel_type,
        "event_ts": ts,
        **extra,
    }


def slack_event(event: dict[str, Any], *, installer: str = ME) -> dict[str, Any]:
    return {
        "token": "legacy-verification-token-unused",
        "team_id": TEAM,
        "api_app_id": "A0HUBLINE",
        "event": event,
        "type": "event_callback",
        "event_id": "Ev0TEST",
        "event_time": int(float(event.get("ts", "0"))),
        "authorizations": [{"team_id": TEAM, "user_id": installer, "is_bot": False}],
    }


def slack_signed_headers(
    body: bytes, *, secret: str = SIGNING_SECRET, ts: int | None = None
) -> dict[str, str]:
    stamp = str(int(time.time()) if ts is None else ts)
    sig = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256)
    return {
        "x-slack-request-timestamp": stamp,
        "x-slack-signature": "v0=" + sig.hexdigest(),
        "content-type": "application/json",
    }


def mock_slack_basics(router: respx.Router) -> None:
    """auth.test, users.info, a one-DM conversations.list and one history message."""
    router.post("https://slack.com/api/auth.test").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "url": "https://hublinedemo.slack.com/",
                "team": "Hubline Demo",
                "user": "dhruv",
                "team_id": TEAM,
                "user_id": ME,
            },
            headers={"x-oauth-scopes": SLACK_SCOPES},
        )
    )
    router.post("https://slack.com/api/users.info").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "user": {"id": ALICE, "name": "alice", "profile": {"display_name": "Alice Chen"}},
            },
        )
    )
    router.post("https://slack.com/api/conversations.list").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": DM, "is_im": True, "user": ALICE}],
                "response_metadata": {"next_cursor": ""},
            },
        )
    )
    router.post("https://slack.com/api/conversations.history", name="slack_history").mock(
        return_value=Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": ALICE,
                        "text": "Are we still on for the review?",
                        "ts": f"{(FIXED_NOW - timedelta(hours=1)).timestamp():.6f}",
                    }
                ],
                "has_more": False,
            },
        )
    )


# ----------------------------------------------------------------------------- Gmail


def gmail_credentials(*, expires_in_s: int = 3600, now: datetime = FIXED_NOW) -> dict[str, Any]:
    return {
        "access_token": "ya29.test-access",
        "refresh_token": "1//test-refresh",
        "expires_at": (now + timedelta(seconds=expires_in_s)).isoformat(),
        "email": GMAIL_ME,
    }


def b64(text: str | bytes) -> str:
    raw = text.encode() if isinstance(text, str) else text
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def gmail_message(
    mid: str = "18c0000000000001",
    *,
    thread: str = "18c0000000000001",
    sender: str = "Priya Raman <priya@acme.example>",
    subject: str = "Contract review",
    text: str | None = "Hi Dhruv,\n\nCan you review the contract by Friday?\n\nThanks,\nPriya",
    html: str | None = None,
    labels: tuple[str, ...] = ("INBOX", "UNREAD", "CATEGORY_PERSONAL"),
    received: datetime | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": GMAIL_ME},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{mid}@mail.acme.example>"},
    ] + [{"name": k, "value": v} for k, v in (extra_headers or {}).items()]
    parts = []
    if text is not None:
        parts.append(
            {
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}],
                "body": {"size": len(text), "data": b64(text)},
            }
        )
    if html is not None:
        parts.append(
            {
                "mimeType": "text/html",
                "headers": [{"name": "Content-Type", "value": 'text/html; charset="UTF-8"'}],
                "body": {"size": len(html), "data": b64(html)},
            }
        )
    when = received or (FIXED_NOW - timedelta(hours=2))
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": list(labels),
        "snippet": (text or "")[:100],
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "body": {"size": 0},
            "parts": parts,
        },
    }


GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me/"


def mock_gmail_basics(router: respx.Router, messages: list[dict[str, Any]] | None = None) -> None:
    messages = messages if messages is not None else [gmail_message()]
    router.get(GMAIL_API + "profile").mock(
        return_value=Response(
            200, json={"emailAddress": GMAIL_ME, "historyId": "1000", "messagesTotal": 3}
        )
    )
    router.get(GMAIL_API + "messages", name="gmail_list").mock(
        return_value=Response(
            200,
            json={
                "messages": [{"id": m["id"], "threadId": m["threadId"]} for m in messages],
                "resultSizeEstimate": len(messages),
            },
        )
    )
    for m in messages:
        router.get(GMAIL_API + f"messages/{m['id']}").mock(return_value=Response(200, json=m))


def json_body(data: Any) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()
