"""Contract suite run against EVERY registered connector. A new connector module is picked up
automatically; if it needs credentials or HTTP mocks, add a factory to CONTEXT_FACTORIES."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
import structlog

from app.connectors import registry
from app.connectors.base import (
    Connector,
    ConnectorContext,
    ReadOnlyConnectorError,
)
from app.connectors.models import FetchResult, ReplyContext, Sender, WebhookRequest
from tests.conftest import make_settings
from tests.connectors import payloads

FIXED_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

# key -> callable returning extra ConnectorContext kwargs (credentials, ...)
CONTEXT_FACTORIES: dict[str, Callable[[], dict[str, Any]]] = {
    "slack": lambda: {"credentials": payloads.slack_credentials()},
    "gmail": lambda: {"credentials": payloads.gmail_credentials(now=FIXED_NOW)},
}
# key -> function registering respx routes for the connector's HTTP calls
HTTP_MOCKS: dict[str, Callable[[respx.Router], None]] = {
    "slack": payloads.mock_slack_basics,
    "gmail": payloads.mock_gmail_basics,
}

KEYS = sorted(registry.all_connectors())


def make_ctx(cls: type[Connector], http: httpx.AsyncClient) -> ConnectorContext:
    desc = cls.describe()
    extra = CONTEXT_FACTORIES.get(desc.key, dict)()
    return ConnectorContext(
        account_id=uuid.uuid4(),
        source=desc.key,
        settings=desc.settings_model() if desc.settings_model else None,
        self_ids=frozenset(),
        http=http,
        app_settings=make_settings(),
        log=structlog.get_logger("test"),
        clock=lambda: FIXED_NOW,
        **extra,
    )


def test_registry_is_not_empty() -> None:
    assert KEYS


@pytest.mark.parametrize("key", KEYS)
def test_descriptor_is_valid(key: str) -> None:
    cls = registry.get(key)
    desc = cls.describe()
    assert desc.key == key
    assert desc.display_name
    module_name = cls.__module__.rsplit(".", 1)[-1]
    assert module_name == key, "connector module file must be named after its key"
    if desc.can_poll:
        assert desc.poll_interval_s is not None
        assert desc.poll_interval_s >= 10
    if desc.settings_model is not None:
        desc.settings_model()  # defaults must be valid
    assert not inspect.isabstract(cls)


@pytest.mark.parametrize("key", KEYS)
async def test_first_fetch_returns_valid_messages(key: str) -> None:
    cls = registry.get(key)
    if not cls.describe().can_poll:
        pytest.skip("not a polling connector")
    with respx.mock(assert_all_called=False) as router:
        HTTP_MOCKS.get(key, lambda _: None)(router)
        async with httpx.AsyncClient() as http:
            connector = cls(make_ctx(cls, http))
            result = await connector.fetch_new(None)
    assert isinstance(result, FetchResult)
    assert result.cursor is not None, "first fetch must return a cursor to resume from"
    ids = [m.external_id for m in result.messages]
    assert len(ids) == len(set(ids)), "external_id must be unique within a batch"
    for m in result.messages:
        assert m.source == key
        assert m.received_at.tzinfo is not None
        if cls.describe().is_demo:
            assert m.is_demo


@pytest.mark.parametrize("key", KEYS)
async def test_read_only_connectors_refuse_send(key: str) -> None:
    cls = registry.get(key)
    if cls.describe().can_send:
        pytest.skip("connector can send")
    async with httpx.AsyncClient() as http:
        connector = cls(make_ctx(cls, http))
        with pytest.raises(ReadOnlyConnectorError):
            await connector.send(
                "t",
                "hello",
                reply_to=ReplyContext("x", None, Sender(id="s"), {}),
            )


@pytest.mark.parametrize("key", KEYS)
async def test_non_webhook_connectors_reject_webhooks(key: str) -> None:
    cls = registry.get(key)
    if "webhook" in {c.value for c in cls.describe().capabilities}:
        pytest.skip("webhook connector; covered by its own signature tests")
    async with httpx.AsyncClient() as http:
        connector = cls(make_ctx(cls, http))
        with pytest.raises(NotImplementedError):
            await connector.parse_webhook(WebhookRequest("POST", "/", {}, {}, b""))


@pytest.mark.parametrize("key", KEYS)
async def test_sample_capable_connectors_generate_valid_unique_messages(key: str) -> None:
    from app.connectors.base import Capability

    cls = registry.get(key)
    if Capability.SAMPLE not in cls.describe().capabilities:
        pytest.skip("no sample capability")
    async with httpx.AsyncClient() as http:
        connector = cls(make_ctx(cls, http))
        a, b = await connector.sample(), await connector.sample()
        with pytest.raises(ValueError):  # noqa: PT011 - message wording is connector-specific
            await connector.sample("definitely-not-a-scenario")
    assert a.source == b.source == key
    assert a.external_id != b.external_id
    assert a.received_at == FIXED_NOW
