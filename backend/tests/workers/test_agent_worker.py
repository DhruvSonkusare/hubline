from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.fake import FakeLLM
from app.agent.llm import LLMOutputError, LLMRefusalError
from app.agent.prompts import PROMPT_VERSION
from app.agent.schemas import DraftResult, TriageResult
from app.config import Settings
from app.models import AgentJob, Draft, Message
from app.services.ingest import ingest_messages
from app.services.jobs import ActiveJobExistsError, enqueue
from app.workers.agent_worker import AgentWorker, IdleBackoff
from app.workers.maintenance import reap_stale_jobs
from tests.conftest import FakeConnector, make_settings, msg
from tests.helpers import setup_accounts

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


def triage(priority: int = 4, needs_reply: bool = True) -> TriageResult:
    return TriageResult(
        priority=priority,
        summary="Alice: needs an answer",
        needs_reply=needs_reply,
        category="question",
        reason="direct question",
    )


def worker(
    sm: SM,
    settings: Settings | None = None,
    t: FakeLLM | None = None,
    d: FakeLLM | None = None,
    name: str = "w",
) -> AgentWorker:
    return AgentWorker(
        sm,
        settings or make_settings(agent_retry_delays_s="0,0,0"),
        asyncio.Event(),
        triage_llm=t or FakeLLM(model_name="fake-triage"),
        draft_llm=d or FakeLLM(model_name="fake-draft"),
        instance_id=name,
    )


async def seed(sm: SM, settings: Settings, *msgs: Any) -> list[Message]:
    accounts = await setup_accounts(sm, settings)
    async with sm() as s, s.begin():
        await ingest_messages(s, accounts["fake"], list(msgs) or [msg(1)])
    async with sm() as s:
        return list((await s.execute(select(Message).order_by(Message.external_id))).scalars())


async def jobs(sm: SM) -> list[AgentJob]:
    async with sm() as s:
        return list((await s.execute(select(AgentJob).order_by(AgentJob.id))).scalars())


async def reload(sm: SM, message_id: Any) -> Message:
    async with sm() as s:
        m = await s.get(Message, message_id)
        assert m is not None
        return m


async def drafts(sm: SM, message_id: Any) -> list[Draft]:
    async with sm() as s:
        return list(
            (
                await s.execute(
                    select(Draft).where(Draft.message_id == message_id).order_by(Draft.version)
                )
            ).scalars()
        )


async def test_ingest_enqueues_one_triage_job_per_new_inbound_message(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    msgs = await seed(sessionmaker, settings, msg(1), msg(2))
    assert [m.agent_status for m in msgs] == ["pending", "pending"]
    js = await jobs(sessionmaker)
    assert [(j.kind, j.status) for j in js] == [("triage", "pending")] * 2


async def test_outbound_messages_get_no_job(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings, msg(1, direction="outbound"))
    assert m.agent_status == "skipped"
    assert await jobs(sessionmaker) == []


async def test_processes_job_and_persists_triage_and_draft(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    t = FakeLLM(model_name="fake-triage").queue(TriageResult, triage(4))
    d = FakeLLM(model_name="fake-draft").queue(
        DraftResult, DraftResult(body="Yes, will do.", tone="casual", open_questions=["When?"])
    )
    assert await worker(sessionmaker, t=t, d=d).tick() == 1

    m = await reload(sessionmaker, m.id)
    assert m.agent_status == "done"
    assert (m.priority, m.needs_reply, m.category) == (4, True, "question")
    assert m.summary == "Alice: needs an answer"
    assert m.agent_model == "fake-triage"
    assert m.prompt_version == PROMPT_VERSION
    assert m.triaged_at is not None
    (dr,) = await drafts(sessionmaker, m.id)
    assert (dr.version, dr.author, dr.body, dr.model) == (1, "agent", "Yes, will do.", "fake-draft")
    assert dr.open_questions == ["When?"]
    (j,) = await jobs(sessionmaker)
    assert (j.status, j.attempts) == ("succeeded", 1)
    assert j.finished_at is not None
    assert "Alice" in t.calls[0].prompt  # message content reached the prompt


async def test_low_priority_stores_triage_without_draft(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, triage(1, needs_reply=False))
    await worker(sessionmaker, t=t).tick()
    m = await reload(sessionmaker, m.id)
    assert (m.agent_status, m.priority) == ("done", 1)
    assert await drafts(sessionmaker, m.id) == []


async def test_thread_context_includes_earlier_messages(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    t0 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    await seed(
        sessionmaker,
        settings,
        msg(1, received_at=t0),
        msg(2, received_at=t0 + timedelta(minutes=5)),
    )
    t = FakeLLM()
    await worker(sessionmaker, t=t).tick()  # concurrency 2: both jobs
    prompts = sorted((c.prompt for c in t.calls if c.schema == "TriageResult"), key=len)
    assert "(no earlier messages)" in prompts[0]
    assert "body 1" in prompts[1].split("<message>")[0]  # m1 appears as thread context for m2


async def test_failure_is_retried_then_dead(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    boom = RuntimeError("provider down")
    t = FakeLLM().queue(TriageResult, boom, boom, boom)
    w = worker(sessionmaker, t=t)

    await w.tick()
    (j,) = await jobs(sessionmaker)
    assert (j.status, j.attempts) == ("pending", 1)
    m = await reload(sessionmaker, m.id)
    assert m.agent_status == "pending"
    assert "retrying" in (m.agent_error or "")

    await w.tick()
    await w.tick()
    (j,) = await jobs(sessionmaker)
    assert (j.status, j.attempts) == ("dead", 3)
    assert "provider down" in (j.last_error or "")
    m = await reload(sessionmaker, m.id)
    assert m.agent_status == "failed"
    assert (m.agent_error or "").startswith("triage failed")
    assert m.priority is None


async def test_retry_waits_for_run_after(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(agent_retry_delays_s="300")
    await seed(sessionmaker, s)
    t = FakeLLM().queue(TriageResult, RuntimeError("x"))
    w = worker(sessionmaker, settings=s, t=t)
    assert await w.tick() == 1
    assert await w.tick() == 0  # not due for 5 minutes
    (j,) = await jobs(sessionmaker)
    assert j.run_after > datetime.now(UTC) + timedelta(seconds=250)


async def test_refusal_is_not_retried(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, LLMRefusalError("declined"))
    await worker(sessionmaker, t=t).tick()
    (j,) = await jobs(sessionmaker)
    assert (j.status, j.attempts) == ("dead", 1)
    assert (await reload(sessionmaker, m.id)).agent_status == "failed"


async def test_invalid_output_counts_as_retryable_failure(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, LLMOutputError("a"), LLMOutputError("b"))
    await worker(sessionmaker, t=t).tick()
    (j,) = await jobs(sessionmaker)
    assert j.status == "pending"


async def test_redraft_creates_next_version_with_instructions(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, triage(4))
    d = FakeLLM().queue(
        DraftResult,
        DraftResult(body="Long first draft.", tone="formal"),
        DraftResult(body="Short.", tone="casual"),
    )
    w = worker(sessionmaker, t=t, d=d)
    await w.tick()

    async with sessionmaker() as s, s.begin():
        await enqueue(s, m.id, "redraft", instructions="make it shorter")
        with pytest.raises(ActiveJobExistsError):
            await enqueue(s, m.id, "redraft")
    assert (await reload(sessionmaker, m.id)).agent_status == "pending"

    await w.tick()
    v1, v2 = await drafts(sessionmaker, m.id)
    assert (v1.version, v2.version) == (1, 2)
    assert v2.body == "Short."
    assert v2.instructions == "make it shorter"
    assert "Long first draft." in d.calls[1].prompt
    assert len(t.calls) == 1  # redraft does not re-triage
    m = await reload(sessionmaker, m.id)
    assert (m.agent_status, m.priority) == ("done", 4)


async def test_failed_redraft_keeps_message_done(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    w = worker(sessionmaker, t=FakeLLM().queue(TriageResult, triage(4)))
    await w.tick()
    async with sessionmaker() as s, s.begin():
        await enqueue(s, m.id, "redraft")
    w.draft_llm.queue(DraftResult, LLMRefusalError("no"))  # type: ignore[attr-defined]
    await w.tick()
    m = await reload(sessionmaker, m.id)
    assert m.agent_status == "done"
    assert (m.agent_error or "").startswith("redraft failed")


async def test_budget_guard_pauses_claiming(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(agent_max_jobs_per_hour=2, agent_concurrency=5)
    await seed(sessionmaker, s, msg(1), msg(2), msg(3))
    w = worker(sessionmaker, settings=s)
    assert await w.tick() == 2  # only 2 of 3 within the hourly budget
    assert await w.tick() == 0
    pending = [j for j in await jobs(sessionmaker) if j.status == "pending"]
    assert len(pending) == 1


async def test_zero_budget_pauses_agent(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(agent_max_jobs_per_hour=0)
    await seed(sessionmaker, s)
    assert await worker(sessionmaker, settings=s).tick() == 0


async def test_reaper_requeues_stale_running_jobs(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m1, m2) = await seed(sessionmaker, settings, msg(1), msg(2))
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "UPDATE agent_jobs SET status='running', attempts=1, "
                "locked_at = now() - interval '20 minutes'"
            )
        )
        # second job is on its last attempt -> dead
        await s.execute(update(AgentJob).where(AgentJob.message_id == m2.id).values(attempts=3))
        await s.execute(update(Message).values(agent_status="running"))

    assert await reap_stale_jobs(sessionmaker, stale_s=600) == 2
    by_msg = {j.message_id: j for j in await jobs(sessionmaker)}
    assert by_msg[m1.id].status == "pending"
    assert by_msg[m2.id].status == "dead"
    assert (await reload(sessionmaker, m1.id)).agent_status == "pending"
    assert (await reload(sessionmaker, m2.id)).agent_status == "failed"

    # fresh running jobs are left alone
    assert await reap_stale_jobs(sessionmaker, stale_s=600) == 0


async def test_concurrent_workers_process_each_job_once(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    await seed(sessionmaker, settings, *(msg(i) for i in range(6)))
    shared_t, shared_d = FakeLLM(), FakeLLM()
    workers = [worker(sessionmaker, t=shared_t, d=shared_d, name=f"w{i}") for i in range(4)]
    total = 0
    for _ in range(5):
        total += sum(await asyncio.gather(*(w.tick() for w in workers)))
    assert total == 6
    triage_calls = [c for c in shared_t.calls if c.schema == "TriageResult"]
    assert len(triage_calls) == 6
    async with sessionmaker() as s:
        done = (
            await s.execute(select(func.count()).where(Message.agent_status == "done"))
        ).scalar_one()
    assert done == 6


async def test_run_loop_wakes_on_event(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(worker_idle_max_s=3600, worker_min_sleep_s=3600)
    await setup_accounts(sessionmaker, s)
    w = worker(sessionmaker, settings=s)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.3)  # worker is now asleep for an hour

    async with sessionmaker() as sess, sess.begin():
        accounts = await setup_accounts(sessionmaker, s)
        await ingest_messages(sess, accounts["fake"], [msg(1)])
    w.wake.set()
    for _ in range(60):
        if (await jobs(sessionmaker))[0].status == "succeeded":
            break
        await asyncio.sleep(0.05)
    assert (await jobs(sessionmaker))[0].status == "succeeded"
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def test_idle_backoff() -> None:
    b = IdleBackoff(2, 60)
    assert [b.next() for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    b.reset()
    assert b.next() == 2
