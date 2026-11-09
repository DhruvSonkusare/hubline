from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_service_token
from app.api.routes import connectors, health, internal, messages

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(internal.router)  # own auth: X-Tick-Secret

# Everything under /api requires the server-to-server bearer token.
_protected = [Depends(require_service_token)]
api_router.include_router(messages.router, dependencies=_protected)
api_router.include_router(connectors.router, dependencies=_protected)
