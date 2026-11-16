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
    Integer,
    LargeBinary,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

CONNECTOR_STATUSES = ("not_configured", "disconnected", "connected", "needs_reauth", "error")


class ConnectorAccount(Base):
    """One row per registered connector (single-user app: one account per source)."""

    __tablename__ = "connector_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    source: Mapped[str] = mapped_column(Text, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'disconnected'"))
    account_identity: Mapped[str | None] = mapped_column(Text)
    self_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sync_state: Mapped[SyncState] = relationship(back_populates="account", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "status IN ('not_configured', 'disconnected', 'connected', 'needs_reauth', 'error')",
            name="status",
        ),
    )


class SyncState(Base):
    """Polling cursor + lease per connector; the scheduler claims rows with SKIP LOCKED."""

    __tablename__ = "sync_state"

    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("connector_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    cursor: Mapped[str | None] = mapped_column(Text)
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped[ConnectorAccount] = relationship(back_populates="sync_state", lazy="raise")


class ConnectorCredential(Base):
    """Encrypted third-party tokens, kept apart so ordinary queries never load them."""

    __tablename__ = "connector_credentials"

    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("connector_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthState(Base):
    """Single-use OAuth state with its PKCE verifier (valid for OAUTH_STATE_TTL_S)."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    code_verifier: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_oauth_states_expires_at", "expires_at"),)
