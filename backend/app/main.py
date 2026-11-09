"""FastAPI app factory. Run with:  uvicorn app.main:create_app --factory"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from app import __version__
from app.api.router import api_router
from app.config import Settings
from app.connectors import discover
from app.db.engine import create_engine, create_sessionmaker
from app.errors import install_error_handlers
from app.logging import configure_logging, request_id_middleware
from app.runtime import Runtime
from app.services.connector_runtime import sync_registry
from app.workers.scheduler import Scheduler
from app.workers.supervisor import Supervisor, Wakeups

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()  # values come from env / .env
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        connectors = discover()
        engine = create_engine(settings.database_url, pool_size=settings.db_pool_size)
        sessionmaker = create_sessionmaker(engine)
        http = httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False)
        supervisor = Supervisor()
        runtime = Runtime(settings, engine, sessionmaker, http, Wakeups(), supervisor)
        app.state.runtime = runtime

        async with sessionmaker() as session, session.begin():
            await sync_registry(session, settings)

        if settings.enable_scheduler:
            scheduler = Scheduler(sessionmaker, settings, http, runtime.wakeups.scheduler)
            supervisor.start("scheduler", scheduler.run)
        else:
            supervisor.mark("scheduler", "disabled")
        # The agent worker arrives in Step 2; the flag is honoured from day one.
        supervisor.mark(
            "agent_worker", "not_implemented" if settings.enable_agent_worker else "disabled"
        )
        log.info(
            "startup_complete",
            version=__version__,
            env=settings.app_env,
            connectors=connectors,
            workers=supervisor.status,
        )
        try:
            yield
        finally:
            await supervisor.stop()
            await http.aclose()
            await engine.dispose()
            log.info("shutdown_complete")

    app = FastAPI(
        title="Hubline API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.middleware("http")(request_id_middleware)
    install_error_handlers(app)
    app.include_router(api_router)
    return app
