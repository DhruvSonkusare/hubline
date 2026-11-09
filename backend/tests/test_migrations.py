"""The migrated schema must match the SQLAlchemy models exactly (same check as `alembic check`)."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401
from app.db.base import Base

pytestmark = pytest.mark.db


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "compare_server_default": True}
    )
    return list(compare_metadata(ctx, Base.metadata))


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []


async def test_step1_tables_only(engine: AsyncEngine) -> None:
    """Plan R1: Step 1 creates exactly these tables."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        )
        tables = [r[0] for r in rows]
    assert tables == ["alembic_version", "connector_accounts", "messages", "sync_state", "threads"]
