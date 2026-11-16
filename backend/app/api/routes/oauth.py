"""Public OAuth redirect target. Protected by single-use state + PKCE, not by the bearer token
(the browser arrives here straight from the provider)."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.api.deps import get_runtime
from app.connectors import registry
from app.runtime import Runtime
from app.services import connect

router = APIRouter(tags=["oauth"])

_SLUG = re.compile(r"[^a-z0-9_]+")


@router.get("/oauth/{key}/callback", include_in_schema=False)
async def oauth_callback(
    key: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    runtime: Runtime = Depends(get_runtime),
) -> RedirectResponse:
    """Finish the connect flow, then send the browser back to the dashboard's Connectors page."""
    settings = runtime.settings
    if key in registry.all_connectors():
        failure = await connect.finish_oauth(
            runtime.sessionmaker, settings, runtime.http, key, code=code, state=state, error=error
        )
    else:
        failure = "unknown_connector"
    if failure is None:
        query = {"connected": key}
    else:
        query = {
            "error": _SLUG.sub("_", failure.lower())[:40],
            "connector": _SLUG.sub("", key)[:40],
        }
    url = f"{settings.frontend_url.rstrip('/')}/connectors?{urlencode(query)}"
    return RedirectResponse(url, status_code=302)
