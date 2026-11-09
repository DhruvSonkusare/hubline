"""Step 2 schema: agent columns on messages, agent_jobs, drafts.

Existing open inbound messages get a triage job so nothing is left "pending" without work;
outbound / already-handled messages are marked skipped.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    # --- messages: agent output columns
    op.add_column("messages", sa.Column("priority", sa.SmallInteger(), nullable=True))
    op.add_column("messages", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("needs_reply", sa.Boolean(), nullable=True))
    op.add_column("messages", sa.Column("category", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("priority_reason", sa.Text(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("agent_status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    )
    op.add_column("messages", sa.Column("agent_error", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("agent_model", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("prompt_version", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_messages_priority"), "messages", "priority BETWEEN 1 AND 5")
    op.create_check_constraint(
        op.f("ck_messages_agent_status"),
        "messages",
        "agent_status IN ('pending', 'running', 'done', 'failed', 'skipped')",
    )
    op.create_index(
        "ix_messages_status_priority",
        "messages",
        ["status", sa.text("COALESCE(priority, 0) DESC"), sa.text("received_at DESC")],
        postgresql_where=sa.text("direction = 'inbound'"),
    )

    # --- drafts
    op.create_table(
        "drafts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column(
            "open_questions",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("author IN ('agent', 'human')", name=op.f("ck_drafts_author")),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_drafts_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drafts")),
        sa.UniqueConstraint("message_id", "version", name=op.f("uq_drafts_message_id_version")),
    )

    # --- agent_jobs
    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('triage', 'redraft')", name=op.f("ck_agent_jobs_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'dead')",
            name=op.f("ck_agent_jobs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_agent_jobs_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_jobs")),
    )
    op.create_index(
        "ix_agent_jobs_ready",
        "agent_jobs",
        ["run_after"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_agent_jobs_message", "agent_jobs", ["message_id"])
    op.create_index(
        "uq_agent_jobs_one_active",
        "agent_jobs",
        ["message_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    # --- data: give pre-existing messages a consistent agent state
    op.execute(
        "UPDATE messages SET agent_status = 'skipped' "
        "WHERE direction = 'outbound' OR status <> 'open'"
    )
    op.execute(
        "INSERT INTO agent_jobs (message_id, kind) "
        "SELECT id, 'triage' FROM messages WHERE direction = 'inbound' AND status = 'open'"
    )


def downgrade() -> None:
    op.drop_index("uq_agent_jobs_one_active", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_message", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_ready", table_name="agent_jobs")
    op.drop_table("agent_jobs")
    op.drop_table("drafts")
    op.drop_index("ix_messages_status_priority", table_name="messages")
    op.drop_constraint(op.f("ck_messages_agent_status"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_priority"), "messages", type_="check")
    for column in (
        "prompt_version",
        "agent_model",
        "triaged_at",
        "agent_error",
        "agent_status",
        "priority_reason",
        "category",
        "needs_reply",
        "summary",
        "priority",
    ):
        op.drop_column("messages", column)
