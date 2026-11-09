"""Shared fixtures.

DB tests run against a real Postgres (SKIP LOCKED, ON CONFLICT and partial indexes are
Postgres-specific). Default: the docker compose database `hubline_test` on port 5433.
Override with TEST_DATABASE_URL (e.g. a Neon `test` branch).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors import discover, registry
from app.connectors.base import (
    AuthType,
    Capability,
    Connector,
    ConnectorDescriptor,
)
from app.connectors.models import FetchResult, NormalizedMessage, Sender
from app.db.engine import create_engine, create_sessionmaker, normalize_database_url
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://hubline:hubline@localhost:5433/hubline_test"
)
TEST_TOKEN = "test-token-0123456789abcdef0123456789abcdef"
TABLES = ("messages", "threads", "sync_state", "connector_accounts")

discover()  # register the real connectors once for the whole session


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "backend_api_tokens": TEST_TOKEN,
        "app_env": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "enable_scheduler": False,
        "enable_agent_worker": False,
        "demo_mode": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# ----------------------------------------------------------------------------- database


async def _probe(url: str) -> None:
    engine = create_engine(url, pool_size=1)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Reset the test database schema once per session and migrate it to head."""
    clean_url, _ = normalize_database_url(TEST_DATABASE_URL)
    db_name = clean_url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in db_name:
        pytest.exit(f"Refusing to reset non-test database {db_name!r}", returncode=2)
    try:
        asyncio.run(_probe(TEST_DATABASE_URL))
    except Exception as exc:
        pytest.exit(
            f"Test database unreachable at {TEST_DATABASE_URL} ({exc!r}).\n"
            "Start it with: docker compose up -d db   (from the repo root)",
            returncode=2,
        )

    async def _reset() -> None:
        engine = create_engine(TEST_DATABASE_URL, pool_size=1)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset())
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_db, pool_size=5)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


# ----------------------------------------------------------------------------- app


@pytest.fixture
async def app_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings())
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        ) as client,
    ):
        yield client


# ----------------------------------------------------------------------------- fake connectors


class FakeConnector(Connector):
    """Scriptable connector for tests. Configure via class attributes before use."""

    descriptor = ConnectorDescriptor(
        key="fake",
        display_name="Fake",
        auth_type=AuthType.NONE,
        capabilities=frozenset({Capability.POLL}),
        poll_interval_s=60,
    )
    pages: ClassVar[list[FetchResult]] = []
    error: ClassVar[Exception | None] = None
    calls: ClassVar[list[str | None]] = []
    delay_s: ClassVar[float] = 0.0

    async def fetch_new(self, since: str | None) -> FetchResult:
        type(self).calls.append(since)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if type(self).error is not None:
            raise type(self).error
        if type(self).pages:
            return type(self).pages.pop(0)
        return FetchResult(messages=[], cursor=since)


class FakeOAuthConnector(FakeConnector):
    descriptor = ConnectorDescriptor(
        key="fakeoauth",
        display_name="Fake OAuth",
        auth_type=AuthType.OAUTH2,
        capabilities=frozenset({Capability.POLL, Capability.SEND}),
        poll_interval_s=60,
    )


@pytest.fixture
def fake_connector(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnector]:
    monkeypatch.setitem(registry._REGISTRY, "fake", FakeConnector)
    monkeypatch.setitem(registry._REGISTRY, "fakeoauth", FakeOAuthConnector)
    FakeConnector.pages = []
    FakeConnector.error = None
    FakeConnector.calls = []
    FakeConnector.delay_s = 0.0
    return FakeConnector


def msg(
    n: int,
    *,
    source: str = "fake",
    thread: str = "t1",
    sender: str = "alice",
    received_at: datetime | None = None,
    **extra: Any,
) -> NormalizedMessage:
    return NormalizedMessage(
        source=source,
        external_id=f"m{n}",
        thread_id=thread,
        sender=Sender(id=sender, name=sender.title()),
        body=f"body {n}",
        received_at=received_at or datetime(2026, 11, 9, 10, n % 60, tzinfo=UTC),
        **extra,
    )
