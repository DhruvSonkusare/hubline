"""DB helpers shared by tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import ConnectorAccount, Message, SyncState
from app.services.connector_runtime import sync_registry


async def setup_accounts(
    sm: async_sessionmaker[AsyncSession], settings: Settings, **enable: bool
) -> dict[str, ConnectorAccount]:
    """Run sync_registry, then set `enabled` per keyword (e.g. fake=True)."""
    async with sm() as s, s.begin():
        await sync_registry(s, settings)
        for key, value in enable.items():
            await s.execute(
                update(ConnectorAccount).where(ConnectorAccount.source == key).values(enabled=value)
            )
    async with sm() as s:
        rows = (await s.execute(select(ConnectorAccount))).scalars().all()
        return {a.source: a for a in rows}


async def get_state(sm: async_sessionmaker[AsyncSession], source: str) -> SyncState:
    async with sm() as s:
        return (
            await s.execute(
                select(SyncState)
                .join(ConnectorAccount, ConnectorAccount.id == SyncState.connector_account_id)
                .where(ConnectorAccount.source == source)
            )
        ).scalar_one()


async def get_account(sm: async_sessionmaker[AsyncSession], source: str) -> ConnectorAccount:
    async with sm() as s:
        return (
            await s.execute(select(ConnectorAccount).where(ConnectorAccount.source == source))
        ).scalar_one()


async def make_due(sm: async_sessionmaker[AsyncSession], source: str) -> None:
    async with sm() as s, s.begin():
        await s.execute(
            text(
                "UPDATE sync_state SET next_poll_at = now() - interval '1 second', "
                "lease_until = NULL WHERE connector_account_id = "
                "(SELECT id FROM connector_accounts WHERE source = :src)"
            ),
            {"src": source},
        )


async def all_messages(sm: async_sessionmaker[AsyncSession], **filters: Any) -> list[Message]:
    async with sm() as s:
        stmt = select(Message).order_by(Message.received_at, Message.external_id)
        for k, v in filters.items():
            stmt = stmt.where(getattr(Message, k) == v)
        return list((await s.execute(stmt)).scalars().all())
