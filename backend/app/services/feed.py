"""Read-side queries for the dashboard feed (keyset pagination on received_at DESC, id DESC)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import and_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import registry
from app.models import Message, Thread
from app.schemas.common import decode_cursor, encode_cursor
from app.schemas.messages import (
    PREVIEW_CHARS,
    MessagePage,
    MessageSummary,
    SenderOut,
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
    cursor: str | None = None,
    limit: int = 50,
) -> MessagePage:
    conditions = [Message.direction == "inbound"]
    if sources:
        conditions.append(Message.source.in_(list(sources)))
    if status != "all":
        conditions.append(Message.status == status)
    if cursor:
        ts, row_id = decode_cursor(cursor)
        conditions.append(tuple_(Message.received_at, Message.id) < tuple_(ts, row_id))

    stmt = (
        select(Message, Thread.title)
        .join(Thread, Thread.id == Message.thread_id)
        .where(and_(*conditions))
        .order_by(Message.received_at.desc(), Message.id.desc())
        .limit(limit + 1)
    )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
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
        )
        for m, title in rows
    ]
    next_cursor = (
        encode_cursor(rows[-1][0].received_at, rows[-1][0].id) if has_more and rows else None
    )
    return MessagePage(items=items, next_cursor=next_cursor)
