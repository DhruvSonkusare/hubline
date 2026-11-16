"""Glue between the connector registry and the database: registry sync, activation rules,
and building a Connector instance with its injected context.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors import registry
from app.connectors.base import AuthType, Connector, ConnectorContext, ConnectorDescriptor
from app.models import ConnectorAccount, SyncState
from app.services.credentials import CredentialsUnavailableError, load_credentials, saver

log = structlog.get_logger(__name__)


def _is_set(value: object) -> bool:
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value().strip())
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def missing_settings(desc: ConnectorDescriptor, settings: Settings) -> list[str]:
    """Env var names (upper-case) required by the connector but empty/unset."""
    return [
        name.upper()
        for name in desc.required_settings
        if not _is_set(getattr(settings, name, None))
    ]


def allowed_by_env(key: str, settings: Settings) -> bool:
    allow = settings.connector_allowlist
    return allow is None or key in allow


def pollable_sources(settings: Settings) -> list[str]:
    """Registered, env-allowed connectors that poll. DB-side filters add enabled + connected."""
    return [
        key
        for key, cls in registry.all_connectors().items()
        if cls.describe().can_poll and allowed_by_env(key, settings)
    ]


async def sync_registry(session: AsyncSession, settings: Settings) -> None:
    """Ensure a connector_accounts + sync_state row exists for each registered connector and that
    its status reflects configuration. Never flips `enabled` on existing rows (plan R4)."""
    for key, cls in registry.all_connectors().items():
        desc = cls.describe()
        initial_status = _initial_status(desc, settings)
        stmt = (
            insert(ConnectorAccount)
            .values(
                source=key,
                status=initial_status,
                is_demo=desc.is_demo,
                enabled=bool(desc.is_demo and settings.demo_mode),
            )
            .on_conflict_do_nothing(index_elements=["source"])
        )
        await session.execute(stmt)
        account = (
            await session.execute(select(ConnectorAccount).where(ConnectorAccount.source == key))
        ).scalar_one()

        new_status = _reconcile_status(account.status, desc, settings)
        if new_status != account.status:
            log.info("connector_status_changed", source=key, old=account.status, new=new_status)
            account.status = new_status
            account.updated_at = datetime.now(UTC)
        account.is_demo = desc.is_demo

        await session.execute(
            insert(SyncState)
            .values(connector_account_id=account.id)
            .on_conflict_do_nothing(index_elements=["connector_account_id"])
        )


def _initial_status(desc: ConnectorDescriptor, settings: Settings) -> str:
    if missing_settings(desc, settings):
        return "not_configured"
    return "connected" if desc.auth_type is AuthType.NONE else "disconnected"


def _reconcile_status(current: str, desc: ConnectorDescriptor, settings: Settings) -> str:
    if missing_settings(desc, settings):
        return "not_configured"
    if desc.auth_type is AuthType.NONE:
        return "connected"
    if current == "not_configured":
        return "disconnected"
    return current


def parse_connector_settings(
    desc: ConnectorDescriptor, raw: dict[str, Any] | None
) -> BaseModel | None:
    if desc.settings_model is None:
        return None
    return desc.settings_model.model_validate(raw or {})


def build_connector(
    account: ConnectorAccount,
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    credentials: dict[str, Any] | None = None,
    save_credentials: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> Connector:
    cls = registry.get(account.source)
    desc = cls.describe()
    ctx = ConnectorContext(
        account_id=account.id,
        source=account.source,
        settings=parse_connector_settings(desc, account.settings),
        self_ids=frozenset(account.self_ids or ()),
        http=http,
        app_settings=settings,
        log=structlog.get_logger(f"connector.{account.source}"),
        credentials=credentials,
    )
    if save_credentials is not None:
        ctx.save_credentials = save_credentials
    return cls(ctx)


async def open_connector(
    sessionmaker: async_sessionmaker[AsyncSession],
    account: ConnectorAccount,
    settings: Settings,
    http: httpx.AsyncClient,
) -> Connector:
    """build_connector() with decrypted credentials (loaded in a short session) and a
    save_credentials callback that persists refreshed tokens in its own transaction.

    Raises CredentialsUnavailableError when an app that needs credentials has none usable.
    """
    if registry.get(account.source).describe().auth_type is AuthType.NONE:
        return build_connector(account, settings, http)
    async with sessionmaker() as session:
        credentials = await load_credentials(session, account.id, settings)
    if credentials is None:
        raise CredentialsUnavailableError(f"{account.source} has no stored credentials")
    return build_connector(
        account,
        settings,
        http,
        credentials=credentials,
        save_credentials=saver(sessionmaker, account.id, settings),
    )
