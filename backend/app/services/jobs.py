"""Agent job queue: enqueue helpers. Claiming/processing lives in app/workers/agent_worker.py.

At most one pending/running job per message is guaranteed by the partial unique index
uq_agent_jobs_one_active, so enqueueing is idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentJob, Message

_ACTIVE = text("status IN ('pending', 'running')")


class ActiveJobExistsError(Exception):
    """A pending or running job already exists for this message."""


async def enqueue_triage(session: AsyncSession, message_ids: Sequence[uuid.UUID]) -> int:
    """Queue triage for new messages. Returns how many jobs were created."""
    if not message_ids:
        return 0
    stmt = (
        insert(AgentJob)
        .values([{"message_id": mid, "kind": "triage"} for mid in message_ids])
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
) -> int:
    """Queue a re-triage or redraft requested by the user. Raises if a job is already active."""
    payload = {"instructions": instructions} if instructions else {}
    stmt = (
        insert(AgentJob)
        .values(message_id=message_id, kind=kind, payload=payload)
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
