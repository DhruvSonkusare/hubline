"""Step 2 addendum ($0 requirement): per-job LLM token accounting on agent_jobs, used by the
rolling 24h token budget that keeps the agent inside free-tier daily limits.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("input_tokens", "output_tokens", "llm_calls"):
        op.add_column(
            "agent_jobs",
            sa.Column(column, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    for column in ("llm_calls", "output_tokens", "input_tokens"):
        op.drop_column("agent_jobs", column)
