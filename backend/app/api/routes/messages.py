from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.errors import ApiError
from app.schemas.common import ErrorBody
from app.schemas.messages import AgentStatus, MessagePage, SortOrder, StatusFilter
from app.services.feed import list_messages

router = APIRouter(prefix="/api/messages", tags=["messages"])


@router.get(
    "",
    response_model=MessagePage,
    responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
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
