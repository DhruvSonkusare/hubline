"""Full Step 1 flow in-process: app starts with the scheduler on, the demo connector is enabled
through the API, the scheduler polls it, and the messages show up in the feed."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app
from tests.conftest import TEST_TOKEN, make_settings

pytestmark = pytest.mark.db


async def test_demo_messages_flow_into_feed(engine: AsyncEngine) -> None:
    app = create_app(make_settings(enable_scheduler=True, scheduler_idle_max_s=3600))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as client,
    ):
        assert (await client.get("/healthz")).json()["workers"]["scheduler"] == "running"
        assert (await client.get("/api/messages")).json()["items"] == []

        # Enabling wakes the (otherwise hour-long idle) scheduler immediately.
        assert (await client.patch("/api/connectors/demo", json={"enabled": True})).is_success
        items: list[dict[str, object]] = []
        for _ in range(100):
            items = (await client.get("/api/messages")).json()["items"]
            if items:
                break
            await asyncio.sleep(0.05)
        assert len(items) == 6
        assert all(i["is_demo"] for i in items)

        connectors = {c["key"]: c for c in (await client.get("/api/connectors")).json()}
        assert connectors["demo"]["last_success_at"] is not None

        # Re-polling right away (sync now) adds nothing: same cursor, dedup holds.
        assert (await client.post("/api/connectors/demo/sync")).status_code == 202
        await asyncio.sleep(0.5)
        assert len((await client.get("/api/messages")).json()["items"]) == 6
