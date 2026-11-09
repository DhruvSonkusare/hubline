"""Free-tier guards in the agent worker: 429s defer (never fail), per-job token accounting, the
rolling 24h token budget, and prompt-size clipping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.fake import FakeLLM
from app.agent.llm import LLMRateLimitError
from app.agent.schemas import DraftResult, TriageResult
from app.config import Settings
from tests.conftest import FakeConnector, make_settings, msg
from tests.workers.test_agent_worker import jobs, reload, seed, triage, worker

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


async def test_rate_limit_defers_job_without_using_an_attempt(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    (m,) = await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, LLMRateLimitError("429 from provider", retry_after=120))
    w = worker(sessionmaker, t=t)
    before = datetime.now(UTC)
    assert await w.tick() == 1

    (j,) = await jobs(sessionmaker)
    assert (j.status, j.attempts) == ("pending", 0)  # deferred, not failed
    assert j.run_after >= before + timedelta(seconds=115)
    assert "rate limited" in (j.last_error or "")
    m = await reload(sessionmaker, m.id)
    assert m.agent_status == "pending"
    assert "retrying in 120s" in (m.agent_error or "")
    # the worker pauses instead of hammering the provider with the next job
    assert w.pause_remaining_s > 100
    assert await w.tick() == 0
    assert w.idle_reason == "rate_limited"


async def test_repeated_rate_limits_never_kill_the_job(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, *[LLMRateLimitError("429", retry_after=1)] * 5)
    w = worker(sessionmaker, t=t)
    for _ in range(5):
        w.paused_until = 0.0
        async with sessionmaker() as s, s.begin():
            await s.execute(text("UPDATE agent_jobs SET run_after = now()"))
        assert await w.tick() == 1
    (j,) = await jobs(sessionmaker)
    assert (j.status, j.attempts) == ("pending", 0)


async def test_tokens_are_recorded_per_job(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    needs_reply = msg(1).model_copy(update={"body": "Can you review the PR today?"})
    await seed(sessionmaker, settings, needs_reply)
    await worker(sessionmaker).tick()  # heuristic fake: triage + draft
    (j,) = await jobs(sessionmaker)
    assert j.llm_calls == 2
    assert j.input_tokens > 0
    assert j.output_tokens == 100  # the fake model reports 50 output tokens per call


async def test_tokens_from_failed_attempts_still_count(
    sessionmaker: SM, settings: Settings, fake_connector: type[FakeConnector]
) -> None:
    await seed(sessionmaker, settings)
    t = FakeLLM().queue(TriageResult, triage(5))
    d = FakeLLM().queue(DraftResult, RuntimeError("draft blew up"))
    await worker(sessionmaker, t=t, d=d).tick()
    (j,) = await jobs(sessionmaker)
    assert j.status == "pending"
    assert j.llm_calls == 1  # the successful triage call still counts toward the budget


async def test_daily_token_budget_stops_claiming(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(agent_max_tokens_per_day=50)
    await seed(sessionmaker, s, msg(1), msg(2))
    async with sessionmaker() as sess, sess.begin():
        await sess.execute(text("UPDATE agent_jobs SET input_tokens = 60 WHERE id = 1"))
    w = worker(sessionmaker, settings=s)
    assert await w.tick() == 0
    assert w.idle_reason == "budget"


async def test_long_message_body_is_clipped_for_the_prompt(
    sessionmaker: SM, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(agent_body_chars=200)
    await seed(sessionmaker, s, msg(1).model_copy(update={"body": "x" * 5000}))
    t = FakeLLM()
    await worker(sessionmaker, settings=s, t=t).tick()
    prompt = t.calls[0].prompt
    assert "x" * 200 in prompt
    assert "x" * 201 not in prompt
    assert "[truncated]" in prompt
