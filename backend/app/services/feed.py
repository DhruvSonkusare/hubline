"""Read-side queries for the dashboard feed (keyset pagination).

sort=received: ORDER BY received_at DESC, id DESC
sort=priority: ORDER BY COALESCE(priority, 0) DESC, received_at DESC, id DESC
(unscored messages sort last; matches index ix_messages_status_priority)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, exists, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import registry
from app.errors import ApiError
from app.models import Draft, Message, Thread
from app.schemas.common import decode_cursor, encode_cursor
from app.schemas.messages import (
    PREVIEW_CHARS,
    AgentStatus,
    MessagePage,
    MessageSummary,
    SenderOut,
    SortOrder,
    StatusFilter,
)


def _display_name(source: str) -> str:
    try:
        return registry.get(source).describe().display_name
    except registry.UnknownConnectorError:
        return source


def _preview(body: str) -> str:
    flat = " ".join(body.split())
    return flat if len(flat) <= PREVIEW_CHARS else flat[: PREVIEW_CHARS - 1] + "…"


async def list_messages(
    session: AsyncSession,
    *,
    sources: Sequence[str] = (),
    status: StatusFilter = "open",
    priorities: Sequence[int] = (),
    min_priority: int | None = None,
    agent_statuses: Sequence[AgentStatus] = (),
    sort: SortOrder = "received",
    cursor: str | None = None,
    limit: int = 50,
) -> MessagePage:
    prio_key = func.coalesce(Message.priority, 0)
    conditions: list[Any] = [Message.direction == "inbound"]
    if sources:
        conditions.append(Message.source.in_(list(sources)))
    if status != "all":
        conditions.append(Message.status == status)
    if priorities:
        conditions.append(Message.priority.in_(list(priorities)))
    if min_priority is not None:
        conditions.append(Message.priority >= min_priority)
    if agent_statuses:
        conditions.append(Message.agent_status.in_(list(agent_statuses)))
    if cursor:
        ts, row_id, cursor_prio = decode_cursor(cursor)
        if (sort == "priority") != (cursor_prio is not None):
            raise ApiError(400, "invalid_cursor", "Cursor does not match the sort order")
        if sort == "priority":
            conditions.append(
                tuple_(prio_key, Message.received_at, Message.id) < tuple_(cursor_prio, ts, row_id)
            )
        else:
            conditions.append(tuple_(Message.received_at, Message.id) < tuple_(ts, row_id))

    has_draft = exists().where(Draft.message_id == Message.id).correlate(Message)
    order: list[Any] = (
        [prio_key.desc(), Message.received_at.desc(), Message.id.desc()]
        if sort == "priority"
        else [Message.received_at.desc(), Message.id.desc()]
    )
    stmt = (
        select(Message, Thread.title, has_draft.label("has_draft"))
        .join(Thread, Thread.id == Message.thread_id)
        .where(and_(*conditions))
        .order_by(*order)
        .limit(limit + 1)
    )
    rows = (await session.execute(stmt)).all()
    more = len(rows) > limit
    rows = rows[:limit]

    items = [
        MessageSummary(
            id=m.id,
            source=m.source,
            source_display=_display_name(m.source),
            is_demo=m.is_demo,
            sender=SenderOut(id=m.sender_id, name=m.sender_name),
            subject=m.subject,
            thread_title=title,
            preview=_preview(m.body),
            received_at=m.received_at,
            status=m.status,
            thread_id=m.thread_id,
            priority=m.priority,
            summary=m.summary,
            needs_reply=m.needs_reply,
            category=m.category,
            agent_status=m.agent_status,
            agent_error=m.agent_error,
            has_draft=bool(draft_flag),
        )
        for m, title, draft_flag in rows
    ]
    next_cursor = None
    if more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor(
            last.received_at, last.id, (last.priority or 0) if sort == "priority" else None
        )
    return MessagePage(items=items, next_cursor=next_cursor)
