from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.workers.supervisor import Supervisor, Wakeups


@dataclass
class Runtime:
    """Process-wide resources created in the FastAPI lifespan (stored on app.state.runtime)."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    wakeups: Wakeups
    supervisor: Supervisor
