"""Step 3 message endpoints: detail, stats, draft edits, approve and send (at-most-once),
triage / redraft buttons, dismiss / reopen, the demo outbox and the stale-sending reaper."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors import registry
from app.connectors.base import AuthType, Capability, ConnectorDescriptor
from app.connectors.models import FetchResult, ReplyContext, SendResult
from app.models import AgentJob, Draft, Message, OutboundReply
from app.services.jobs import PRIORITY_USER
from app.services.replies import reap_stale_sending
from tests.conftest import FakeConnector

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


class FailingSender(FakeConnector):
    """Can send, but every send fails (to test the error path)."""

    descriptor = ConnectorDescriptor(
        key="failsend",
        display_name="Fail Send",
        auth_type=AuthType.NONE,
        capabilities=frozenset({Capability.POLL, Capability.SEND}),
        poll_interval_s=60,
    )
    sends: ClassVar[int] = 0

    async def fetch_new(self, since: str | None) -> FetchResult:
        return FetchResult(messages=[], cursor="x")

    async def send(self, thread_id: str, text: str, *, reply_to: ReplyContext) -> SendResult:
        type(self).sends += 1
        raise RuntimeError("upstream exploded")


@pytest.fixture
def failsend(monkeypatch: pytest.MonkeyPatch) -> type[FailingSender]:
    monkeypatch.setitem(registry._REGISTRY, "failsend", FailingSender)
    FailingSender.sends = 0
    return FailingSender


async def _sample(client: httpx.AsyncClient, scenario: str = "client-question") -> str:
    r = await client.post("/api/connectors/demo/sample", json={"scenario": scenario})
    assert r.status_code == 201, r.text
    return str(r.json()["message_id"])


async def _triage_now(client: httpx.AsyncClient) -> None:
    """The app_client fixture runs without background work: run one agent pass by hand."""
    runtime: Any = client._transport.app.state.runtime  # type: ignore[attr-defined]
    while await runtime.runner.worker.tick():
        pass


async def test_detail_includes_thread_draft_open_questions_and_can_send(
    app_client: httpx.AsyncClient,
) -> None:
    mid = await _sample(app_client)
    await _triage_now(app_client)
    r = await app_client.get(f"/api/messages/{mid}")
    assert r.status_code == 200
    d = r.json()
    assert d["can_send"] is True  # demo outbox
    assert d["agent_status"] == "done"
    assert d["priority"] is not None
    assert d["body"].startswith("Hello Dhruv")
    assert [t["direction"] for t in d["thread"]] == ["inbound"]
    assert d["draft"]["version"] == 1
    assert d["draft"]["author"] == "agent"
    assert isinstance(d["draft"]["open_questions"], list)
    assert d["draft_versions"] == 1
    assert d["replies"] == []
    assert (await app_client.get(f"/api/messages/{uuid.uuid4()}")).status_code == 404


async def test_stats(app_client: httpx.AsyncClient) -> None:
    await _sample(app_client)
    await _sample(app_client, "newsletter")
    s = (await app_client.get("/api/messages/stats")).json()
    assert s["total_open"] == 2
    assert s["open_by_source"] == {"demo": 2}
    assert s["agent_active"] == 2  # both pending until the agent runs


async def test_human_draft_versions(app_client: httpx.AsyncClient, sessionmaker: SM) -> None:
    mid = await _sample(app_client)
    await _triage_now(app_client)
    r = await app_client.put(f"/api/messages/{mid}/draft", json={"body": "My own words."})
    assert r.status_code == 200
    assert (r.json()["version"], r.json()["author"]) == (2, "human")
    # agent's open questions stay visible next to the edited text
    agent_q = (await app_client.get(f"/api/messages/{mid}")).json()["draft"]["open_questions"]
    assert r.json()["open_questions"] == agent_q
    # same text again: no new version
    again = await app_client.put(f"/api/messages/{mid}/draft", json={"body": "My own words."})
    assert again.json()["version"] == 2


async def test_approve_sends_once_into_demo_outbox(
    app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    mid = await _sample(app_client)
    await _triage_now(app_client)
    key = str(uuid.uuid4())
    body = {"body": "Hi Rahul, we retry 5 times over 24 hours.", "idempotency_key": key}
    r = await app_client.post(f"/api/messages/{mid}/approve", json=body)
    assert r.status_code == 200, r.text
    reply = r.json()
    assert reply["status"] == "sent"
    assert reply["external_id"].startswith("demo-out-")

    # the same click delivered twice -> the same reply, nothing re-sent
    r2 = await app_client.post(f"/api/messages/{mid}/approve", json=body)
    assert r2.json()["id"] == reply["id"]
    # a second, different approval -> already replied
    r3 = await app_client.post(
        f"/api/messages/{mid}/approve", json={**body, "idempotency_key": str(uuid.uuid4())}
    )
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "already_replied"

    d = (await app_client.get(f"/api/messages/{mid}")).json()
    assert d["status"] == "replied"
    assert [t["direction"] for t in d["thread"]] == ["inbound", "outbound"]
    assert d["thread"][1]["body"] == body["body"]
    assert d["draft"]["author"] == "human"  # the edited text was kept as a version
    async with sessionmaker() as s:
        replies = (await s.execute(select(OutboundReply))).scalars().all()
    assert len(replies) == 1


async def test_concurrent_approvals_with_different_keys_send_once(
    app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    mid = await _sample(app_client)
    results = await asyncio.gather(
        *(
            app_client.post(
                f"/api/messages/{mid}/approve",
                json={"body": "Reply", "idempotency_key": str(uuid.uuid4())},
            )
            for _ in range(3)
        )
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1
    assert set(codes) <= {200, 409}
    async with sessionmaker() as s:
        sent = (
            (await s.execute(select(OutboundReply).where(OutboundReply.status == "sent")))
            .scalars()
            .all()
        )
    assert len(sent) == 1


async def test_read_only_source_cannot_be_approved(
    fake_connector: type[FakeConnector],
    app_client: httpx.AsyncClient,
    sessionmaker: SM,
) -> None:
    from app.services.ingest import ingest_messages
    from tests.conftest import msg
    from tests.helpers import get_account

    acct = await get_account(sessionmaker, "fake")
    async with sessionmaker() as s, s.begin():
        await ingest_messages(s, acct, [msg(1)])
    async with sessionmaker() as s:
        mid = (await s.execute(select(Message.id))).scalar_one()
    detail = (await app_client.get(f"/api/messages/{mid}")).json()
    assert detail["can_send"] is False  # -> the dashboard shows Copy draft
    r = await app_client.post(
        f"/api/messages/{mid}/approve", json={"body": "x", "idempotency_key": str(uuid.uuid4())}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "connector_read_only"


class BlockingSender(FailingSender):
    """Can send, but refuses no-reply senders (the reply_blocked_reason hook)."""

    descriptor = ConnectorDescriptor(
        key="blocksend",
        display_name="Block Send",
        auth_type=AuthType.NONE,
        capabilities=frozenset({Capability.POLL, Capability.SEND}),
        poll_interval_s=60,
    )

    @classmethod
    def reply_blocked_reason(cls, *, sender_id: str, reply_meta: Any) -> str | None:
        return "no-reply sender" if sender_id.startswith("noreply") else None


@pytest.fixture
def blocksend(monkeypatch: pytest.MonkeyPatch) -> type[BlockingSender]:
    monkeypatch.setitem(registry._REGISTRY, "blocksend", BlockingSender)
    BlockingSender.sends = 0
    return BlockingSender


async def test_blocked_reply_is_hidden_in_detail_and_refused_without_sending(
    blocksend: type[BlockingSender], app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    from app.services.ingest import ingest_messages
    from tests.conftest import msg
    from tests.helpers import get_account

    acct = await get_account(sessionmaker, "blocksend")
    async with sessionmaker() as s, s.begin():
        await ingest_messages(s, acct, [msg(1, source="blocksend", sender="noreply@x.example")])
    async with sessionmaker() as s:
        mid = (await s.execute(select(Message.id))).scalar_one()

    d = (await app_client.get(f"/api/messages/{mid}")).json()
    assert d["can_send"] is False
    assert d["send_blocked_reason"] == "no-reply sender"
    r = await app_client.post(
        f"/api/messages/{mid}/approve", json={"body": "x", "idempotency_key": str(uuid.uuid4())}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reply_blocked"
    assert blocksend.sends == 0
    async with sessionmaker() as s:
        assert (await s.execute(select(OutboundReply))).first() is None  # no reply row at all


async def test_failed_send_returns_502_keeps_message_open_and_edited_draft(
    failsend: type[FailingSender], app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    from app.services.ingest import ingest_messages
    from tests.conftest import msg
    from tests.helpers import get_account

    acct = await get_account(sessionmaker, "failsend")
    async with sessionmaker() as s, s.begin():
        await ingest_messages(s, acct, [msg(1, source="failsend")])
    async with sessionmaker() as s:
        mid = (await s.execute(select(Message.id))).scalar_one()

    edited = "My carefully edited reply"
    r = await app_client.post(
        f"/api/messages/{mid}/approve",
        json={"body": edited, "idempotency_key": str(uuid.uuid4())},
    )
    assert r.status_code == 502
    assert "upstream exploded" in r.json()["error"]["message"]
    d = (await app_client.get(f"/api/messages/{mid}")).json()
    assert d["status"] == "open"
    assert d["draft"]["body"] == edited  # nothing lost
    assert [x["status"] for x in d["replies"]] == ["failed"]
    assert "upstream exploded" in d["replies"][0]["error"]
    assert failsend.sends == 1

    # retrying is a deliberate new click (new key) and is allowed after a failure
    r2 = await app_client.post(
        f"/api/messages/{mid}/approve",
        json={"body": edited, "idempotency_key": str(uuid.uuid4())},
    )
    assert r2.status_code == 502
    assert failsend.sends == 2


async def test_triage_button_on_skipped_message(
    app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    mid = await _sample(app_client)
    async with sessionmaker() as s, s.begin():
        await s.execute(text("DELETE FROM agent_jobs"))
        await s.execute(text("UPDATE messages SET agent_status = 'skipped'"))
    r = await app_client.post(f"/api/messages/{mid}/triage")
    assert r.status_code == 202
    async with sessionmaker() as s:
        job = (await s.execute(select(AgentJob))).scalar_one()
    assert (job.kind, job.priority) == ("triage", PRIORITY_USER)
    again = await app_client.post(f"/api/messages/{mid}/triage")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "job_active"
    await _triage_now(app_client)
    assert (await app_client.get(f"/api/messages/{mid}")).json()["agent_status"] == "done"


async def test_redraft_with_instructions(app_client: httpx.AsyncClient, sessionmaker: SM) -> None:
    mid = await _sample(app_client)
    await _triage_now(app_client)
    r = await app_client.post(f"/api/messages/{mid}/redraft", json={"instructions": "shorter"})
    assert r.status_code == 202
    await _triage_now(app_client)
    async with sessionmaker() as s:
        drafts = (await s.execute(select(Draft).order_by(Draft.version))).scalars().all()
    assert [d.version for d in drafts] == [1, 2]
    assert drafts[1].instructions == "shorter"


async def test_dismiss_and_reopen(app_client: httpx.AsyncClient) -> None:
    mid = await _sample(app_client)
    r = await app_client.post(f"/api/messages/{mid}/dismiss")
    assert (r.status_code, r.json()["status"]) == (200, "dismissed")
    assert (await app_client.post(f"/api/messages/{mid}/dismiss")).status_code == 409
    feed = (await app_client.get("/api/messages")).json()["items"]
    assert feed == []
    r = await app_client.post(f"/api/messages/{mid}/reopen")
    assert r.json()["status"] == "open"
    # editing/redrafting a dismissed message is blocked until reopened
    await app_client.post(f"/api/messages/{mid}/dismiss")
    assert (
        await app_client.put(f"/api/messages/{mid}/draft", json={"body": "x"})
    ).status_code == 409


async def test_scenarios_endpoint(
    fake_connector: type[FakeConnector], app_client: httpx.AsyncClient
) -> None:
    r = await app_client.get("/api/connectors/demo/scenarios")
    assert "prod-incident" in r.json()["scenarios"]
    assert (await app_client.get("/api/connectors/fake/scenarios")).status_code == 409


async def test_stale_sending_reaped_to_unknown(
    app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    mid = await _sample(app_client)
    async with sessionmaker() as s, s.begin():
        acct_id = (await s.execute(select(Message.connector_account_id))).scalar_one()
        s.add(
            OutboundReply(
                message_id=uuid.UUID(mid),
                connector_account_id=acct_id,
                external_thread_id="t",
                body="b",
                status="sending",
                idempotency_key=uuid.uuid4(),
                approved_by="password:owner",
            )
        )
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE outbound_replies SET created_at = now() - interval '10 minutes'")
        )
    assert await reap_stale_sending(sessionmaker) == 1
    d = (await app_client.get(f"/api/messages/{mid}")).json()
    assert d["replies"][0]["status"] == "unknown"
    # an unknown send blocks another approval until you check the app
    r = await app_client.post(
        f"/api/messages/{mid}/approve", json={"body": "x", "idempotency_key": str(uuid.uuid4())}
    )
    assert r.json()["error"]["code"] == "send_in_progress"
