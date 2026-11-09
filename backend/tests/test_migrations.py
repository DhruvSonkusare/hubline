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


async def test_tables_so_far(engine: AsyncEngine) -> None:
    """Plan R1: tables arrive step by step (Step 1: 4 tables; Step 2: agent_jobs, drafts)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        )
        tables = [r[0] for r in rows]
    assert tables == [
        "agent_jobs",
        "alembic_version",
        "connector_accounts",
        "drafts",
        "messages",
        "sync_state",
        "threads",
    ]


async def test_0002_backfills_existing_messages(engine: AsyncEngine) -> None:
    """Upgrading a Step 1 database gives open inbound messages a triage job; others skipped."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    # alembic's env.py calls asyncio.run(), so run it in a worker thread (no running loop there)
    await asyncio.to_thread(command.downgrade, cfg, "0001")
    try:
        acct, thread = (
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-0000000000a1",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO connector_accounts (id, source) VALUES (:a, 'legacy')"),
                {"a": acct},
            )
            await conn.execute(
                text(
                    "INSERT INTO threads (id, source, external_thread_id, last_message_at) "
                    "VALUES (:t, 'legacy', 't', now())"
                ),
                {"t": thread},
            )
            for ext, direction, status in [
                ("open-in", "inbound", "open"),
                ("done-in", "inbound", "dismissed"),
                ("out", "outbound", "open"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO messages (connector_account_id, source, external_id, "
                        "thread_id, external_thread_id, direction, sender_id, body, "
                        "received_at, status) "
                        "VALUES (:a, 'legacy', :ext, :t, 't', :dir, 'x', 'x', now(), :status)"
                    ),
                    {"a": acct, "t": thread, "ext": ext, "dir": direction, "status": status},
                )
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")

    async with engine.connect() as conn:
        rows = dict(
            (await conn.execute(text("SELECT external_id, agent_status FROM messages"))).all()
        )
        job_msgs = (
            (
                await conn.execute(
                    text(
                        "SELECT m.external_id FROM agent_jobs j "
                        "JOIN messages m ON m.id = j.message_id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == {"open-in": "pending", "done-in": "skipped", "out": "skipped"}
    assert job_msgs == ["open-in"]
