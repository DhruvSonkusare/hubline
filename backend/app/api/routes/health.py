from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.deps import get_runtime
from app.runtime import Runtime

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(runtime: Runtime = Depends(get_runtime)) -> dict[str, object]:
    """Liveness. Deliberately does NOT touch the database, so frequent probes let Neon sleep."""
    return {"status": "ok", "version": __version__, "workers": dict(runtime.supervisor.status)}


@router.get("/readyz")
async def readyz(runtime: Runtime = Depends(get_runtime)) -> JSONResponse:
    """Readiness: also checks the database (use for deploy health checks, not frequent pings)."""
    try:
        async with asyncio.timeout(5):
            async with runtime.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        db = "ok"
    except Exception:
        db = "unavailable"
    ok = db == "ok"
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "db": db, "workers": runtime.supervisor.status},
        status_code=200 if ok else 503,
    )
