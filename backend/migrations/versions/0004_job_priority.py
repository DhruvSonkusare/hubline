"""Step 2 addendum 3: agent job priority (dashboard > live > backfill) and reasoning tokens.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_jobs",
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("10"), nullable=False),
    )
    op.add_column(
        "agent_jobs",
        sa.Column("reasoning_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    # Claim order is now priority DESC, run_after: rebuild the ready index to match.
    op.drop_index("ix_agent_jobs_ready", table_name="agent_jobs")
    op.create_index(
        "ix_agent_jobs_ready",
        "agent_jobs",
        [sa.text("priority DESC"), "run_after"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_jobs_ready", table_name="agent_jobs")
    op.create_index(
        "ix_agent_jobs_ready",
        "agent_jobs",
        ["run_after"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_column("agent_jobs", "reasoning_tokens")
    op.drop_column("agent_jobs", "priority")
