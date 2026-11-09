"""Full flow in-process with the scheduler and agent worker running (fake LLM): the demo
connector is enabled through the API, the scheduler polls it, the agent triages, and the results
show up in the feed. Loops are configured to idle for an hour, so everything below proves the
in-process wake-ups work."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app
from tests.conftest import TEST_TOKEN, make_settings

pytestmark = pytest.mark.db


@asynccontextmanager
async def running_app(**overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        enable_scheduler=True,
        enable_agent_worker=True,
        scheduler_idle_max_s=3600,
        worker_idle_max_s=3600,
        worker_min_sleep_s=3600,
        **overrides,
    )
    app = create_app(settings)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as client,
    ):
        yield client


async def _poll_feed(client: httpx.AsyncClient, done: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for _ in range(100):
        items = (await client.get("/api/messages")).json()["items"]
        if done(items):
            break
        await asyncio.sleep(0.05)
    return items


async def test_demo_messages_flow_into_feed_and_get_triaged(engine: AsyncEngine) -> None:
    async with running_app() as client:
        workers = (await client.get("/healthz")).json()["workers"]
        assert workers == {"scheduler": "running", "agent_worker": "running"}
        assert (await client.get("/api/messages")).json()["items"] == []

        # Enabling wakes the scheduler; ingest wakes the agent worker.
        assert (await client.patch("/api/connectors/demo", json={"enabled": True})).is_success
        items = await _poll_feed(
            client, lambda xs: len(xs) == 6 and all(i["agent_status"] == "done" for i in xs)
        )
        assert len(items) == 6
        assert all(i["is_demo"] for i in items)
        assert all(i["agent_status"] == "done" for i in items), items
        assert all(i["priority"] is not None and i["summary"] for i in items)
        assert any(i["has_draft"] for i in items)

        connectors = {c["key"]: c for c in (await client.get("/api/connectors")).json()}
        assert connectors["demo"]["last_success_at"] is not None

        # Re-polling right away (sync now) adds nothing: same cursor, dedup holds.
        assert (await client.post("/api/connectors/demo/sync")).status_code == 202
        await asyncio.sleep(0.5)
        assert len((await client.get("/api/messages")).json()["items"]) == 6


async def test_live_sample_is_triaged_by_running_worker(engine: AsyncEngine) -> None:
    """The presenter flow: generate a message, the agent picks it up within moments."""
    async with running_app() as client:
        await asyncio.sleep(0.2)  # both loops are now idle for an hour
        r = await client.post("/api/connectors/demo/sample", json={"scenario": "prod-incident"})
        message_id = r.json()["message_id"]
        items = await _poll_feed(client, lambda xs: xs and xs[0]["agent_status"] == "done")
        assert items[0]["id"] == message_id
        assert items[0]["agent_status"] == "done"
        assert items[0]["priority"] == 5
        assert items[0]["has_draft"] is True
