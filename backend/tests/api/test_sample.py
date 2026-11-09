"""POST /api/connectors/{key}/sample: generate one demo message on demand."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentJob, Message
from tests.conftest import FakeConnector

pytestmark = pytest.mark.db


async def test_sample_creates_message_and_triage_job_even_when_drip_disabled(
    app_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    connectors = {c["key"]: c for c in (await app_client.get("/api/connectors")).json()}
    assert connectors["demo"]["enabled"] is False

    r = await app_client.post("/api/connectors/demo/sample", json={"scenario": "prod-incident"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent_job_enqueued"] is True
    assert body["external_id"].startswith("demo-live-")

    async with sessionmaker() as s:
        m = await s.get(Message, body["message_id"])
        assert m is not None
        assert m.is_demo is True
        assert m.subject == "URGENT: payments API 500s in prod"
        assert m.agent_status == "pending"
        job = (await s.execute(select(AgentJob).where(AgentJob.message_id == m.id))).scalar_one()
        assert (job.kind, job.status) == ("triage", "pending")

    feed = (await app_client.get("/api/messages", params={"source": "demo"})).json()["items"]
    assert [i["id"] for i in feed] == [body["message_id"]]


async def test_sample_without_body_picks_a_scenario_and_ids_are_unique(
    app_client: httpx.AsyncClient,
) -> None:
    ids = set()
    for _ in range(3):
        r = await app_client.post("/api/connectors/demo/sample")
        assert r.status_code == 201
        ids.add(r.json()["message_id"])
    assert len(ids) == 3


async def test_unknown_scenario_is_422_listing_valid_ones(app_client: httpx.AsyncClient) -> None:
    r = await app_client.post("/api/connectors/demo/sample", json={"scenario": "nope"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_scenario"
    assert "prod-incident" in r.json()["error"]["message"]


async def test_connector_without_sample_capability(
    fake_connector: type[FakeConnector], app_client: httpx.AsyncClient
) -> None:
    r = await app_client.post("/api/connectors/fake/sample")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_supported"
    assert (await app_client.post("/api/connectors/nope/sample")).status_code == 404


async def test_sample_job_has_dashboard_priority(
    app_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.services.jobs import PRIORITY_USER

    r = await app_client.post("/api/connectors/demo/sample")
    async with sessionmaker() as s:
        job = (
            await s.execute(select(AgentJob).where(AgentJob.message_id == r.json()["message_id"]))
        ).scalar_one()
    assert job.priority == PRIORITY_USER
