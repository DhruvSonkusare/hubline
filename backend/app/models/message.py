from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
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
AGENT_STATUSES = ("pending", "running", "done", "failed", "skipped")
JOB_KINDS = ("triage", "redraft")
JOB_STATUSES = ("pending", "running", "succeeded", "dead")
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

    # --- agent output (migration 0002, Step 2). 1:1 with the message, so stored inline.
    priority: Mapped[int | None] = mapped_column(SmallInteger)
    summary: Mapped[str | None] = mapped_column(Text)
    needs_reply: Mapped[bool | None] = mapped_column(Boolean)
    category: Mapped[str | None] = mapped_column(Text)
    priority_reason: Mapped[str | None] = mapped_column(Text)
    agent_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    agent_error: Mapped[str | None] = mapped_column(Text)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("status IN ('open', 'replied', 'dismissed')", name="status"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority"),
        CheckConstraint(
            "agent_status IN ('pending', 'running', 'done', 'failed', 'skipped')",
            name="agent_status",
        ),
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
        Index(
            "ix_messages_status_priority",
            "status",
            text("COALESCE(priority, 0) DESC"),
            text("received_at DESC"),
            postgresql_where=text("direction = 'inbound'"),
        ),
    )


class Draft(Base):
    """Versioned reply drafts; the current draft is the highest version."""

    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    open_questions: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("message_id", "version"),
        CheckConstraint("author IN ('agent', 'human')", name="author"),
    )


class AgentJob(Base):
    """Work queue for the agent worker (claimed with FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "agent_jobs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("3"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    last_error: Mapped[str | None] = mapped_column(Text)
    # LLM usage across all attempts (migration 0003): drives the rolling 24h token budget.
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    llm_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind IN ('triage', 'redraft')", name="kind"),
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'dead')", name="status"),
        Index("ix_agent_jobs_ready", "run_after", postgresql_where=text("status = 'pending'")),
        Index("ix_agent_jobs_message", "message_id"),
        Index(
            "uq_agent_jobs_one_active",
            "message_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )
