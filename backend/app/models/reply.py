from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

REPLY_STATUSES = ("sending", "sent", "failed", "unknown")


class OutboundReply(Base):
    """A human-approved reply. Sends are at-most-once: the row is written as 'sending' BEFORE
    calling connector.send(); a crash leaves it 'sending' -> reaped to 'unknown' (never resent)."""

    __tablename__ = "outbound_replies"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("messages.id"))
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("connector_accounts.id")
    )
    external_thread_id: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    external_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('sending', 'sent', 'failed', 'unknown')", name="status"),
        Index("ix_outbound_replies_message", "message_id"),
    )
