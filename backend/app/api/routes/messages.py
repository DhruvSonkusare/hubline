from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.schemas.common import ErrorBody
from app.schemas.messages import MessagePage, StatusFilter
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
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> MessagePage:
    """Unified feed of inbound messages, newest first (keyset pagination)."""
    return await list_messages(session, sources=source, status=status, cursor=cursor, limit=limit)
