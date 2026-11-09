"""Step 3: outbound_replies (human-approved replies, at-most-once sends).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_replies",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("connector_account_id", sa.Uuid(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('sending', 'sent', 'failed', 'unknown')",
            name=op.f("ck_outbound_replies_status"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_outbound_replies_message_id_messages")
        ),
        sa.ForeignKeyConstraint(
            ["connector_account_id"],
            ["connector_accounts.id"],
            name=op.f("fk_outbound_replies_connector_account_id_connector_accounts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbound_replies")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_outbound_replies_idempotency_key")),
    )
    op.create_index("ix_outbound_replies_message", "outbound_replies", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_outbound_replies_message", table_name="outbound_replies")
    op.drop_table("outbound_replies")
