"""Agent worker: claims agent_jobs with FOR UPDATE SKIP LOCKED, runs the LangGraph agent, and
persists priority / summary / draft. It never sends anything.

Transactions are short: claim -> (commit) -> load context -> (no txn) LLM calls -> persist.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import build_graph, run_agent
from app.agent.llm import (
    LLMConfigError,
    LLMRateLimitError,
    LLMRefusalError,
    StructuredLLM,
    Usage,
    build_llm,
    job_usage,
)
from app.agent.prompts import PROMPT_VERSION
from app.agent.state import (
    DEFAULT_MAX_REPLY_CHARS,
    AgentState,
    MessageForAgent,
    OwnerProfile,
    ThreadItem,
    initial_state,
    result_summary,
)
from app.config import Settings
from app.connectors import registry
from app.models import AgentJob, ConnectorAccount, Draft, Message, Thread
from app.workers.maintenance import reap_stale_jobs
from app.workers.supervisor import sleep_or_wake

log = structlog.get_logger(__name__)

_CLAIM_SQL = text(
    """
    WITH claimed AS (
        UPDATE agent_jobs
        SET status = 'running', locked_at = now(), locked_by = :me,
            attempts = attempts + 1, updated_at = now()
        WHERE id IN (
            SELECT id FROM agent_jobs
            WHERE status = 'pending' AND run_after <= now()
            ORDER BY run_after, id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, message_id, kind, attempts, max_attempts, payload
    ), marked AS (
        UPDATE messages SET agent_status = 'running'
        FROM claimed WHERE messages.id = claimed.message_id
    )
    SELECT * FROM claimed
    """
)

_RECENT_STARTS_SQL = text(
    "SELECT count(*) FROM agent_jobs WHERE locked_at > now() - interval '1 hour'"
)
# Rolling 24h token use (free-tier TPD guard). Tokens are written on every attempt.
_TOKENS_24H_SQL = text(
    "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM agent_jobs "
    "WHERE updated_at > now() - interval '24 hours'"
)


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    message_id: uuid.UUID
    kind: str
    attempts: int
    max_attempts: int
    payload: dict[str, Any]


class IdleBackoff:
    """Exponential idle sleep: min_s, 2*min_s, ... capped at max_s; reset when work appears."""

    def __init__(self, min_s: float, max_s: float) -> None:
        self.min_s, self.max_s = min_s, max(min_s, max_s)
        self._next = min_s

    def reset(self) -> None:
        self._next = self.min_s

    def next(self) -> float:
        delay = self._next
        self._next = min(self._next * 2, self.max_s)
        return delay


class AgentWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        wake: asyncio.Event,
        *,
        triage_llm: StructuredLLM | None = None,
        draft_llm: StructuredLLM | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.wake = wake
        self.triage_llm = triage_llm or build_llm(settings, "triage")
        self.draft_llm = draft_llm or build_llm(settings, "draft")
        self.graph = build_graph(self.triage_llm, self.draft_llm)
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}"
        self.owner = OwnerProfile(
            name=settings.agent_owner_name,
            context=settings.agent_owner_context,
            timezone=settings.owner_timezone,
            vips=settings.vip_senders,
        )
        self._budget_logged = False
        self.paused_until = 0.0  # monotonic time; set when the provider rate-limits us
        self.idle_reason = "idle"  # why the last tick claimed nothing (for /internal/tick)

    @property
    def pause_remaining_s(self) -> float:
        return max(0.0, self.paused_until - time.monotonic())

    # ------------------------------------------------------------------ loop

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "agent_worker_started",
            instance=self.instance_id,
            triage_model=self.triage_llm.model_name,
            draft_model=self.draft_llm.model_name,
        )
        await reap_stale_jobs(self.sessionmaker, self.settings.agent_stale_after_s)
        last_maintenance = time.monotonic()
        backoff = IdleBackoff(self.settings.worker_min_sleep_s, self.settings.worker_idle_max_s)
        while not stop.is_set():
            if time.monotonic() - last_maintenance >= self.settings.agent_maintenance_interval_s:
                await reap_stale_jobs(self.sessionmaker, self.settings.agent_stale_after_s)
                last_maintenance = time.monotonic()
            processed = await self.tick()
            if processed:
                backoff.reset()
                delay = 0.0
            elif self.pause_remaining_s:
                delay = self.pause_remaining_s
            else:
                delay = backoff.next()
            await sleep_or_wake(self.wake, stop, delay)

    async def tick(self) -> int:
        """Claim up to AGENT_CONCURRENCY jobs (within the hourly and daily budgets) and process
        them. Returns the number processed; `idle_reason` says why it was 0."""
        if self.pause_remaining_s:
            self.idle_reason = "rate_limited"
            return 0
        limit = min(self.settings.agent_concurrency, await self._budget_left())
        if limit <= 0:
            self.idle_reason = "budget"
            return 0
        jobs = await self.claim(limit)
        if jobs:
            await asyncio.gather(*(self.process(job) for job in jobs))
        else:
            self.idle_reason = "drained"
        return len(jobs)

    async def _budget_left(self) -> int:
        """Jobs we may start now: hourly job cap, then the rolling 24h token budget."""
        cap = self.settings.agent_max_jobs_per_hour
        token_cap = self.settings.agent_max_tokens_per_day
        async with self.sessionmaker() as session:
            started = int((await session.execute(_RECENT_STARTS_SQL)).scalar_one())
            used = int((await session.execute(_TOKENS_24H_SQL)).scalar_one()) if token_cap else 0
        left = cap - started
        if token_cap and used >= token_cap:
            left = 0
        if left <= 0 and not self._budget_logged:
            log.warning(
                "agent_budget_exhausted",
                max_jobs_per_hour=cap,
                tokens_24h=used,
                max_tokens_per_day=token_cap,
            )
            self._budget_logged = True
        elif left > 0:
            self._budget_logged = False
        return left

    async def claim(self, limit: int) -> list[ClaimedJob]:
        async with self.sessionmaker() as session, session.begin():
            rows = (
                (await session.execute(_CLAIM_SQL, {"me": self.instance_id, "limit": limit}))
                .mappings()
                .all()
            )
        return [ClaimedJob(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ one job

    async def process(self, job: ClaimedJob) -> None:
        jlog = log.bind(job_id=job.id, kind=job.kind, message_id=str(job.message_id))
        usage = Usage()
        token = job_usage.set(usage)  # LLM calls in this task add their tokens here
        try:
            state = await self.load_state(job)
            if state is None:  # message deleted meanwhile
                await self._finish_job(job.id)
                return
            result = await asyncio.wait_for(
                run_agent(self.graph, state), timeout=self.settings.agent_job_timeout_s
            )
            await self.persist(job, result, usage)
            jlog.info("agent_job_done", tokens=usage.total_tokens, **result_summary(result))
        except asyncio.CancelledError:
            raise  # shutdown: the reaper re-queues the job on the next start
        except LLMRateLimitError as exc:
            jlog.warning("agent_job_deferred_rate_limited", retry_after_s=exc.retry_after)
            await self.defer(job, exc, usage)
        except (LLMRefusalError, LLMConfigError) as exc:
            jlog.warning("agent_job_failed_permanently", error=str(exc))
            await self.fail(job, exc, usage, permanent=True)
        except Exception as exc:
            jlog.exception("agent_job_failed", attempt=job.attempts)
            await self.fail(job, exc, usage)
        finally:
            job_usage.reset(token)

    async def load_state(self, job: ClaimedJob) -> AgentState | None:
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(Message, ConnectorAccount, Thread.title)
                    .join(ConnectorAccount, ConnectorAccount.id == Message.connector_account_id)
                    .join(Thread, Thread.id == Message.thread_id)
                    .where(Message.id == job.message_id)
                )
            ).first()
            if row is None:
                return None
            msg, account, thread_title = row
            history: list[Message] = []
            if self.settings.agent_thread_context:
                history = list(
                    (
                        await session.execute(
                            select(Message)
                            .where(
                                Message.thread_id == msg.thread_id,
                                Message.id != msg.id,
                                Message.received_at <= msg.received_at,
                            )
                            .order_by(Message.received_at.desc())
                            .limit(self.settings.agent_thread_context)
                        )
                    ).scalars()
                )
            previous_draft = (
                await session.execute(
                    select(Draft.body)
                    .where(Draft.message_id == msg.id)
                    .order_by(Draft.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        try:
            desc = registry.get(msg.source).describe()
            source_display, can_send = desc.display_name, desc.can_send
            max_chars = desc.max_reply_chars or DEFAULT_MAX_REPLY_CHARS
        except registry.UnknownConnectorError:
            source_display, can_send, max_chars = msg.source, False, DEFAULT_MAX_REPLY_CHARS

        raw_headers = (msg.raw or {}).get("headers") if isinstance(msg.raw, dict) else None
        headers = (
            {str(k).lower(): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        message = MessageForAgent(
            id=str(msg.id),
            source=msg.source,
            source_display=source_display,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            subject=msg.subject,
            thread_title=thread_title,
            body=_clip(msg.body, self.settings.agent_body_chars),
            received_at=msg.received_at,
            direction="outbound" if msg.direction == "outbound" else "inbound",
            can_send=can_send,
            max_reply_chars=max_chars,
            headers=headers,
        )
        owner = OwnerProfile(
            name=self.owner.name,
            context=self.owner.context,
            timezone=self.owner.timezone,
            vips=self.owner.vips,
            self_ids=frozenset(account.self_ids or ()),
        )
        thread = [
            ThreadItem(
                direction="outbound" if h.direction == "outbound" else "inbound",
                sender_name=h.sender_name or h.sender_id,
                body=_clip(h.body, self.settings.agent_thread_item_chars),
                received_at=h.received_at,
            )
            for h in reversed(history)
        ]
        instructions = job.payload.get("instructions") if job.payload else None
        return initial_state(
            job_kind="redraft" if job.kind == "redraft" else "triage",
            message=message,
            owner=owner,
            thread=thread,
            previous_draft=previous_draft,
            redraft_instructions=instructions,
        )

    async def persist(self, job: ClaimedJob, state: AgentState, usage: Usage | None = None) -> None:
        now = datetime.now(UTC)
        triage = state.get("triage")
        draft = state.get("draft")
        draft_error = state.get("draft_error")
        async with self.sessionmaker() as session, session.begin():
            # Lock the message row: serialises draft versioning with human edits (Step 3).
            msg = await session.get(Message, job.message_id, with_for_update=True)
            if msg is None:
                await self._finish_job(job.id, session=session)
                return
            if state.get("outcome") == "skipped":
                msg.agent_status = "skipped"
                msg.agent_error = None
            elif job.kind == "triage" and triage is not None:
                msg.priority = triage.priority
                msg.summary = triage.summary
                msg.needs_reply = triage.needs_reply
                msg.category = triage.category
                msg.priority_reason = triage.reason
                msg.triaged_at = now
                msg.agent_model = (
                    "rules" if state.get("triage_by") == "rules" else self.triage_llm.model_name
                )
                msg.prompt_version = PROMPT_VERSION
                msg.agent_status = "done"
                msg.agent_error = draft_error
            else:  # redraft
                msg.agent_status = "done" if msg.triaged_at is not None else "failed"
                msg.agent_error = draft_error

            if draft is not None:
                current = (
                    await session.execute(
                        select(func.coalesce(func.max(Draft.version), 0)).where(
                            Draft.message_id == msg.id
                        )
                    )
                ).scalar_one()
                session.add(
                    Draft(
                        message_id=msg.id,
                        version=int(current) + 1,
                        body=draft.body,
                        author="agent",
                        instructions=state.get("redraft_instructions"),
                        open_questions=list(draft.open_questions),
                        model=self.draft_llm.model_name,
                    )
                )
            await self._finish_job(job.id, session=session, usage=usage)

    async def defer(self, job: ClaimedJob, exc: LLMRateLimitError, usage: Usage) -> None:
        """Rate limited: put the job back for retry_after seconds without using up an attempt,
        and pause the worker so the other jobs don't hit the same limit."""
        wait = exc.retry_after
        self.paused_until = max(self.paused_until, time.monotonic() + wait)
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(AgentJob)
                .where(AgentJob.id == job.id)
                .values(
                    status="pending",
                    attempts=AgentJob.attempts - 1,  # a 429 is not a failed attempt
                    run_after=func.now() + timedelta(seconds=wait),  # DB clock
                    last_error=f"rate limited: {exc}"[:500],
                    **_token_values(usage),
                    updated_at=func.now(),
                )
            )
            await session.execute(
                update(Message)
                .where(Message.id == job.message_id)
                .values(
                    agent_status="pending",
                    agent_error=f"rate limited by the LLM provider; retrying in {wait:.0f}s",
                )
            )

    async def fail(
        self,
        job: ClaimedJob,
        exc: BaseException,
        usage: Usage | None = None,
        *,
        permanent: bool = False,
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"[:500]
        delays = self.settings.retry_delays
        dead = permanent or job.attempts >= job.max_attempts
        async with self.sessionmaker() as session, session.begin():
            if dead:
                await session.execute(
                    update(AgentJob)
                    .where(AgentJob.id == job.id)
                    .values(
                        status="dead",
                        last_error=error,
                        finished_at=func.now(),
                        **_token_values(usage),
                        updated_at=func.now(),
                    )
                )
                msg = await session.get(Message, job.message_id, with_for_update=True)
                if msg is not None:
                    msg.agent_status = "failed" if msg.triaged_at is None else "done"
                    msg.agent_error = f"{job.kind} failed: {error}"
            else:
                delay = delays[min(job.attempts - 1, len(delays) - 1)]
                await session.execute(
                    update(AgentJob)
                    .where(AgentJob.id == job.id)
                    .values(
                        status="pending",
                        last_error=error,
                        run_after=func.now() + timedelta(seconds=delay),  # DB clock
                        **_token_values(usage),
                        updated_at=func.now(),
                    )
                )
                await session.execute(
                    update(Message)
                    .where(Message.id == job.message_id)
                    .values(agent_status="pending", agent_error=f"retrying: {error}")
                )

    async def _finish_job(
        self, job_id: int, *, session: AsyncSession | None = None, usage: Usage | None = None
    ) -> None:
        stmt = (
            update(AgentJob)
            .where(AgentJob.id == job_id)
            .values(
                status="succeeded",
                last_error=None,
                finished_at=func.now(),
                **_token_values(usage),
                updated_at=func.now(),
            )
        )
        if session is not None:
            await session.execute(stmt)
            return
        async with self.sessionmaker() as s, s.begin():
            await s.execute(stmt)


def _clip(text: str, limit: int) -> str:
    """Bound what we send to the LLM (free-tier token budget)."""
    return text if len(text) <= limit else text[:limit].rstrip() + " …[truncated]"


def _token_values(usage: Usage | None) -> dict[str, Any]:
    """Accumulate this attempt's token use onto the job row (counts toward the 24h budget)."""
    if usage is None or not usage.calls:
        return {}
    return {
        "input_tokens": AgentJob.input_tokens + usage.input_tokens,
        "output_tokens": AgentJob.output_tokens + usage.output_tokens,
        "llm_calls": AgentJob.llm_calls + usage.calls,
    }
