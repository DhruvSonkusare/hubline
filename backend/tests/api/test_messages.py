from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Message
from app.services.ingest import ingest_messages
from tests.conftest import FakeConnector, make_settings, msg
from tests.helpers import setup_accounts

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


async def seed(sm: async_sessionmaker[AsyncSession]) -> None:
    accounts = await setup_accounts(sm, make_settings())
    async with sm() as s, s.begin():
        await ingest_messages(
            s,
            accounts["fake"],
            [msg(i, received_at=T0 + timedelta(minutes=i), subject=f"S{i}") for i in range(5)],
        )
        await ingest_messages(
            s,
            accounts["demo"],
            [msg(10, source="demo", received_at=T0 + timedelta(minutes=30))],
        )
        await s.execute(
            update(Message).where(Message.external_id == "m0").values(status="dismissed")
        )


async def test_feed_newest_first_open_only(
    app_client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    fake_connector: type[FakeConnector],
) -> None:
    await seed(sessionmaker)
    r = await app_client.get("/api/messages")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["subject"] for i in items] == [None, "S4", "S3", "S2", "S1"]
    first = items[0]
    assert first["source"] == "demo"
    assert first["source_display"] == "Demo data"
    assert first["is_demo"] is True
    assert first["preview"] == "body 10"
    assert items[1]["is_demo"] is False
    assert r.json()["next_cursor"] is None


async def test_filters(
    app_client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    fake_connector: type[FakeConnector],
) -> None:
    await seed(sessionmaker)
    r = await app_client.get("/api/messages", params={"source": "demo"})
    assert [i["source"] for i in r.json()["items"]] == ["demo"]

    r = await app_client.get("/api/messages", params=[("source", "demo"), ("source", "fake")])
    assert len(r.json()["items"]) == 5

    r = await app_client.get("/api/messages", params={"status": "dismissed"})
    assert [i["subject"] for i in r.json()["items"]] == ["S0"]

    r = await app_client.get("/api/messages", params={"status": "all"})
    assert len(r.json()["items"]) == 6


async def test_keyset_pagination(
    app_client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    fake_connector: type[FakeConnector],
) -> None:
    await seed(sessionmaker)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": "2", "status": "all"} | ({"cursor": cursor} if cursor else {})
        page = (await app_client.get("/api/messages", params=params)).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 6
    assert len(set(seen)) == 6


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"cursor": "garbage"}, 400, "invalid_cursor"),
        ({"limit": "0"}, 422, "validation_error"),
        ({"limit": "101"}, 422, "validation_error"),
        ({"status": "weird"}, 422, "validation_error"),
    ],
)
async def test_bad_params(
    app_client: httpx.AsyncClient, params: dict[str, str], status: int, code: str
) -> None:
    r = await app_client.get("/api/messages", params=params)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
