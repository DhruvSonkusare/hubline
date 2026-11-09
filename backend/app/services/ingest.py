"""Ingest normalised messages: upsert thread, insert message with (source, external_id) dedup.

Shared by polling and (from Step 5) webhooks. The caller owns the transaction, so the cursor
update and (from Step 2) agent-job enqueue commit atomically with the messages.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.models import NormalizedMessage
from app.models import ConnectorAccount, Message, Thread
from app.models.message import BODY_MAX_CHARS
from app.services.jobs import BACKFILL_SKIP_NOTE, PRIORITY_LIVE, enqueue_triage

RAW_MAX_BYTES = 64_000


@dataclass
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    new_inbound_ids: list[uuid.UUID] = field(default_factory=list)
    jobs_enqueued: int = 0


async def ingest_messages(
    session: AsyncSession,
    account: ConnectorAccount,
    messages: Sequence[NormalizedMessage],
    *,
    backfill: bool = False,
    job_priority: int = PRIORITY_LIVE,
) -> IngestResult:
    """Store messages and (unless `backfill`) queue triage for new inbound ones.

    backfill=True (a connector's first sync): every message is stored, but inbound ones are
    marked agent_status='skipped' and NOT queued; the caller then promotes only the newest
    BACKFILL_AUTO_TRIAGE of them (services.jobs.promote_newest_backfill) at lowest priority.
    """
    result = IngestResult()
    self_ids = set(account.self_ids or ())
    inbound_status = "skipped" if backfill else "pending"

    for msg in sorted(messages, key=lambda m: m.received_at):
        if msg.source != account.source:
            raise ValueError(
                f"connector {account.source!r} produced a message for source {msg.source!r}"
            )
        received_at = msg.received_at.astimezone(UTC)
        direction = "outbound" if msg.sender.id in self_ids else msg.direction

        thread_id = await _upsert_thread(session, msg, received_at)

        stmt = (
            insert(Message)
            .values(
                connector_account_id=account.id,
                source=msg.source,
                external_id=msg.external_id,
                thread_id=thread_id,
                external_thread_id=msg.thread_id,
                direction=direction,
                sender_id=msg.sender.id,
                sender_name=msg.sender.name,
                subject=msg.subject,
                body=msg.body[:BODY_MAX_CHARS],
                received_at=received_at,
                permalink=msg.permalink,
                reply_meta=msg.reply_meta,
                raw=_trim_raw(msg.raw),
                is_demo=msg.is_demo or account.is_demo,
                # outbound rows never go to the agent
                agent_status=inbound_status if direction == "inbound" else "skipped",
                agent_error=BACKFILL_SKIP_NOTE if backfill and direction == "inbound" else None,
            )
            .on_conflict_do_nothing(index_elements=["source", "external_id"])
            .returning(Message.id)
        )
        new_id = (await session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            result.duplicates += 1
            continue

        result.inserted += 1
        if direction == "inbound":
            result.new_inbound_ids.append(new_id)
        else:
            # You answered in the app itself: earlier open messages in the thread are handled.
            await session.execute(
                update(Message)
                .where(
                    Message.thread_id == thread_id,
                    Message.direction == "inbound",
                    Message.status == "open",
                    Message.received_at <= received_at,
                )
                .values(status="replied", status_changed_at=func.now())
            )

    # Same transaction as the inserts: a committed live message always has its triage job.
    if not backfill:
        result.jobs_enqueued = await enqueue_triage(
            session, result.new_inbound_ids, priority=job_priority
        )
    return result


async def _upsert_thread(
    session: AsyncSession, msg: NormalizedMessage, received_at: datetime
) -> uuid.UUID:
    base = insert(Thread).values(
        source=msg.source,
        external_thread_id=msg.thread_id,
        title=msg.thread_title or msg.subject,
        last_message_at=received_at,
    )
    stmt = base.on_conflict_do_update(
        index_elements=["source", "external_thread_id"],
        set_={
            "last_message_at": func.greatest(Thread.last_message_at, base.excluded.last_message_at),
            "title": func.coalesce(Thread.title, base.excluded.title),
        },
    ).returning(Thread.id)
    thread_id: uuid.UUID = (await session.execute(stmt)).scalar_one()
    return thread_id


def _trim_raw(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    encoded = json.dumps(raw, default=str)
    if len(encoded) > RAW_MAX_BYTES:
        return {"_truncated": True, "_original_bytes": len(encoded)}
    return raw
