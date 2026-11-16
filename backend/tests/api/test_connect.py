"""Connect lifecycle over the API: token paste, OAuth start/callback, disconnect, no leaks."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.connectors.gmail import SCOPES, TOKEN_URL
from app.models import ConnectorCredential, OAuthState
from tests.connectors import payloads as p
from tests.helpers import get_account, get_state

pytestmark = pytest.mark.db

SECRETS = (p.SLACK_TOKEN, "ya29.new", "1//new-refresh", "4/the-code")


def assert_no_secrets(*blobs: Any) -> None:
    dump = json.dumps(blobs, default=str)
    for secret in SECRETS:
        assert secret not in dump


async def paste_slack_token(client: httpx.AsyncClient, token: str = p.SLACK_TOKEN) -> Response:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        return await client.post(
            "/api/connectors/slack/credentials", json={"fields": {"token": token}}
        )


# ----------------------------------------------------------------------------- listing


async def test_connectors_describe_how_to_connect(connector_client: httpx.AsyncClient) -> None:
    resp = await connector_client.get("/api/connectors")
    by_key = {c["key"]: c for c in resp.json()}
    assert by_key["slack"]["connect_method"] == "token"
    assert by_key["slack"]["token_fields"][0]["name"] == "token"
    assert by_key["gmail"]["connect_method"] == "oauth"
    assert by_key["gmail"]["token_fields"] == []
    assert by_key["demo"]["connect_method"] == "none"
    assert by_key["slack"]["status"] == by_key["gmail"]["status"] == "disconnected"


async def test_unconfigured_connectors_say_which_env_is_missing(
    app_client: httpx.AsyncClient,
) -> None:
    by_key = {c["key"]: c for c in (await app_client.get("/api/connectors")).json()}
    assert by_key["slack"]["status"] == "not_configured"
    assert set(by_key["slack"]["missing_env"]) == {
        "SLACK_SIGNING_SECRET",
        "CREDENTIALS_ENCRYPTION_KEY",
    }
    resp = await app_client.post("/api/connectors/gmail/oauth/start")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "not_configured"


# ----------------------------------------------------------------------------- token paste


async def test_slack_token_paste_connects_and_stores_it_encrypted(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with capture_logs() as logs:
        resp = await paste_slack_token(connector_client)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "connected"
    assert out["account_identity"] == "Hubline Demo / dhruv"
    assert_no_secrets(out, logs, (await connector_client.get("/api/connectors")).json())

    account = await get_account(sessionmaker, "slack")
    assert account.self_ids == [p.ME]
    async with sessionmaker() as s:
        cred = (await s.execute(select(ConnectorCredential))).scalar_one()
    assert p.SLACK_TOKEN.encode() not in bytes(cred.ciphertext)
    assert "chat:write" in cred.scopes


@pytest.mark.parametrize(
    ("token", "code"), [("xoxb-bot", "invalid_token"), ("not-a-token", "invalid_token")]
)
async def test_bad_tokens_are_rejected_with_a_clear_code(
    connector_client: httpx.AsyncClient, token: str, code: str
) -> None:
    resp = await paste_slack_token(connector_client, token)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code
    assert token not in resp.text


async def test_token_paste_rejects_unknown_fields_and_non_token_apps(
    connector_client: httpx.AsyncClient,
) -> None:
    bad = await connector_client.post(
        "/api/connectors/slack/credentials", json={"fields": {"password": "x"}}
    )
    assert bad.status_code == 422
    gmail = await connector_client.post(
        "/api/connectors/gmail/credentials", json={"fields": {"token": "x"}}
    )
    assert gmail.status_code == 409


async def test_slack_down_is_a_502(connector_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post("https://slack.com/api/auth.test").mock(return_value=Response(503))
        resp = await connector_client.post(
            "/api/connectors/slack/credentials", json={"fields": {"token": p.SLACK_TOKEN}}
        )
    assert resp.status_code == 502


async def test_reconnecting_a_different_account_resets_the_cursor(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await paste_slack_token(connector_client)
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE sync_state SET cursor = :c"), {"c": '{"v":1}'})
    await paste_slack_token(connector_client)  # same identity: cursor kept
    assert (await get_state(sessionmaker, "slack")).cursor == '{"v":1}'
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE connector_accounts SET account_identity = 'someone else'"))
    await paste_slack_token(connector_client)
    assert (await get_state(sessionmaker, "slack")).cursor is None


# ----------------------------------------------------------------------------- OAuth


async def start_gmail(client: httpx.AsyncClient) -> dict[str, str]:
    resp = await client.post("/api/connectors/gmail/oauth/start")
    assert resp.status_code == 200, resp.text
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.json()["authorize_url"]).query).items()}


async def callback(client: httpx.AsyncClient, **params: str) -> Response:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "ya29.new",
                    "expires_in": 3599,
                    "refresh_token": "1//new-refresh",
                    "scope": params.pop("_scope", " ".join(SCOPES)),
                },
            )
        )
        p.mock_gmail_basics(router)
        return await client.get("/oauth/gmail/callback", params=params, headers={})


async def test_oauth_start_stores_state_and_uses_the_exact_redirect(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    q = await start_gmail(connector_client)
    assert q["redirect_uri"] == "http://localhost:8000/oauth/gmail/callback"
    assert q["code_challenge_method"] == "S256"
    async with sessionmaker() as s:
        state = await s.get(OAuthState, q["state"])
    assert state is not None
    assert state.source == "gmail"
    assert state.consumed_at is None
    assert len(state.code_verifier) >= 43


async def test_oauth_callback_connects_gmail_and_redirects_to_the_dashboard(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    q = await start_gmail(connector_client)
    with capture_logs() as logs:
        resp = await callback(connector_client, code="4/the-code", state=q["state"])
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://localhost:3000/connectors?connected=gmail"
    account = await get_account(sessionmaker, "gmail")
    assert account.status == "connected"
    assert account.account_identity == p.GMAIL_ME
    assert_no_secrets(logs, (await connector_client.get("/api/connectors")).json())

    # the state is single-use
    again = await callback(connector_client, code="4/the-code", state=q["state"])
    assert again.headers["location"].endswith("error=invalid_state&connector=gmail")


async def test_reconnect_preselects_the_connected_gmail_account(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    first = await start_gmail(connector_client)
    assert "login_hint" not in first  # never connected: the user picks the account
    q = await start_gmail(connector_client)
    await callback(connector_client, code="4/the-code", state=q["state"])

    assert (await start_gmail(connector_client))["login_hint"] == p.GMAIL_ME  # connected
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE connector_accounts SET status = 'needs_reauth'"))
    assert (await start_gmail(connector_client))["login_hint"] == p.GMAIL_ME  # login expired

    with respx.mock(assert_all_called=False) as router:
        router.post("https://oauth2.googleapis.com/revoke").mock(return_value=Response(200))
        await connector_client.post("/api/connectors/gmail/disconnect")
    assert "login_hint" not in await start_gmail(connector_client)  # after Disconnect: no hint


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"code": "c", "state": "never-issued"}, "invalid_state"),
        ({"code": "c"}, "invalid_state"),
        ({"error": "access_denied", "state": "<issued>"}, "access_denied"),
        ({"code": "c", "state": "<issued>", "_scope": SCOPES[0]}, "missing_scope"),
    ],
)
async def test_oauth_callback_failures_redirect_with_an_error(
    connector_client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    params: dict[str, str],
    error: str,
) -> None:
    params = dict(params)
    if params.get("state") == "<issued>":
        params["state"] = (await start_gmail(connector_client))["state"]
    resp = await callback(connector_client, **params)
    assert resp.status_code == 302
    assert resp.headers["location"] == (
        f"http://localhost:3000/connectors?error={error}&connector=gmail"
    )
    assert (await get_account(sessionmaker, "gmail")).status == "disconnected"


async def test_expired_state_is_rejected(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    q = await start_gmail(connector_client)
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE oauth_states SET expires_at = now() - interval '1 second'"))
    resp = await callback(connector_client, code="c", state=q["state"])
    assert "error=invalid_state" in resp.headers["location"]


async def test_state_for_another_connector_is_rejected(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    q = await start_gmail(connector_client)
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE oauth_states SET source = 'slack'"))
    resp = await callback(connector_client, code="c", state=q["state"])
    assert "error=invalid_state" in resp.headers["location"]


async def test_oauth_start_is_only_for_oauth_connectors(
    connector_client: httpx.AsyncClient,
) -> None:
    assert (await connector_client.post("/api/connectors/slack/oauth/start")).status_code == 409
    assert (await connector_client.post("/api/connectors/nope/oauth/start")).status_code == 404


async def test_unknown_connector_callback_redirects_with_error(
    connector_client: httpx.AsyncClient,
) -> None:
    resp = await connector_client.get("/oauth/nope/callback", params={"code": "c", "state": "s"})
    assert "error=unknown_connector" in resp.headers["location"]


# ----------------------------------------------------------------------------- disconnect


async def test_disconnect_revokes_deletes_credentials_and_disables(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await paste_slack_token(connector_client)
    await connector_client.patch("/api/connectors/slack", json={"enabled": True})
    async with sessionmaker() as s, s.begin():  # an error left over from the old account
        await s.execute(text("UPDATE connector_accounts SET last_error = 'token revoked'"))
        await s.execute(text("UPDATE sync_state SET last_error = 'token revoked'"))
    with respx.mock(assert_all_called=False) as router:
        revoke = router.post("https://slack.com/api/auth.revoke").mock(
            return_value=Response(200, json={"ok": True, "revoked": True})
        )
        resp = await connector_client.post("/api/connectors/slack/disconnect")
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "disconnected"
    assert out["enabled"] is False
    assert out["account_identity"] is None
    assert out["last_error"] is None
    assert revoke.call_count == 1
    async with sessionmaker() as s:
        assert (await s.execute(select(ConnectorCredential))).first() is None
    account = await get_account(sessionmaker, "slack")
    assert account.self_ids == []
    assert account.last_error is None
    assert (await get_state(sessionmaker, "slack")).last_error is None


async def test_disconnect_still_works_when_revoke_fails(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await paste_slack_token(connector_client)
    with respx.mock(assert_all_called=False) as router:
        router.post("https://slack.com/api/auth.revoke").mock(side_effect=httpx.ConnectError("x"))
        resp = await connector_client.post("/api/connectors/slack/disconnect")
    assert resp.json()["status"] == "disconnected"


async def test_demo_has_nothing_to_disconnect(connector_client: httpx.AsyncClient) -> None:
    assert (await connector_client.post("/api/connectors/demo/disconnect")).status_code == 409
