from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import SyncState
from tests.conftest import FakeConnector, FakeOAuthConnector, make_settings
from tests.helpers import setup_accounts

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


async def test_creates_rows_and_statuses(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    accounts = await setup_accounts(sessionmaker, settings)
    assert accounts["demo"].status == "connected"  # auth NONE
    assert accounts["demo"].is_demo is True
    assert accounts["demo"].enabled is False  # R4: DEMO_MODE defaults to false
    assert accounts["fakeoauth"].status == "disconnected"
    async with sessionmaker() as s:
        count = (await s.execute(select(func.count()).select_from(SyncState))).scalar_one()
    assert count == len(accounts)


async def test_demo_mode_enables_demo_only_on_first_registration(sessionmaker: SM) -> None:
    demo_on = make_settings(demo_mode=True)
    accounts = await setup_accounts(sessionmaker, demo_on)
    assert accounts["demo"].enabled is True

    # User switches it off; a restart with DEMO_MODE=true must not switch it back on.
    accounts = await setup_accounts(sessionmaker, demo_on, demo=False)
    assert accounts["demo"].enabled is False
    accounts = await setup_accounts(sessionmaker, demo_on)
    assert accounts["demo"].enabled is False


async def test_missing_required_settings_marks_not_configured(
    sessionmaker: SM,
    settings: Settings,
    fake_connector: type[FakeConnector],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FakeOAuthConnector,
        "descriptor",
        replace(FakeOAuthConnector.descriptor, required_settings=("frontend_url",)),
    )
    accounts = await setup_accounts(sessionmaker, make_settings(frontend_url=""))
    assert accounts["fakeoauth"].status == "not_configured"

    # Once configured it becomes "disconnected" (still needs OAuth), never "connected".
    accounts = await setup_accounts(sessionmaker, settings)
    assert accounts["fakeoauth"].status == "disconnected"
