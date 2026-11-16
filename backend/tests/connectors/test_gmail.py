"""Gmail connector: OAuth URL + code exchange, token refresh, history polling, threaded send."""

from __future__ import annotations

import base64
import email
import json
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
import structlog
from httpx import Response

from app.connectors.base import AuthExpiredError, ConnectError, ConnectorContext, RateLimitedError
from app.connectors.gmail import SCOPES, TOKEN_URL, GmailConnector
from app.connectors.models import ReplyContext, Sender
from tests.conftest import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, make_connector_settings
from tests.connectors import payloads as p

API = p.GMAIL_API
REDIRECT = "http://localhost:8000/oauth/gmail/callback"


def make_gmail(
    http: httpx.AsyncClient, *, creds: dict[str, Any] | None = None, saved: list[Any] | None = None
) -> GmailConnector:
    async def save(data: dict[str, Any]) -> None:
        if saved is not None:
            saved.append(data)

    ctx = ConnectorContext(
        account_id=uuid.uuid4(),
        source="gmail",
        settings=None,
        self_ids=frozenset({p.GMAIL_ME}),
        http=http,
        app_settings=make_connector_settings(),
        log=structlog.get_logger("test"),
        credentials=creds if creds is not None else p.gmail_credentials(),
        save_credentials=save,
        clock=lambda: p.FIXED_NOW,
    )
    return GmailConnector(ctx)


def token_response(**extra: Any) -> Response:
    return Response(
        200,
        json={
            "access_token": "ya29.new",
            "expires_in": 3599,
            "refresh_token": "1//new-refresh",
            "scope": " ".join(SCOPES),
            "token_type": "Bearer",
            **extra,
        },
    )


# ----------------------------------------------------------------------------- OAuth


def test_authorize_url_uses_the_exact_redirect_uri_and_pkce() -> None:
    url = GmailConnector.authorize_url(
        state="st4te",
        code_challenge="ch4llenge",
        redirect_uri=REDIRECT,
        app_settings=make_connector_settings(),
    )
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert q == {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": "http://localhost:8000/oauth/gmail/callback",
        "response_type": "code",
        "scope": (
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/gmail.send"
        ),
        "access_type": "offline",
        "prompt": "consent",
        "state": "st4te",
        "code_challenge": "ch4llenge",
        "code_challenge_method": "S256",
    }


async def test_exchange_code_sends_verifier_and_reads_profile() -> None:
    with respx.mock(assert_all_called=False) as router:
        token = router.post(TOKEN_URL).mock(return_value=token_response())
        p.mock_gmail_basics(router)
        async with httpx.AsyncClient() as http:
            result = await GmailConnector.exchange_code(
                code="4/code",
                code_verifier="v" * 50,
                redirect_uri=REDIRECT,
                http=http,
                app_settings=make_connector_settings(),
            )
    form = parse_qs(token.calls.last.request.content.decode())
    assert form["code_verifier"] == ["v" * 50]
    assert form["client_secret"] == [GOOGLE_CLIENT_SECRET]
    assert form["redirect_uri"] == [REDIRECT]
    assert form["grant_type"] == ["authorization_code"]
    assert result.account_identity == p.GMAIL_ME
    assert result.self_ids == [p.GMAIL_ME]
    assert result.credentials["refresh_token"] == "1//new-refresh"
    assert result.access_expires_at is not None


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (token_response(scope=SCOPES[0]), "missing_scope"),  # send box unticked
        (token_response(refresh_token=None), "no_refresh_token"),
        (Response(400, json={"error": "invalid_grant"}), "token_exchange_failed"),
    ],
)
async def test_exchange_code_failures(response: Response, code: str) -> None:
    if response.status_code == 200:
        body = json.loads(response.content)
        response = Response(200, json={k: v for k, v in body.items() if v is not None})
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=response)
        p.mock_gmail_basics(router)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ConnectError) as err:
                await GmailConnector.exchange_code(
                    code="c",
                    code_verifier="v",
                    redirect_uri=REDIRECT,
                    http=http,
                    app_settings=make_connector_settings(),
                )
    assert err.value.code == code


# ----------------------------------------------------------------------------- tokens


async def test_access_token_is_refreshed_before_expiry_and_saved() -> None:
    saved: list[Any] = []
    with respx.mock(assert_all_called=False) as router:
        refresh = router.post(TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "ya29.fresh", "expires_in": 3599})
        )
        profile = router.get(API + "profile").mock(
            return_value=Response(200, json={"emailAddress": p.GMAIL_ME, "historyId": "5"})
        )
        router.get(API + "messages").mock(return_value=Response(200, json={}))
        async with httpx.AsyncClient() as http:
            gmail = make_gmail(http, creds=p.gmail_credentials(expires_in_s=30), saved=saved)
            await gmail.fetch_new(None)
    assert refresh.call_count == 1
    assert parse_qs(refresh.calls.last.request.content.decode())["grant_type"] == ["refresh_token"]
    assert profile.calls.last.request.headers["authorization"] == "Bearer ya29.fresh"
    assert saved
    assert saved[0]["access_token"] == "ya29.fresh"
    assert saved[0]["refresh_token"] == "1//test-refresh"  # kept when Google omits it


async def test_401_triggers_one_refresh_and_retry() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=Response(200, json={"access_token": "ya29.fresh", "expires_in": 3599})
        )
        profile = router.get(API + "profile").mock(
            side_effect=[
                Response(401),
                Response(200, json={"emailAddress": p.GMAIL_ME, "historyId": "5"}),
            ]
        )
        router.get(API + "messages").mock(return_value=Response(200, json={}))
        async with httpx.AsyncClient() as http:
            await make_gmail(http).fetch_new(None)
    assert profile.call_count == 2


async def test_revoked_refresh_token_needs_reauth() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=Response(400, json={"error": "invalid_grant"}))
        async with httpx.AsyncClient() as http:
            gmail = make_gmail(http, creds=p.gmail_credentials(expires_in_s=-10))
            with pytest.raises(AuthExpiredError, match="reconnect Gmail"):
                await gmail.fetch_new(None)


async def test_rate_limit_is_reported() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(API + "profile").mock(return_value=Response(429, headers={"retry-after": "9"}))
        async with httpx.AsyncClient() as http:
            with pytest.raises(RateLimitedError) as err:
                await make_gmail(http).fetch_new(None)
    assert err.value.retry_after == 9


# ----------------------------------------------------------------------------- fetch


async def test_first_fetch_backfills_inbox_and_returns_history_cursor() -> None:
    msgs = [
        p.gmail_message("a1"),
        p.gmail_message("a2", labels=("INBOX", "CATEGORY_PROMOTIONS")),
        p.gmail_message("a3", received=p.FIXED_NOW - timedelta(hours=72)),  # older than 48 h
        p.gmail_message("a4", sender=f"Me <{p.GMAIL_ME}>"),
    ]
    with respx.mock(assert_all_called=False) as router:
        p.mock_gmail_basics(router, msgs)
        async with httpx.AsyncClient() as http:
            result = await make_gmail(http).fetch_new(None)
        q = router["gmail_list"].calls.last.request.url.params["q"]
    assert q == "in:inbox -category:promotions -category:social newer_than:2d"
    assert [m.external_id for m in result.messages] == ["a1", "a4"]
    assert result.messages[1].direction == "outbound"
    assert json.loads(result.cursor or "{}") == {"history_id": "1000"}

    m = result.messages[0]
    assert m.sender == Sender(id="priya@acme.example", name="Priya Raman")
    assert m.subject == "Contract review"
    assert m.thread_id == "18c0000000000001"
    assert m.body.startswith("Hi Dhruv,")
    assert m.reply_meta["message_id"] == "<a1@mail.acme.example>"
    assert m.reply_meta["reply_to"] == "priya@acme.example"
    assert m.received_at == p.FIXED_NOW - timedelta(hours=2)


async def test_bulk_headers_reach_the_prefilter() -> None:
    msg = p.gmail_message(
        "n1",
        sender="Acme News <no-reply@news.acme.example>",
        extra_headers={"List-Unsubscribe": "<mailto:u@acme.example>", "Precedence": "bulk"},
    )
    with respx.mock(assert_all_called=False) as router:
        p.mock_gmail_basics(router, [msg])
        async with httpx.AsyncClient() as http:
            [m] = (await make_gmail(http).fetch_new(None)).messages
    assert m.raw is not None
    assert m.raw["headers"] == {
        "list-unsubscribe": "<mailto:u@acme.example>",
        "precedence": "bulk",
    }


async def test_incremental_fetch_uses_history() -> None:
    new = p.gmail_message("b1", thread="t-b")
    with respx.mock(assert_all_called=False) as router:
        history = router.get(API + "history").mock(
            return_value=Response(
                200,
                json={
                    "history": [
                        {
                            "id": "1001",
                            "messagesAdded": [{"message": {"id": "b1", "labelIds": ["INBOX"]}}],
                        },
                        {
                            "id": "1002",
                            "messagesAdded": [{"message": {"id": "s1", "labelIds": ["SENT"]}}],
                        },
                        {
                            "id": "1003",
                            "messagesAdded": [{"message": {"id": "b1", "labelIds": ["INBOX"]}}],
                        },
                    ],
                    "historyId": "1003",
                },
            )
        )
        router.get(API + "messages/b1").mock(return_value=Response(200, json=new))
        async with httpx.AsyncClient() as http:
            result = await make_gmail(http).fetch_new(json.dumps({"history_id": "1000"}))
    params = history.calls.last.request.url.params
    assert params["startHistoryId"] == "1000"
    assert params["historyTypes"] == "messageAdded"
    assert params["labelId"] == "INBOX"
    assert [m.external_id for m in result.messages] == ["b1"]  # deduped, SENT ignored
    assert json.loads(result.cursor or "{}") == {"history_id": "1003"}


async def test_expired_history_id_falls_back_to_the_last_day() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(API + "history").mock(return_value=Response(404))
        p.mock_gmail_basics(router, [p.gmail_message("c1")])
        async with httpx.AsyncClient() as http:
            result = await make_gmail(http).fetch_new(json.dumps({"history_id": "1"}))
        q = router["gmail_list"].calls.last.request.url.params["q"]
    assert "newer_than:1d" in q
    assert [m.external_id for m in result.messages] == ["c1"]
    assert json.loads(result.cursor or "{}") == {"history_id": "1000"}


# ----------------------------------------------------------------------------- send


def reply_ctx(reply_to: str = "priya@acme.example") -> ReplyContext:
    return ReplyContext(
        in_reply_to_external_id="a1",
        subject="Contract review",
        recipient=Sender(id="priya@acme.example"),
        reply_meta={
            "message_id": "<a1@mail.acme.example>",
            "references": "<root@mail.acme.example>",
            "reply_to": reply_to,
            "subject": "Contract review",
        },
    )


async def test_send_replies_in_thread_to_the_sender_only() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(API + "messages/send").mock(
            return_value=Response(200, json={"id": "sent1", "threadId": "18c0000000000001"})
        )
        async with httpx.AsyncClient() as http:
            result = await make_gmail(http).send(
                "18c0000000000001", "Will do by Friday.", reply_to=reply_ctx("team@acme.example")
            )
    body = json.loads(route.calls.last.request.content)
    assert body["threadId"] == "18c0000000000001"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert msg["From"] == p.GMAIL_ME
    assert msg["To"] == "team@acme.example"  # Reply-To wins over From
    assert msg["Subject"] == "Re: Contract review"
    assert msg["In-Reply-To"] == "<a1@mail.acme.example>"
    assert msg["References"] == "<root@mail.acme.example> <a1@mail.acme.example>"
    assert msg["Cc"] is None
    assert result.external_id == "sent1"


async def test_send_is_never_retried_on_server_errors() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(API + "messages/send").mock(return_value=Response(503))
        async with httpx.AsyncClient() as http:
            with pytest.raises(Exception, match="503"):
                await make_gmail(http).send("t", "x", reply_to=reply_ctx())
    assert route.call_count == 1


async def test_revoke_posts_the_refresh_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post("https://oauth2.googleapis.com/revoke").mock(return_value=Response(200))
        async with httpx.AsyncClient() as http:
            await make_gmail(http).revoke()
    assert parse_qs(route.calls.last.request.content.decode()) == {"token": ["1//test-refresh"]}
