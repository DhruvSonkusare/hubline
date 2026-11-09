"""Human actions on a message: save a draft, approve and send (at-most-once), dismiss/reopen.

Approve and send is at-most-once. The flow:
  1. txn A (message row locked): validate, keep the edited text as a human draft version, and
     insert outbound_replies(status='sending', idempotency_key). A repeated key returns the
     existing reply, and an in-flight/sent reply blocks a second one, so a double click or two
     tabs can never send twice.
  2. connector.send() OUTSIDE any transaction.
  3. txn B: mark 'sent' + store the outbound message in the thread (-> message 'replied'), or
     mark 'failed' (message stays open, the edited draft is kept).
A crash between 1 and 3 leaves 'sending'; reap_stale_sending() turns it into 'unknown' at the
next start. Nothing is ever re-sent automatically.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors import registry
from app.connectors.models import NormalizedMessage, ReplyContext, Sender
from app.errors import ApiError, not_found
from app.models import ConnectorAccount, Draft, Message, OutboundReply
from app.services.connector_runtime import build_connector
from app.services.ingest import ingest_messages

log = structlog.get_logger(__name__)

SEND_TIMEOUT_S = 30.0
STALE_SENDING_S = 120


async def _locked_message(session: AsyncSession, message_id: uuid.UUID) -> Message:
    msg = await session.get(Message, message_id, with_for_update=True)
    if msg is None or msg.direction != "inbound":
        raise not_found("Message")
    return msg


async def _current_draft(session: AsyncSession, message_id: uuid.UUID) -> Draft | None:
    return (
        await session.execute(
            select(Draft)
            .where(Draft.message_id == message_id)
            .order_by(Draft.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _add_human_version(session: AsyncSession, msg: Message, body: str) -> Draft:
    current = await _current_draft(session, msg.id)
    draft = Draft(
        message_id=msg.id,
        version=(current.version if current else 0) + 1,
        body=body,
        author="human",
        # keep the agent's open questions visible next to the edited text
        open_questions=list(current.open_questions) if current else [],
        model=None,
    )
    session.add(draft)
    await session.flush()
    return draft


async def save_human_draft(session: AsyncSession, message_id: uuid.UUID, body: str) -> Draft:
    msg = await _locked_message(session, message_id)
    if msg.status != "open":
        raise ApiError(409, "invalid_state", f"message is {msg.status}; reopen it to edit")
    current = await _current_draft(session, msg.id)
    if current is not None and current.body == body:
        return current  # unchanged: no new version
    draft = await _add_human_version(session, msg, body)
    await session.commit()
    return draft


async def set_status(session: AsyncSession, message_id: uuid.UUID, target: str) -> Message:
    msg = await _locked_message(session, message_id)
    allowed = {"dismissed": ("open",), "open": ("dismissed",)}[target]
    if msg.status not in allowed:
        raise ApiError(409, "invalid_state", f"cannot change a {msg.status} message to {target}")
    msg.status = target
    msg.status_changed_at = datetime.now(UTC)
    await session.commit()
    return msg


async def approve_and_send(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    message_id: uuid.UUID,
    body: str,
    idempotency_key: uuid.UUID,
    actor: str,
) -> OutboundReply:
    body = body.strip()
    # ---- txn A: validate + record intent
    async with sessionmaker() as session, session.begin():
        existing = (
            await session.execute(
                select(OutboundReply).where(OutboundReply.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.message_id != message_id:
                raise ApiError(409, "invalid_state", "idempotency key belongs to another message")
            return existing  # same click delivered twice: same answer

        msg = await _locked_message(session, message_id)
        account = await session.get(ConnectorAccount, msg.connector_account_id)
        assert account is not None
        try:
            desc = registry.get(msg.source).describe()
        except registry.UnknownConnectorError:
            raise ApiError(503, "connector_unavailable", f"{msg.source} is not installed") from None
        if not desc.can_send:
            raise ApiError(
                409, "connector_read_only", f"{desc.display_name} is read-only: copy the draft"
            )
        if msg.status == "replied":
            raise ApiError(409, "already_replied", "this message has already been answered")
        if msg.status != "open":
            raise ApiError(409, "invalid_state", f"message is {msg.status}")
        if not body:
            raise ApiError(422, "validation_error", "the reply is empty")
        if desc.max_reply_chars and len(body) > desc.max_reply_chars:
            raise ApiError(
                422, "validation_error", f"the reply is longer than {desc.max_reply_chars} chars"
            )
        if account.status != "connected":
            raise ApiError(503, "connector_unavailable", f"{msg.source} is {account.status}")
        in_flight = (
            (
                await session.execute(
                    select(OutboundReply.status).where(
                        OutboundReply.message_id == msg.id,
                        OutboundReply.status.in_(("sending", "sent", "unknown")),
                    )
                )
            )
            .scalars()
            .first()
        )
        if in_flight == "sent":
            raise ApiError(409, "already_replied", "this message has already been answered")
        if in_flight is not None:
            raise ApiError(
                409, "send_in_progress", f"a reply is already {in_flight}; check the app first"
            )

        current = await _current_draft(session, msg.id)
        if current is None or current.body != body:
            await _add_human_version(session, msg, body)  # the edited draft is never lost

        reply_id = (
            await session.execute(
                insert(OutboundReply)
                .values(
                    message_id=msg.id,
                    connector_account_id=account.id,
                    external_thread_id=msg.external_thread_id,
                    body=body,
                    status="sending",
                    idempotency_key=idempotency_key,
                    approved_by=actor,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(OutboundReply.id)
            )
        ).scalar_one_or_none()
        if reply_id is None:  # raced with the same key
            again = (
                await session.execute(
                    select(OutboundReply).where(OutboundReply.idempotency_key == idempotency_key)
                )
            ).scalar_one()
            return again
        reply_to = ReplyContext(
            in_reply_to_external_id=msg.external_id,
            subject=msg.subject,
            recipient=Sender(id=msg.sender_id, name=msg.sender_name),
            reply_meta=dict(msg.reply_meta or {}),
        )
        source, thread, is_demo = msg.source, msg.external_thread_id, msg.is_demo
        me = account.account_identity or "me"

    # ---- send, no transaction open
    try:
        connector = build_connector(account, settings, http)
        result = await asyncio.wait_for(
            connector.send(thread, body, reply_to=reply_to), timeout=SEND_TIMEOUT_S
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("send_failed", message_id=str(message_id), error=error)
        async with sessionmaker() as session, session.begin():
            await session.execute(
                update(OutboundReply)
                .where(OutboundReply.id == reply_id)
                .values(status="failed", error=error)
            )
        raise ApiError(
            502, "upstream_error", f"sending failed: {error}", {"reply_id": str(reply_id)}
        ) from exc

    # ---- txn B: record success
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(OutboundReply)
            .where(OutboundReply.id == reply_id)
            .values(status="sent", external_id=result.external_id, sent_at=result.sent_at)
        )
        account_row = await session.get(ConnectorAccount, account.id)
        assert account_row is not None
        await ingest_messages(
            session,
            account_row,
            [
                NormalizedMessage(
                    source=source,
                    external_id=result.external_id,
                    thread_id=thread,
                    sender=Sender(id=me, name="You"),
                    body=body,
                    received_at=result.sent_at,
                    direction="outbound",
                    is_demo=is_demo,
                )
            ],
        )
        await session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(status="replied", status_changed_at=func.now())
        )
        sent = await session.get(OutboundReply, reply_id)
        assert sent is not None
    log.info("reply_sent", message_id=str(message_id), source=source, actor=actor)
    return sent


async def reap_stale_sending(
    sessionmaker: async_sessionmaker[AsyncSession], older_than_s: int = STALE_SENDING_S
) -> int:
    """At start-up: 'sending' rows left by a crash become 'unknown' (never auto-resent)."""
    async with sessionmaker() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "UPDATE outbound_replies SET status = 'unknown', error = "
                    "'the app stopped while sending; check the app before trying again' "
                    "WHERE status = 'sending' AND created_at < now() - make_interval(secs => :s) "
                    "RETURNING id"
                ),
                {"s": float(older_than_s)},
            )
        ).all()
    if rows:
        log.warning("stale_sending_replies_marked_unknown", count=len(rows))
    return len(rows)
