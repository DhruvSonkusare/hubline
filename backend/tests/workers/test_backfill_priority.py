"""Backfill must not drain the free budget: first sync stores everything but auto-triages only
the newest BACKFILL_AUTO_TRIAGE; live messages and dashboard actions are claimed first."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.fake import FakeLLM
from app.connectors.models import FetchResult
from app.models import AgentJob, Message
from app.services.ingest import ingest_messages
from app.services.jobs import (
    BACKFILL_SKIP_NOTE,
    PRIORITY_BACKFILL,
    PRIORITY_LIVE,
    PRIORITY_USER,
    enqueue,
)
from app.workers.scheduler import Scheduler
from tests.conftest import FakeConnector, make_settings, msg
from tests.helpers import make_due, setup_accounts
from tests.workers.test_agent_worker import worker

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


async def _messages(sm: SM) -> dict[str, Message]:
    async with sm() as s:
        return {m.external_id: m for m in (await s.execute(select(Message))).scalars()}


async def _jobs(sm: SM) -> dict[str, AgentJob]:
    async with sm() as s:
        rows = (await s.execute(select(Message.external_id, AgentJob).join(AgentJob))).all()
    return {row[0]: row[1] for row in rows}


async def test_first_sync_triages_only_the_newest_n(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    settings = make_settings(backfill_auto_triage=10)
    await setup_accounts(sessionmaker, settings, fake=True)
    # 25 history messages over two pages (m0 oldest ... m24 newest)
    history = [msg(i, received_at=T0 + timedelta(minutes=i)) for i in range(25)]
    fake_connector.pages = [
        FetchResult(messages=history[:15], cursor="p1", has_more=True),
        FetchResult(messages=history[15:], cursor="p2"),
    ]
    kicked: list[int] = []
    async with httpx.AsyncClient() as http:
        sch = Scheduler(
            sessionmaker, settings, http, asyncio.Event(), on_new_jobs=lambda: kicked.append(1)
        )
        await sch.tick()

    msgs = await _messages(sessionmaker)
    assert len(msgs) == 25  # everything is stored
    newest = {f"m{i}" for i in range(15, 25)}
    assert {e for e, m in msgs.items() if m.agent_status == "pending"} == newest
    older = [m for e, m in msgs.items() if e not in newest]
    assert all(m.agent_status == "skipped" for m in older)
    assert all(m.agent_error == BACKFILL_SKIP_NOTE for m in older)

    jobs = await _jobs(sessionmaker)
    assert set(jobs) == newest
    assert {j.priority for j in jobs.values()} == {PRIORITY_BACKFILL}
    assert kicked  # the agent is told about the queued backfill

    # The next (live) poll queues new messages at live priority, nothing else changes.
    fake_connector.pages = [FetchResult(messages=[msg(99, received_at=T0 + timedelta(1))])]
    await make_due(sessionmaker, "fake")
    async with httpx.AsyncClient() as http:
        await Scheduler(sessionmaker, settings, http, asyncio.Event()).tick()
    jobs = await _jobs(sessionmaker)
    assert jobs["m99"].priority == PRIORITY_LIVE
    assert len(jobs) == 11


async def test_backfill_auto_triage_zero_triages_nothing(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    settings = make_settings(backfill_auto_triage=0)
    await setup_accounts(sessionmaker, settings, fake=True)
    fake_connector.pages = [FetchResult(messages=[msg(1), msg(2)], cursor="c")]
    async with httpx.AsyncClient() as http:
        await Scheduler(sessionmaker, settings, http, asyncio.Event()).tick()
    assert {m.agent_status for m in (await _messages(sessionmaker)).values()} == {"skipped"}
    assert await _jobs(sessionmaker) == {}


async def test_skipped_backfill_can_be_triaged_on_request(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    settings = make_settings(backfill_auto_triage=0)
    accounts = await setup_accounts(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        await ingest_messages(s, accounts["fake"], [msg(1)], backfill=True)
    (m,) = (await _messages(sessionmaker)).values()
    assert m.agent_status == "skipped"

    async with sessionmaker() as s, s.begin():  # what the Step 3 "Triage" button will call
        await enqueue(s, m.id, "triage")
    (j,) = (await _jobs(sessionmaker)).values()
    assert j.priority == PRIORITY_USER
    await worker(sessionmaker).tick()
    async with sessionmaker() as s:
        done = await s.get(Message, m.id)
    assert done is not None
    assert done.agent_status == "done"
    assert done.agent_error is None


async def test_dashboard_and_live_jobs_are_claimed_before_backfill(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    settings = make_settings(agent_concurrency=1)
    accounts = await setup_accounts(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        # backfill first (oldest run_after), then live, then a dashboard action
        await ingest_messages(s, accounts["fake"], [msg(1)], job_priority=PRIORITY_BACKFILL)
        await ingest_messages(s, accounts["fake"], [msg(2)])
        await ingest_messages(s, accounts["fake"], [msg(3)], job_priority=PRIORITY_USER)
    t = FakeLLM()
    w = worker(sessionmaker, settings=settings, t=t)
    for _ in range(3):
        assert await w.tick() == 1
    order = [
        c.prompt.split("<message>\n")[1].split("\n")[0]
        for c in t.calls
        if c.schema == "TriageResult"
    ]
    assert order == ["body 3", "body 2", "body 1"]


async def test_reasoning_tokens_are_persisted(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    from app.agent.llm import record_usage

    class ReasoningFake(FakeLLM):
        async def generate(self, schema, *, system, prompt, max_tokens):  # type: ignore[no-untyped-def]
            result = await super().generate(
                schema, system=system, prompt=prompt, max_tokens=max_tokens
            )
            record_usage(self.model_name, 0, 0, self.usage, reasoning=40)
            return result

    settings = make_settings()
    accounts = await setup_accounts(sessionmaker, settings)
    async with sessionmaker() as s, s.begin():
        await ingest_messages(s, accounts["fake"], [msg(1)])
    await worker(sessionmaker, t=ReasoningFake()).tick()
    (j,) = (await _jobs(sessionmaker)).values()
    assert j.reasoning_tokens == 40
