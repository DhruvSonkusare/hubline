from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.db


async def test_healthz_is_public_and_reports_workers(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/healthz", headers={"Authorization": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["workers"] == {"scheduler": "disabled", "agent_worker": "disabled"}
    assert r.headers["x-request-id"]


async def test_readyz_checks_database(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    ],
)
@pytest.mark.parametrize("path", ["/api/messages", "/api/connectors"])
async def test_api_requires_valid_bearer_token(
    app_client: httpx.AsyncClient, headers: dict[str, str], path: str
) -> None:
    r = await app_client.get(path, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_valid_token_accepted(app_client: httpx.AsyncClient) -> None:
    assert (await app_client.get("/api/messages")).status_code == 200


async def test_unknown_route_uses_error_shape(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
