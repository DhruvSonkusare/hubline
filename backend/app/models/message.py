from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MESSAGE_STATUSES = ("open", "replied", "dismissed")
BODY_MAX_CHARS = 50_000


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    source: Mapped[str] = mapped_column(Text)
    external_thread_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    participants: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("source", "external_thread_id"),)


class Message(Base):
    """Inbound and outbound messages in the common format.

    Agent columns (priority, summary, ...) are added by migration 0002 in Step 2 (plan R1).
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("connector_accounts.id", ondelete="RESTRICT")
    )
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("threads.id"))
    external_thread_id: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(Text)
    sender_id: Mapped[str] = mapped_column(Text)
    sender_name: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    permalink: Mapped[str | None] = mapped_column(Text)
    reply_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("status IN ('open', 'replied', 'dismissed')", name="status"),
        Index(
            "ix_messages_feed",
            text("received_at DESC"),
            text("id DESC"),
            postgresql_where=text("direction = 'inbound'"),
        ),
        Index(
            "ix_messages_status_received",
            "status",
            text("received_at DESC"),
            postgresql_where=text("direction = 'inbound'"),
        ),
        Index("ix_messages_source_received", "source", text("received_at DESC")),
        Index("ix_messages_thread", "thread_id", "received_at"),
    )
