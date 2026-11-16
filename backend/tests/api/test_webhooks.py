"""POST /webhooks/{key}: verification, ingest, inactive drops, echo dedup, approve round trip."""

from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentJob, Message
from tests.connectors import payloads as p
from tests.helpers import all_messages, get_account

pytestmark = pytest.mark.db


async def post_event(
    client: httpx.AsyncClient, payload: Any, *, headers: dict[str, str] | None = None, **kw: Any
) -> Response:
    body = p.json_body(payload)
    sent = {**p.slack_signed_headers(body, **kw), **(headers or {})}
    return await client.post("/webhooks/slack", content=body, headers=sent)


async def connect_slack(client: httpx.AsyncClient, *, enable: bool = True) -> None:
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        resp = await client.post(
            "/api/connectors/slack/credentials", json={"fields": {"token": p.SLACK_TOKEN}}
        )
    assert resp.status_code == 200, resp.text
    if enable:
        assert (
            await client.patch("/api/connectors/slack", json={"enabled": True})
        ).status_code == 200


async def test_unknown_and_non_webhook_connectors_404(connector_client: httpx.AsyncClient) -> None:
    assert (await connector_client.post("/webhooks/nope", content=b"{}")).status_code == 404
    assert (await connector_client.post("/webhooks/demo", content=b"{}")).status_code == 404
    assert (await connector_client.post("/webhooks/gmail", content=b"{}")).status_code == 404


async def test_unconfigured_slack_is_503(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post("/webhooks/slack", content=b"{}")
    assert resp.status_code == 503


async def test_url_verification_works_before_connecting(
    connector_client: httpx.AsyncClient,
) -> None:
    resp = await post_event(connector_client, {"type": "url_verification", "challenge": "c-123"})
    assert resp.status_code == 200
    assert resp.text == "c-123"
    assert resp.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "kw",
    [
        {"secret": "wrong-secret"},
        {"ts": int(time.time()) - 600},
    ],
)
async def test_forged_or_stale_requests_are_401_and_store_nothing(
    connector_client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    kw: dict[str, Any],
) -> None:
    await connect_slack(connector_client)
    resp = await post_event(connector_client, p.slack_event(p.slack_message()), **kw)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert await all_messages(sessionmaker) == []


async def test_missing_signature_is_401(connector_client: httpx.AsyncClient) -> None:
    resp = await connector_client.post("/webhooks/slack", content=b'{"type":"event_callback"}')
    assert resp.status_code == 401


async def test_dm_event_is_ingested_and_queued_for_the_agent(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await connect_slack(connector_client)
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        resp = await post_event(connector_client, p.slack_event(p.slack_message()))
        # Slack retries the same event (e.g. after a slow first answer): harmless
        retry = await post_event(
            connector_client,
            p.slack_event(p.slack_message()),
            headers={"x-slack-retry-num": "1", "x-slack-retry-reason": "http_timeout"},
        )
    assert resp.status_code == retry.status_code == 200
    [m] = await all_messages(sessionmaker)
    assert m.source == "slack"
    assert m.sender_name == "Alice Chen"
    assert m.direction == "inbound"
    async with sessionmaker() as s:
        jobs = (await s.execute(select(AgentJob))).scalars().all()
    assert [j.message_id for j in jobs] == [m.id]


async def test_events_are_dropped_while_disconnected_or_disabled(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    resp = await post_event(connector_client, p.slack_event(p.slack_message()))
    assert resp.status_code == 200  # acknowledged so Slack doesn't retry
    await connect_slack(connector_client, enable=False)
    resp = await post_event(connector_client, p.slack_event(p.slack_message()))
    assert resp.status_code == 200
    assert await all_messages(sessionmaker) == []


async def test_own_messages_are_stored_as_outbound_without_a_job(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await connect_slack(connector_client)
    await post_event(connector_client, p.slack_event(p.slack_message("on it", user=p.ME)))
    [m] = await all_messages(sessionmaker)
    assert m.direction == "outbound"
    async with sessionmaker() as s:
        assert (await s.execute(select(AgentJob))).first() is None


async def test_approve_posts_into_the_dm_and_the_echo_is_deduped(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await connect_slack(connector_client)
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        await post_event(connector_client, p.slack_event(p.slack_message()))
    [incoming] = await all_messages(sessionmaker)

    with respx.mock(assert_all_called=False) as router:
        post = router.post("https://slack.com/api/chat.postMessage").mock(
            return_value=Response(
                200, json={"ok": True, "channel": p.DM, "ts": "1762690000.000300"}
            )
        )
        resp = await connector_client.post(
            f"/api/messages/{incoming.id}/approve",
            json={"body": "Sending it now.", "idempotency_key": str(uuid.uuid4())},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"
    form = parse_qs(post.calls.last.request.content.decode())
    assert form["channel"] == [p.DM]
    assert "thread_ts" not in form  # a plain DM reply stays in the DM

    # Slack then delivers our own reply as an event: same ts -> no duplicate, no triage
    echo = p.slack_message("Sending it now.", user=p.ME, ts="1762690000.000300")
    assert (await post_event(connector_client, p.slack_event(echo))).status_code == 200
    rows = await all_messages(sessionmaker)
    assert [(m.direction, m.body) for m in rows] == [
        ("inbound", "Can you send me the Q3 deck by 3pm?"),
        ("outbound", "Sending it now."),
    ]
    assert rows[0].status == "replied"
    assert rows[1].thread_id == rows[0].thread_id


async def test_revoked_token_on_send_marks_needs_reauth(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await connect_slack(connector_client)
    with respx.mock(assert_all_called=False) as router:
        p.mock_slack_basics(router)
        await post_event(connector_client, p.slack_event(p.slack_message()))
    [incoming] = await all_messages(sessionmaker)
    with respx.mock(assert_all_called=False) as router:
        router.post("https://slack.com/api/chat.postMessage").mock(
            return_value=Response(200, json={"ok": False, "error": "token_revoked"})
        )
        resp = await connector_client.post(
            f"/api/messages/{incoming.id}/approve",
            json={"body": "ok", "idempotency_key": str(uuid.uuid4())},
        )
    assert resp.status_code == 502
    assert (await get_account(sessionmaker, "slack")).status == "needs_reauth"
    async with sessionmaker() as s:
        status = (await s.execute(select(Message.status).where(Message.id == incoming.id))).scalar()
    assert status == "open"  # the draft stays; nothing is resent automatically


async def test_undecryptable_credentials_on_poll_mark_needs_reauth(
    connector_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from tests.helpers import make_due

    await connect_slack(connector_client)
    async with sessionmaker() as s, s.begin():
        await s.execute(text("UPDATE connector_credentials SET ciphertext = 'garbage'::bytea"))
    await make_due(sessionmaker, "slack")
    runtime = connector_client.app.state.runtime  # type: ignore[attr-defined]
    await runtime.runner.tick()
    account = await get_account(sessionmaker, "slack")
    assert account.status == "needs_reauth"
    assert "reconnect" in (account.last_error or "")
