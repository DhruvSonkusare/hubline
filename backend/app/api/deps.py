from __future__ import annotations

import hmac
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.runtime import Runtime

_bearer = HTTPBearer(auto_error=False, description="BACKEND_API_TOKEN (server-to-server only)")


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


async def get_session(runtime: Runtime = Depends(get_runtime)) -> AsyncIterator[AsyncSession]:
    async with runtime.sessionmaker() as session:
        yield session


async def require_service_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    runtime: Runtime = Depends(get_runtime),
) -> str:
    """Constant-time check of the bearer token. Returns the actor (X-Actor header) for auditing."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "unauthorized", "Missing bearer token")
    presented = credentials.credentials.encode()
    valid = False
    for token in runtime.settings.api_tokens:
        # no early exit: compare against every configured token
        valid |= hmac.compare_digest(presented, token.encode())
    if not valid:
        raise ApiError(401, "unauthorized", "Invalid bearer token")
    return request.headers.get("x-actor", "unknown")[:200]
