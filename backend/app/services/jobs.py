"""Agent job queue: enqueue helpers. Claiming/processing lives in app/workers/agent_worker.py.

At most one pending/running job per message is guaranteed by the partial unique index
uq_agent_jobs_one_active, so enqueueing is idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentJob, Message

_ACTIVE = text("status IN ('pending', 'running')")


# Claim order: higher first. Dashboard actions and live messages always beat backfill.
PRIORITY_USER = 20  # dashboard actions: Triage / Redraft / Retry, generated samples
PRIORITY_LIVE = 10  # new messages from regular polls and webhooks
PRIORITY_BACKFILL = 0  # first-sync history (only the newest BACKFILL_AUTO_TRIAGE per connector)

BACKFILL_SKIP_NOTE = (
    "Not auto-triaged: older backfilled message (only the newest are triaged automatically "
    "to protect the free LLM budget). Use Triage to run the agent on it."
)


class ActiveJobExistsError(Exception):
    """A pending or running job already exists for this message."""


async def enqueue_triage(
    session: AsyncSession,
    message_ids: Sequence[uuid.UUID],
    *,
    priority: int = PRIORITY_LIVE,
) -> int:
    """Queue triage for new messages. Returns how many jobs were created."""
    if not message_ids:
        return 0
    stmt = (
        insert(AgentJob)
        .values(
            [{"message_id": mid, "kind": "triage", "priority": priority} for mid in message_ids]
        )
        .on_conflict_do_nothing(index_elements=["message_id"], index_where=_ACTIVE)
        .returning(AgentJob.id)
    )
    return len((await session.execute(stmt)).all())


async def enqueue(
    session: AsyncSession,
    message_id: uuid.UUID,
    kind: Literal["triage", "redraft"],
    *,
    instructions: str | None = None,
    priority: int = PRIORITY_USER,
) -> int:
    """Queue a (re-)triage or redraft requested by the user, e.g. the Triage button on a
    skipped backfill message. Raises if a job is already active."""
    payload = {"instructions": instructions} if instructions else {}
    stmt = (
        insert(AgentJob)
        .values(message_id=message_id, kind=kind, payload=payload, priority=priority)
        .on_conflict_do_nothing(index_elements=["message_id"], index_where=_ACTIVE)
        .returning(AgentJob.id)
    )
    job_id = (await session.execute(stmt)).scalar_one_or_none()
    if job_id is None:
        raise ActiveJobExistsError(str(message_id))
    await session.execute(
        update(Message)
        .where(Message.id == message_id)
        .values(agent_status="pending", agent_error=None)
    )
    return int(job_id)


async def promote_newest_backfill(
    session: AsyncSession, message_ids: Sequence[uuid.UUID], limit: int
) -> int:
    """After a first sync: queue triage (lowest priority) for the newest `limit` of the
    backfilled inbound messages; the rest stay agent_status='skipped'."""
    if not message_ids or limit <= 0:
        return 0
    newest = (
        select(Message.id)
        .where(Message.id.in_(list(message_ids)), Message.direction == "inbound")
        .order_by(Message.received_at.desc(), Message.id.desc())
        .limit(limit)
    )
    promoted = list(
        (
            await session.execute(
                update(Message)
                .where(Message.id.in_(newest.scalar_subquery()))
                .values(agent_status="pending", agent_error=None)
                .returning(Message.id)
            )
        ).scalars()
    )
    return await enqueue_triage(session, promoted, priority=PRIORITY_BACKFILL)
