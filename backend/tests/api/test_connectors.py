from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeConnector
from tests.helpers import get_account, get_state

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


async def test_list_connectors(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/api/connectors")
    assert r.status_code == 200
    by_key = {c["key"]: c for c in r.json()}
    demo = by_key["demo"]
    assert demo["is_demo"] is True
    assert demo["enabled"] is False
    assert demo["status"] == "connected"
    assert demo["capabilities"] == ["poll"]
    assert demo["can_send"] is False
    assert demo["settings_schema"]["properties"]["emit_every_minutes"]["default"] == 20
    assert demo["missing_env"] == []


async def test_enable_demo_schedules_immediate_poll(
    app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    r = await app_client.patch("/api/connectors/demo", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    state = await get_state(sessionmaker, "demo")
    assert state.next_poll_at <= datetime.now(UTC) + timedelta(seconds=1)

    r = await app_client.patch("/api/connectors/demo", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert (await get_account(sessionmaker, "demo")).enabled is False


async def test_patch_settings_merges_and_validates(
    app_client: httpx.AsyncClient, sessionmaker: SM
) -> None:
    r = await app_client.patch("/api/connectors/demo", json={"settings": {"max_messages": 10}})
    assert r.status_code == 200
    assert r.json()["settings"]["max_messages"] == 10
    assert r.json()["settings"]["emit_every_minutes"] == 20

    r = await app_client.patch("/api/connectors/demo", json={"settings": {"emit_every_minutes": 0}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert (await get_account(sessionmaker, "demo")).settings["max_messages"] == 10


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code"),
    [
        ("PATCH", "/api/connectors/nope", {"enabled": True}, 404, "not_found"),
        ("POST", "/api/connectors/nope/sync", None, 404, "not_found"),
        ("PATCH", "/api/connectors/demo", {"bogus": 1}, 422, "validation_error"),
        ("POST", "/api/connectors/demo/sync", None, 409, "invalid_state"),  # not enabled
        ("PATCH", "/api/connectors/fakeoauth", {"enabled": True}, 409, "invalid_state"),
    ],
)
async def test_errors(
    fake_connector: type[FakeConnector],  # registered before the app starts -> rows exist
    app_client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
    status: int,
    code: str,
) -> None:
    r = await app_client.request(method, path, json=body)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


async def test_sync_when_enabled(app_client: httpx.AsyncClient) -> None:
    await app_client.patch("/api/connectors/demo", json={"enabled": True})
    r = await app_client.post("/api/connectors/demo/sync")
    assert r.status_code == 202
    assert r.json() == {"status": "scheduled"}
