from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session, require_service_token
from app.errors import ApiError, not_found
from app.models import Message
from app.runtime import Runtime
from app.schemas.common import ErrorBody
from app.schemas.messages import (
    AgentStatus,
    ApproveIn,
    DraftIn,
    DraftOut,
    JobQueued,
    MessageDetail,
    MessagePage,
    MessageStats,
    MessageSummary,
    RedraftIn,
    ReplyOut,
    SortOrder,
    StatusFilter,
)
from app.services import replies
from app.services.feed import get_message_detail, list_messages, message_stats
from app.services.jobs import ActiveJobExistsError, enqueue

router = APIRouter(prefix="/api/messages", tags=["messages"])

_ERRORS: dict[int | str, dict[str, object]] = {
    s: {"model": ErrorBody} for s in (400, 401, 404, 409, 422, 502, 503)
}


@router.get("", response_model=MessagePage, responses=_ERRORS)
async def get_messages(
    source: list[str] = Query(default=[], description="Filter by source; repeatable"),
    status: StatusFilter = Query(default="open"),
    priority: list[int] = Query(default=[], description="Exact priorities (1-5); repeatable"),
    min_priority: int | None = Query(default=None, ge=1, le=5),
    agent_status: list[AgentStatus] = Query(default=[], description="Repeatable"),
    sort: SortOrder = Query(default="received"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> MessagePage:
    """Unified feed of inbound messages (keyset pagination)."""
    if any(not 1 <= p <= 5 for p in priority):
        raise ApiError(422, "validation_error", "priority must be between 1 and 5")
    return await list_messages(
        session,
        sources=source,
        status=status,
        priorities=priority,
        min_priority=min_priority,
        agent_statuses=agent_status,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )


@router.get("/stats", response_model=MessageStats, responses=_ERRORS)
async def get_stats(session: AsyncSession = Depends(get_session)) -> MessageStats:
    """Counts for the sidebar badges."""
    return await message_stats(session)


@router.get("/{message_id}", response_model=MessageDetail, responses=_ERRORS)
async def get_message(
    message_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> MessageDetail:
    return await get_message_detail(session, message_id)


@router.put("/{message_id}/draft", response_model=DraftOut, responses=_ERRORS)
async def put_draft(
    message_id: uuid.UUID, body: DraftIn, session: AsyncSession = Depends(get_session)
) -> DraftOut:
    """Save your edit as a new human draft version (no-op if unchanged)."""
    draft = await replies.save_human_draft(session, message_id, body.body)
    return DraftOut.model_validate(draft, from_attributes=True)


@router.post("/{message_id}/approve", response_model=ReplyOut, responses=_ERRORS)
async def approve(
    message_id: uuid.UUID,
    body: ApproveIn,
    actor: str = Depends(require_service_token),
    runtime: Runtime = Depends(get_runtime),
) -> ReplyOut:
    """Approve and send through the message's own app. At-most-once per idempotency_key."""
    reply = await replies.approve_and_send(
        runtime.sessionmaker,
        runtime.settings,
        runtime.http,
        message_id=message_id,
        body=body.body,
        idempotency_key=body.idempotency_key,
        actor=actor,
    )
    return ReplyOut.model_validate(reply, from_attributes=True)


async def _queue(
    session: AsyncSession,
    runtime: Runtime,
    message_id: uuid.UUID,
    kind: Literal["triage", "redraft"],
    instructions: str | None = None,
) -> JobQueued:
    msg = await session.get(Message, message_id)
    if msg is None or msg.direction != "inbound":
        raise not_found("Message")
    if kind == "redraft" and msg.status != "open":
        raise ApiError(409, "invalid_state", f"message is {msg.status}; reopen it to redraft")
    try:
        job_id = await enqueue(session, message_id, kind, instructions=instructions)
    except ActiveJobExistsError:
        raise ApiError(409, "job_active", "the agent is already working on this message") from None
    await session.commit()
    if runtime.runner is not None:
        runtime.runner.kick_agent()
    return JobQueued(job_id=job_id)


@router.post("/{message_id}/triage", response_model=JobQueued, status_code=202, responses=_ERRORS)
async def triage(
    message_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> JobQueued:
    """The Triage / Retry agent button: (re-)run the agent now, ahead of live and backfill."""
    return await _queue(session, runtime, message_id, "triage")


@router.post("/{message_id}/redraft", response_model=JobQueued, status_code=202, responses=_ERRORS)
async def redraft(
    message_id: uuid.UUID,
    body: RedraftIn | None = None,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> JobQueued:
    """New agent draft, optionally following your instructions ("shorter", "more formal")."""
    instructions = (body.instructions or "").strip() or None if body else None
    return await _queue(session, runtime, message_id, "redraft", instructions)


@router.post("/{message_id}/dismiss", response_model=MessageSummary, responses=_ERRORS)
async def dismiss(
    message_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> MessageSummary:
    await replies.set_status(session, message_id, "dismissed")
    return await get_message_detail(session, message_id)


@router.post("/{message_id}/reopen", response_model=MessageSummary, responses=_ERRORS)
async def reopen(
    message_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> MessageSummary:
    await replies.set_status(session, message_id, "open")
    return await get_message_detail(session, message_id)
