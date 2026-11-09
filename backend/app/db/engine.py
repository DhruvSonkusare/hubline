"""Async engine + session factory, and DATABASE_URL normalisation for asyncpg/Neon."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# libpq-style params that asyncpg rejects; Neon URLs include some of them by default.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding", "options", "target_session_attrs"}
_SSL_MODES_REQUIRING_TLS = {"require", "verify-ca", "verify-full"}


def normalize_database_url(url: str) -> tuple[str, dict[str, Any]]:
    """Return (sqlalchemy_url, connect_args) suitable for create_async_engine with asyncpg.

    - postgres:// and postgresql:// become postgresql+asyncpg://
    - sslmode=require (Neon) becomes connect_args={"ssl": "require"} because asyncpg
      does not understand sslmode in the URL
    - other libpq-only params are dropped
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    elif scheme != "postgresql+asyncpg":
        raise ValueError(f"Unsupported database URL scheme: {parts.scheme!r}")

    connect_args: dict[str, Any] = {}
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            if value in _SSL_MODES_REQUIRING_TLS:
                connect_args["ssl"] = "require"
        elif key == "ssl":
            connect_args["ssl"] = value
        elif key not in _LIBPQ_ONLY_PARAMS:
            kept.append((key, value))

    clean = urlunsplit((scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    return clean, connect_args


def create_engine(database_url: str, *, pool_size: int = 5) -> AsyncEngine:
    url, connect_args = normalize_database_url(database_url)
    return create_async_engine(
        url,
        connect_args=connect_args,
        pool_size=pool_size,
        max_overflow=2,
        pool_pre_ping=True,  # Neon suspends idle compute; reconnect transparently
        pool_recycle=300,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
