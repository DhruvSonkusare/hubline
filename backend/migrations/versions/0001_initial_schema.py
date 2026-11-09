"""Step 1 schema: connector_accounts, sync_state, threads, messages (core columns).

Revision ID: 0001
Revises:
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID_PK = sa.text("gen_random_uuid()")
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("id", sa.Uuid(), server_default=UUID_PK, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'disconnected'"), nullable=False),
        sa.Column("account_identity", sa.Text(), nullable=True),
        sa.Column(
            "self_ids",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('not_configured', 'disconnected', 'connected', 'needs_reauth', 'error')",
            name=op.f("ck_connector_accounts_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_accounts")),
        sa.UniqueConstraint("source", name=op.f("uq_connector_accounts_source")),
    )

    op.create_table(
        "sync_state",
        sa.Column("connector_account_id", sa.Uuid(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["connector_account_id"],
            ["connector_accounts.id"],
            name=op.f("fk_sync_state_connector_account_id_connector_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connector_account_id", name=op.f("pk_sync_state")),
    )

    op.create_table(
        "threads",
        sa.Column("id", sa.Uuid(), server_default=UUID_PK, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "participants",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_threads")),
        sa.UniqueConstraint(
            "source", "external_thread_id", name=op.f("uq_threads_source_external_thread_id")
        ),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=UUID_PK, nullable=False),
        sa.Column("connector_account_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.Text(), nullable=False),
        sa.Column("sender_name", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column(
            "reply_meta",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name=op.f("ck_messages_direction")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'replied', 'dismissed')", name=op.f("ck_messages_status")
        ),
        sa.ForeignKeyConstraint(
            ["connector_account_id"],
            ["connector_accounts.id"],
            name=op.f("fk_messages_connector_account_id_connector_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["threads.id"], name=op.f("fk_messages_thread_id_threads")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("source", "external_id", name=op.f("uq_messages_source_external_id")),
    )
    op.create_index(
        "ix_messages_feed",
        "messages",
        [sa.text("received_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("direction = 'inbound'"),
    )
    op.create_index(
        "ix_messages_status_received",
        "messages",
        ["status", sa.text("received_at DESC")],
        postgresql_where=sa.text("direction = 'inbound'"),
    )
    op.create_index(
        "ix_messages_source_received", "messages", ["source", sa.text("received_at DESC")]
    )
    op.create_index("ix_messages_thread", "messages", ["thread_id", "received_at"])


def downgrade() -> None:
    op.drop_index("ix_messages_thread", table_name="messages")
    op.drop_index("ix_messages_source_received", table_name="messages")
    op.drop_index("ix_messages_status_received", table_name="messages")
    op.drop_index("ix_messages_feed", table_name="messages")
    op.drop_table("messages")
    op.drop_table("threads")
    op.drop_table("sync_state")
    op.drop_table("connector_accounts")
