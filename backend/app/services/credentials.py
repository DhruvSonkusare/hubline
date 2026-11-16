"""Encrypted connector credentials (plan R2: one Fernet key, CREDENTIALS_ENCRYPTION_KEY).

Tokens only ever exist in plaintext in memory: they are encrypted before they reach the
database, never returned by the API, and never logged.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import ConnectorCredential


class CredentialsUnavailableError(Exception):
    """No usable key, or stored credentials can't be decrypted (key changed) -> reconnect."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.credentials_encryption_key
    if key is None or not key.get_secret_value():
        raise CredentialsUnavailableError("CREDENTIALS_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.get_secret_value().encode())
    except ValueError:
        raise CredentialsUnavailableError(
            "CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)"
        ) from None


def encrypt(settings: Settings, data: dict[str, Any]) -> bytes:
    return _fernet(settings).encrypt(json.dumps(data, separators=(",", ":")).encode())


def decrypt(settings: Settings, blob: bytes) -> dict[str, Any]:
    try:
        plain = _fernet(settings).decrypt(bytes(blob))
    except InvalidToken:
        raise CredentialsUnavailableError(
            "stored credentials can't be decrypted with the current CREDENTIALS_ENCRYPTION_KEY; "
            "reconnect this app"
        ) from None
    data: dict[str, Any] = json.loads(plain)
    return data


async def load_credentials(
    session: AsyncSession, account_id: uuid.UUID, settings: Settings
) -> dict[str, Any] | None:
    blob = (
        await session.execute(
            select(ConnectorCredential.ciphertext).where(
                ConnectorCredential.connector_account_id == account_id
            )
        )
    ).scalar_one_or_none()
    return None if blob is None else decrypt(settings, blob)


async def store_credentials(
    session: AsyncSession,
    account_id: uuid.UUID,
    settings: Settings,
    data: dict[str, Any],
    *,
    scopes: list[str] | None = None,
    access_expires_at: datetime | None = None,
) -> None:
    values: dict[str, Any] = {
        "ciphertext": encrypt(settings, data),
        "access_expires_at": access_expires_at,
        "updated_at": func.now(),
    }
    if scopes is not None:
        values["scopes"] = scopes
    await session.execute(
        insert(ConnectorCredential)
        .values(connector_account_id=account_id, **values)
        .on_conflict_do_update(index_elements=["connector_account_id"], set_=values)
    )


async def delete_credentials(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(
        delete(ConnectorCredential).where(ConnectorCredential.connector_account_id == account_id)
    )


def saver(
    sessionmaker: async_sessionmaker[AsyncSession], account_id: uuid.UUID, settings: Settings
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """ConnectorContext.save_credentials: persist refreshed tokens in their own short txn."""

    async def _save(data: dict[str, Any]) -> None:
        expires = data.get("expires_at")
        async with sessionmaker() as session, session.begin():
            await store_credentials(
                session,
                account_id,
                settings,
                data,
                access_expires_at=datetime.fromisoformat(expires) if expires else None,
            )

    return _save
