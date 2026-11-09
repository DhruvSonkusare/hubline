"""POST /internal/tick: one scheduler pass + agent drain, for an external free cron.

Protected by the X-Tick-Secret header (TICK_SECRET). Disabled (404) when TICK_SECRET is unset.
?wait=false (default) returns 202 immediately and runs in the background, which suits cron
services with short request timeouts (cron-job.org: 30 s); ?wait=true returns the result.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse

from app.api.deps import get_runtime
from app.errors import ApiError, not_found
from app.runtime import Runtime
from app.schemas.common import ErrorBody

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post(
    "/tick",
    responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def tick(
    wait: bool = Query(default=False, description="Run inline and return the result"),
    x_tick_secret: str | None = Header(default=None),
    runtime: Runtime = Depends(get_runtime),
) -> JSONResponse:
    secret = runtime.settings.tick_secret
    if secret is None or runtime.runner is None:
        raise not_found("Route")
    if not x_tick_secret or not hmac.compare_digest(
        x_tick_secret.encode(), secret.get_secret_value().encode()
    ):
        raise ApiError(401, "unauthorized", "Invalid tick secret")
    if wait:
        result = await runtime.runner.tick()
        return JSONResponse(result.as_dict(), status_code=200)
    runtime.runner.spawn_tick()
    return JSONResponse({"status": "started"}, status_code=202)
