"""structlog configuration: JSON in production, console locally, secrets always redacted."""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog
from starlette.requests import Request
from starlette.responses import Response

REDACTED = "[REDACTED]"
# Keys whose values must never reach logs. Anchored so e.g. "status_code" is not caught by "code".
_SECRET_KEY = re.compile(
    r"(token|secret|authorization|password|passwd|cookie|api[_-]?key|credentials?)$|^code$",
    re.IGNORECASE,
)


# Query parameters whose values never reach the access log (OAuth callback code/state, tokens).
_SECRET_QUERY = re.compile(
    r"([?&](?:code|state|token|access_token|refresh_token|id_token|secret|password|key)=)[^&#\s]*",
    re.IGNORECASE,
)


def redact_query(path: str) -> str:
    """`/oauth/gmail/callback?code=abc&state=xyz` -> `...?code=[REDACTED]&state=[REDACTED]`."""
    return _SECRET_QUERY.sub(lambda m: m.group(1) + REDACTED, path)


class AccessLogRedactor(logging.Filter):
    """uvicorn.access logs `(client, method, full_path, http_version, status)`; redact the path."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_query(args[2]), *args[3:])
        return True


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactor) for f in access.filters):
        access.addFilter(AccessLogRedactor())


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response
