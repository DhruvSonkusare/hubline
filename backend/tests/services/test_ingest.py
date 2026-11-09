from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors.models import NormalizedMessage
from app.models import ConnectorAccount, Thread
from app.models.message import BODY_MAX_CHARS
from app.services.ingest import IngestResult, ingest_messages
from tests.conftest import FakeConnector, msg
from tests.helpers import all_messages, setup_accounts

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


async def _ingest(sm: SM, account: ConnectorAccount, msgs: list[NormalizedMessage]) -> IngestResult:
    async with sm() as s, s.begin():
        return await ingest_messages(s, account, msgs)


async def test_inserts_and_dedups(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    acct = (await setup_accounts(sessionmaker, settings))["fake"]

    r1 = await _ingest(sessionmaker, acct, [msg(1), msg(2), msg(2)])
    assert (r1.inserted, r1.duplicates) == (2, 1)
    assert len(r1.new_inbound_ids) == 2

    r2 = await _ingest(sessionmaker, acct, [msg(1), msg(2), msg(3)])
    assert (r2.inserted, r2.duplicates) == (1, 2)
    assert [m.external_id for m in await all_messages(sessionmaker)] == ["m1", "m2", "m3"]


async def test_thread_upsert_tracks_latest_message(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    acct = (await setup_accounts(sessionmaker, settings))["fake"]
    t = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    await _ingest(sessionmaker, acct, [msg(1, received_at=t + timedelta(hours=1), subject="Hi")])
    await _ingest(sessionmaker, acct, [msg(2, received_at=t)])  # older, arrives later
    async with sessionmaker() as s:
        threads = (await s.execute(select(Thread))).scalars().all()
    assert len(threads) == 1
    assert threads[0].last_message_at == t + timedelta(hours=1)
    assert threads[0].title == "Hi"
    assert {m.thread_id for m in await all_messages(sessionmaker)} == {threads[0].id}


async def test_self_sender_is_outbound_and_marks_thread_replied(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    acct = (await setup_accounts(sessionmaker, settings))["fake"]
    async with sessionmaker() as s, s.begin():
        db_acct = await s.get(ConnectorAccount, acct.id)
        assert db_acct is not None
        db_acct.self_ids = ["me"]
    acct.self_ids = ["me"]

    t = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    await _ingest(sessionmaker, acct, [msg(1, received_at=t), msg(2, received_at=t, thread="x")])
    r = await _ingest(
        sessionmaker, acct, [msg(3, sender="me", received_at=t + timedelta(minutes=5))]
    )
    assert r.inserted == 1
    assert r.new_inbound_ids == []

    by_id = {m.external_id: m for m in await all_messages(sessionmaker)}
    assert by_id["m3"].direction == "outbound"
    assert by_id["m1"].status == "replied"  # same thread, answered natively in the app
    assert by_id["m1"].status_changed_at is not None
    assert by_id["m2"].status == "open"  # other thread untouched


async def test_rejects_foreign_source(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    acct = (await setup_accounts(sessionmaker, settings))["fake"]
    with pytest.raises(ValueError, match="produced a message for source"):
        await _ingest(sessionmaker, acct, [msg(1, source="demo")])
    assert await all_messages(sessionmaker) == []


async def test_truncates_body_and_oversized_raw(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    acct = (await setup_accounts(sessionmaker, settings))["fake"]
    huge = msg(5).model_copy(
        update={"body": "x" * (BODY_MAX_CHARS + 10), "raw": {"b": "y" * 70_000}}
    )
    await _ingest(sessionmaker, acct, [huge])
    (stored,) = await all_messages(sessionmaker)
    assert len(stored.body) == BODY_MAX_CHARS
    assert stored.raw is not None
    assert stored.raw["_truncated"] is True


async def test_demo_account_marks_rows_demo(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    acct = (await setup_accounts(sessionmaker, settings))["demo"]
    await _ingest(sessionmaker, acct, [msg(1, source="demo")])  # is_demo not set on the message
    (stored,) = await all_messages(sessionmaker)
    assert stored.is_demo is True
