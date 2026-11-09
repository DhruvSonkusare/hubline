"""POST /internal/tick and on-demand work when the background loops are off (Render free)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app
from tests.conftest import TEST_TOKEN, make_settings

pytestmark = pytest.mark.db

SECRET = "tick-secret-for-tests-0123456789"
TICK = {"X-Tick-Secret": SECRET}


@asynccontextmanager
async def app_without_loops(**overrides: Any) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    values: dict[str, Any] = {
        "enable_scheduler": False,
        "enable_agent_worker": False,
        "work_on_demand": True,
        "tick_secret": SECRET,
    }
    values.update(overrides)
    app = create_app(make_settings(**values))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as client,
    ):
        yield app, client


async def _idle(app: FastAPI) -> None:
    await app.state.runtime.runner.idle()


async def _feed(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = (await client.get("/api/messages")).json()["items"]
    return items


async def test_tick_disabled_without_secret(engine: AsyncEngine) -> None:
    async with app_without_loops(tick_secret=None) as (_, client):
        r = await client.post("/internal/tick", headers={"X-Tick-Secret": "anything"})
        assert r.status_code == 404


@pytest.mark.parametrize("headers", [{}, {"X-Tick-Secret": "wrong"}])
async def test_tick_rejects_bad_secret(engine: AsyncEngine, headers: dict[str, str]) -> None:
    async with app_without_loops() as (_, client):
        r = await client.post("/internal/tick", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"


async def test_tick_polls_and_drains_agent_jobs(engine: AsyncEngine) -> None:
    async with app_without_loops(demo_mode=True) as (_, client):  # demo enabled on first sync
        r = await client.post("/internal/tick", params={"wait": "true"}, headers=TICK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["polled"], body["agent_jobs"], body["stopped"]) == (1, 6, "drained")
        items = await _feed(client)
        assert len(items) == 6
        assert all(i["agent_status"] == "done" for i in items)

        # nothing due any more: a second tick is a cheap no-op
        again = (await client.post("/internal/tick", params={"wait": "true"}, headers=TICK)).json()
        assert (again["polled"], again["agent_jobs"]) == (0, 0)


async def test_tick_without_wait_returns_202_and_runs_in_background(engine: AsyncEngine) -> None:
    async with app_without_loops(demo_mode=True) as (app, client):
        r = await client.post("/internal/tick", headers=TICK)
        assert r.status_code == 202
        assert r.json() == {"status": "started"}
        await _idle(app)
        items = await _feed(client)
        assert len(items) == 6
        assert all(i["agent_status"] == "done" for i in items)


async def test_tick_stops_at_the_time_budget(engine: AsyncEngine) -> None:
    async with app_without_loops(demo_mode=True, tick_budget_s=0.001) as (app, _):
        result = await app.state.runtime.runner.tick()
        assert result.polled == 1
        assert result.stopped == "time_limit"
        assert result.agent_jobs < 6


async def test_concurrent_ticks_are_single_flight(engine: AsyncEngine) -> None:
    async with app_without_loops(demo_mode=True) as (app, _):
        runner = app.state.runtime.runner
        first, second = await asyncio.gather(runner.tick(), runner.tick())
        assert sorted([first.stopped, second.stopped]) == ["busy", "drained"]


async def test_sample_is_processed_on_demand_without_loops(engine: AsyncEngine) -> None:
    async with app_without_loops() as (app, client):
        r = await client.post("/api/connectors/demo/sample", json={"scenario": "prod-incident"})
        assert r.status_code == 201
        await _idle(app)
        (item,) = await _feed(client)
        assert item["agent_status"] == "done"
        assert item["priority"] == 5


async def test_enabling_a_connector_polls_and_triages_on_demand(engine: AsyncEngine) -> None:
    async with app_without_loops() as (app, client):
        assert (await client.patch("/api/connectors/demo", json={"enabled": True})).is_success
        await _idle(app)
        items = await _feed(client)
        assert len(items) == 6
        assert all(i["agent_status"] == "done" for i in items)


async def test_tick_on_startup(engine: AsyncEngine) -> None:
    async with app_without_loops(demo_mode=True, tick_on_startup=True) as (app, client):
        await _idle(app)
        assert len(await _feed(client)) == 6


async def test_work_on_demand_off_leaves_jobs_for_the_next_tick(engine: AsyncEngine) -> None:
    async with app_without_loops(work_on_demand=False) as (app, client):
        await client.post("/api/connectors/demo/sample")
        await _idle(app)
        (item,) = await _feed(client)
        assert item["agent_status"] == "pending"
