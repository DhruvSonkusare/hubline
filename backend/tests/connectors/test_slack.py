"""Slack connector: signature verification, event parsing, token connect, send, paced polling."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
import structlog
from httpx import Response

from app.connectors import slack as slack_mod
from app.connectors.base import (
    AuthExpiredError,
    ConnectError,
    ConnectorContext,
    RateLimitedError,
    WebhookVerificationError,
)
from app.connectors.models import ReplyContext, Sender, WebhookRequest
from app.connectors.slack import SlackConnector, to_plain_text, verify_signature
from tests.conftest import make_connector_settings
from tests.connectors import payloads as p

API = "https://slack.com/api/"


def make_slack(
    http: httpx.AsyncClient, *, connected: bool = True, now: Any = None
) -> SlackConnector:
    ctx = ConnectorContext(
        account_id=uuid.uuid4(),
        source="slack",
        settings=None,
        self_ids=frozenset({p.ME}) if connected else frozenset(),
        http=http,
        app_settings=make_connector_settings(),
        log=structlog.get_logger("test"),
        credentials=p.slack_credentials() if connected else None,
        clock=(lambda: now) if now is not None else (lambda: p.FIXED_NOW),
    )
    return SlackConnector(ctx)


def signed_request(payload: Any, **kw: Any) -> WebhookRequest:
    body = p.json_body(payload)
    return WebhookRequest("POST", "/webhooks/slack", p.slack_signed_headers(body, **kw), {}, body)


@pytest.fixture(autouse=True)
def _clear_name_cache() -> None:
    slack_mod._name_cache.clear()


# ----------------------------------------------------------------------------- signatures


def test_valid_signature_passes() -> None:
    body = b'{"type":"url_verification"}'
    verify_signature(p.slack_signed_headers(body), body, p.SIGNING_SECRET, now=time.time())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h, b: ({**h, "x-slack-signature": "v0=" + "0" * 64}, b),  # forged
        lambda h, b: (h, b + b" "),  # body changed after signing
        lambda h, b: ({k: v for k, v in h.items() if k != "x-slack-signature"}, b),
        lambda h, b: ({**h, "x-slack-request-timestamp": "nope"}, b),
    ],
)
def test_bad_signatures_are_rejected(mutate: Any) -> None:
    body = b'{"type":"event_callback"}'
    headers, body2 = mutate(p.slack_signed_headers(body), body)
    with pytest.raises(WebhookVerificationError):
        verify_signature(headers, body2, p.SIGNING_SECRET, now=time.time())


@pytest.mark.parametrize("skew", [-301, 301])
def test_stale_or_future_timestamps_are_rejected(skew: int) -> None:
    body = b"{}"
    now = time.time()
    headers = p.slack_signed_headers(body, ts=int(now) + skew)
    with pytest.raises(WebhookVerificationError, match="stale"):
        verify_signature(headers, body, p.SIGNING_SECRET, now=now)


def test_wrong_secret_is_rejected() -> None:
    body = b"{}"
    with pytest.raises(WebhookVerificationError):
        verify_signature(
            p.slack_signed_headers(body, secret="other"), body, p.SIGNING_SECRET, now=time.time()
        )


# ----------------------------------------------------------------------------- parsing


async def test_url_verification_echoes_challenge_even_when_not_connected() -> None:
    async with httpx.AsyncClient() as http:
        result = await make_slack(http, connected=False).parse_webhook(
            signed_request({"type": "url_verification", "challenge": "abc123"})
        )
    assert result.response is not None
    assert result.response.body == b"abc123"
    assert result.response.content_type == "text/plain"
    assert result.messages == []


async def test_dm_is_normalized_with_sender_name() -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        async with httpx.AsyncClient() as http:
            result = await make_slack(http).parse_webhook(
                signed_request(p.slack_event(p.slack_message()))
            )
    [m] = result.messages
    assert m.source == "slack"
    assert m.external_id == f"{p.TEAM}:{p.DM}:1762689600.000100"
    assert m.thread_id == f"{p.TEAM}:{p.DM}"  # the whole DM is one conversation
    assert m.sender == Sender(id=p.ALICE, name="Alice Chen")
    assert m.direction == "inbound"
    assert m.body == "Can you send me the Q3 deck by 3pm?"
    assert m.reply_meta == {"channel": p.DM, "thread_ts": None, "team": p.TEAM}
    assert m.permalink == f"https://hublinedemo.slack.com/archives/{p.DM}/p1762689600000100"
    assert m.thread_title == "Slack · Direct message"


async def test_threaded_dm_reply_gets_its_own_thread() -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        async with httpx.AsyncClient() as http:
            event = p.slack_message(ts="1762689700.000200", thread_ts="1762689600.000100")
            result = await make_slack(http).parse_webhook(signed_request(p.slack_event(event)))
    [m] = result.messages
    assert m.thread_id == f"{p.TEAM}:{p.DM}:1762689600.000100"
    assert m.reply_meta["thread_ts"] == "1762689600.000100"


async def test_group_dm_is_kept() -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        async with httpx.AsyncClient() as http:
            event = p.slack_message(channel=p.GROUP_DM, channel_type="mpim")
            result = await make_slack(http).parse_webhook(signed_request(p.slack_event(event)))
    [m] = result.messages
    assert m.thread_title == "Slack · Group DM"


async def test_channel_mention_is_kept_and_replies_in_thread() -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        async with httpx.AsyncClient() as http:
            event = p.slack_message(
                f"<@{p.ME}> can you approve the release?", channel=p.CHANNEL, channel_type="channel"
            )
            result = await make_slack(http).parse_webhook(signed_request(p.slack_event(event)))
    [m] = result.messages
    assert m.body == "@you can you approve the release?"
    assert m.thread_id == f"{p.TEAM}:{p.CHANNEL}:1762689600.000100"
    assert m.reply_meta["thread_ts"] == "1762689600.000100"


@pytest.mark.parametrize("channel_type", ["channel", "group"])
async def test_channel_chatter_without_a_mention_is_dropped(channel_type: str) -> None:
    async with httpx.AsyncClient() as http:
        event = p.slack_message("lunch?", channel=p.CHANNEL, channel_type=channel_type)
        result = await make_slack(http).parse_webhook(signed_request(p.slack_event(event)))
    assert result.messages == []


@pytest.mark.parametrize(
    "extra",
    [
        {"subtype": "message_changed"},
        {"subtype": "message_deleted"},
        {"subtype": "channel_join"},
        {"subtype": "bot_message"},
        {"hidden": True},
        {"user": None},
        {"text": "   "},
    ],
)
async def test_ignored_messages(extra: dict[str, Any]) -> None:
    async with httpx.AsyncClient() as http:
        event = {**p.slack_message(), **extra}
        result = await make_slack(http).parse_webhook(signed_request(p.slack_event(event)))
    assert result.messages == []


async def test_own_message_is_outbound_and_needs_no_lookup() -> None:
    async with httpx.AsyncClient() as http:  # no respx: a users.info call would fail loudly
        event = p.slack_message("on it", user=p.ME)
        result = await make_slack(http).parse_webhook(signed_request(p.slack_event(event)))
    [m] = result.messages
    assert m.direction == "outbound"
    assert m.sender.name == "You"


async def test_installer_from_authorizations_counts_as_self_before_connecting() -> None:
    async with httpx.AsyncClient() as http:
        event = p.slack_message(f"<@{p.ME}> ping", channel=p.CHANNEL, channel_type="channel")
        result = await make_slack(http, connected=False).parse_webhook(
            signed_request(p.slack_event(event))
        )
    assert len(result.messages) == 1  # mention detected via the event's authorizations


async def test_slow_user_lookup_does_not_block_the_webhook() -> None:
    async def slow(_: httpx.Request) -> Response:
        await asyncio.sleep(5)
        return Response(200, json={"ok": True})

    with respx.mock(assert_all_called=False) as router:
        router.post(API + "users.info").mock(side_effect=slow)
        async with httpx.AsyncClient() as http:
            started = time.monotonic()
            result = await make_slack(http).parse_webhook(
                signed_request(p.slack_event(p.slack_message()))
            )
    assert time.monotonic() - started < 2.0
    assert result.messages[0].sender.name is None


async def test_other_event_types_are_ignored() -> None:
    async with httpx.AsyncClient() as http:
        result = await make_slack(http).parse_webhook(
            signed_request(p.slack_event({"type": "reaction_added", "user": p.ALICE}))
        )
        rate = await make_slack(http).parse_webhook(signed_request({"type": "app_rate_limited"}))
    assert result.messages == []
    assert rate.messages == []


def test_mrkdwn_to_plain_text() -> None:
    text = (
        f"hey <@{p.ME}> and <@{p.ALICE}|alice>: see <https://x.example/a|the doc> in "
        "<#C1|general> &amp; <!here> &lt;3"
    )
    assert to_plain_text(text, frozenset({p.ME})) == (
        f"hey @you and @{p.ALICE}: see the doc (https://x.example/a) in #general & @here <3"
    )


# ----------------------------------------------------------------------------- connect


async def test_connect_with_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        async with httpx.AsyncClient() as http:
            result = await SlackConnector.connect_with_token(
                {"token": p.SLACK_TOKEN}, http=http, app_settings=make_connector_settings()
            )
        sent = router.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {p.SLACK_TOKEN}"
    assert result.account_identity == "Hubline Demo / dhruv"
    assert result.self_ids == [p.ME]
    assert result.credentials["token"] == p.SLACK_TOKEN
    assert "chat:write" in result.scopes


@pytest.mark.parametrize("token", ["", "xoxb-bot-token", "xapp-1-app-token"])
async def test_connect_rejects_non_user_tokens(token: str) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ConnectError) as err:
            await SlackConnector.connect_with_token(
                {"token": token}, http=http, app_settings=make_connector_settings()
            )
    assert err.value.code == "invalid_token"


async def test_connect_rejects_revoked_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(API + "auth.test").mock(
            return_value=Response(200, json={"ok": False, "error": "invalid_auth"})
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ConnectError) as err:
                await SlackConnector.connect_with_token(
                    {"token": p.SLACK_TOKEN}, http=http, app_settings=make_connector_settings()
                )
    assert err.value.code == "invalid_token"


async def test_connect_reports_missing_scopes() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(API + "auth.test").mock(
            return_value=Response(
                200,
                json={"ok": True, "team_id": p.TEAM, "user_id": p.ME},
                headers={"x-oauth-scopes": "im:history,users:read"},
            )
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ConnectError) as err:
                await SlackConnector.connect_with_token(
                    {"token": p.SLACK_TOKEN}, http=http, app_settings=make_connector_settings()
                )
    assert err.value.code == "missing_scope"
    assert "chat:write" in str(err.value)


# ----------------------------------------------------------------------------- send


def reply_ctx(thread_ts: str | None) -> ReplyContext:
    return ReplyContext(
        in_reply_to_external_id=f"{p.TEAM}:{p.DM}:1762689600.000100",
        subject=None,
        recipient=Sender(id=p.ALICE),
        reply_meta={"channel": p.DM, "thread_ts": thread_ts, "team": p.TEAM},
    )


@pytest.mark.parametrize("thread_ts", [None, "1762689600.000100"])
async def test_send_posts_as_the_user(thread_ts: str | None) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(API + "chat.postMessage").mock(
            return_value=Response(
                200, json={"ok": True, "channel": p.DM, "ts": "1762690000.000300"}
            )
        )
        async with httpx.AsyncClient() as http:
            result = await make_slack(http).send(
                f"{p.TEAM}:{p.DM}", "Sending it now.", reply_to=reply_ctx(thread_ts)
            )
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["channel"] == [p.DM]
    assert form["text"] == ["Sending it now."]
    assert form.get("thread_ts") == ([thread_ts] if thread_ts else None)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {p.SLACK_TOKEN}"
    # the same id the echo event will carry, so dedup drops the echo
    assert result.external_id == f"{p.TEAM}:{p.DM}:1762690000.000300"


async def test_send_rate_limit_and_revoked_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(API + "chat.postMessage").mock(
            side_effect=[
                Response(429, headers={"retry-after": "7"}),
                Response(200, json={"ok": False, "error": "token_revoked"}),
            ]
        )
        async with httpx.AsyncClient() as http:
            slack = make_slack(http)
            with pytest.raises(RateLimitedError) as rate:
                await slack.send("t", "x", reply_to=reply_ctx(None))
            with pytest.raises(AuthExpiredError):
                await slack.send("t", "x", reply_to=reply_ctx(None))
    assert rate.value.retry_after == 7


# ----------------------------------------------------------------------------- paced polling


def _mock_listing(router: respx.Router, channels: list[str]) -> None:
    router.post(API + "conversations.list").mock(
        return_value=Response(
            200, json={"ok": True, "channels": [{"id": c, "is_im": True} for c in channels]}
        )
    )


async def test_poll_makes_at_most_one_history_call_and_rotates() -> None:
    dms = ["D1", "D2", "D3"]
    seen: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        _mock_listing(router, dms)

        def history(request: httpx.Request) -> Response:
            seen.append(parse_qs(request.content.decode())["channel"][0])
            return Response(200, json={"ok": True, "messages": []})

        hist = router.post(API + "conversations.history").mock(side_effect=history)
        cursor = None
        async with httpx.AsyncClient() as http:
            for i in range(4):
                now = p.FIXED_NOW + timedelta(minutes=5 * i)
                result = await make_slack(http, now=now).fetch_new(cursor)
                cursor = result.cursor
                assert hist.call_count == i + 1  # exactly one history call per poll
    assert seen == ["D1", "D2", "D3", "D1"]  # least recently checked first


async def test_poll_skips_history_within_a_minute_of_the_last_call() -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_listing(router, ["D1", "D2"])
        hist = router.post(API + "conversations.history").mock(
            return_value=Response(200, json={"ok": True, "messages": []})
        )
        async with httpx.AsyncClient() as http:
            first = await make_slack(http, now=p.FIXED_NOW).fetch_new(None)
            # e.g. "Sync now" clicked 30 s later: no history call at all
            second = await make_slack(http, now=p.FIXED_NOW + timedelta(seconds=30)).fetch_new(
                first.cursor
            )
            third = await make_slack(http, now=p.FIXED_NOW + timedelta(seconds=61)).fetch_new(
                second.cursor
            )
    assert hist.call_count == 2
    assert second.cursor == first.cursor
    assert third.cursor != first.cursor


async def test_poll_reads_since_the_last_seen_message() -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        async with httpx.AsyncClient() as http:
            first = await make_slack(http).fetch_new(None)
            hist = router["slack_history"]
            oldest_first = parse_qs(hist.calls.last.request.content.decode())["oldest"][0]
            await make_slack(http, now=p.FIXED_NOW + timedelta(minutes=5)).fetch_new(first.cursor)
            oldest_next = parse_qs(hist.calls.last.request.content.decode())["oldest"][0]
            limit = parse_qs(hist.calls.last.request.content.decode())["limit"][0]
    [m] = first.messages
    assert float(oldest_first) == pytest.approx((p.FIXED_NOW - timedelta(hours=24)).timestamp())
    assert oldest_next == m.external_id.rsplit(":", 1)[1]  # resumes after the newest seen
    assert limit == "15"
    assert json.loads(first.cursor or "{}")["latest"][p.DM] == oldest_next


async def test_poll_without_conversations_makes_no_history_call() -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_listing(router, [])
        hist = router.post(API + "conversations.history")
        async with httpx.AsyncClient() as http:
            result = await make_slack(http).fetch_new(None)
    assert hist.call_count == 0
    assert result.cursor is not None
