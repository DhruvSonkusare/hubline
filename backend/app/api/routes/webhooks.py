"""Public webhook endpoint for every connector with the `webhook` capability: POST /webhooks/{key}.

The connector verifies the request itself (signature first). The route then ingests the
messages in one short transaction and answers straight away (Slack wants a 2xx within 3 s);
the agent runs afterwards in the background.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, update

from app.api.deps import get_runtime
from app.connectors import registry
from app.connectors.base import Capability, WebhookVerificationError
from app.connectors.models import WebhookRequest
from app.errors import ApiError, not_found
from app.models import ConnectorAccount, SyncState
from app.runtime import Runtime
from app.services.connector_runtime import allowed_by_env, build_connector, missing_settings
from app.services.credentials import CredentialsUnavailableError, load_credentials, saver
from app.services.ingest import ingest_messages

router = APIRouter(tags=["webhooks"])
log = structlog.get_logger(__name__)

MAX_BODY_BYTES = 1_000_000
PARSE_TIMEOUT_S = 2.5  # leaves room inside Slack's 3-second deadline


@router.post("/webhooks/{key}", include_in_schema=False)
async def receive_webhook(
    key: str, request: Request, runtime: Runtime = Depends(get_runtime)
) -> Response:
    try:
        cls = registry.get(key)
    except registry.UnknownConnectorError:
        raise not_found("Webhook") from None
    desc = cls.describe()
    if Capability.WEBHOOK not in desc.capabilities:
        raise not_found("Webhook")
    if missing_settings(desc, runtime.settings):
        raise ApiError(503, "not_configured", f"{desc.display_name} is not configured")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "webhook body too large")

    sm = runtime.sessionmaker
    async with sm() as session:
        account = (
            await session.execute(select(ConnectorAccount).where(ConnectorAccount.source == key))
        ).scalar_one_or_none()
        if account is None:
            raise not_found("Webhook")
        credentials = None
        if account.status == "connected":
            try:
                credentials = await load_credentials(session, account.id, runtime.settings)
            except CredentialsUnavailableError:
                credentials = None
    active = (
        account.enabled
        and account.status == "connected"
        and credentials is not None
        and allowed_by_env(key, runtime.settings)
    )
    connector = build_connector(
        account,
        runtime.settings,
        runtime.http,
        credentials=credentials,
        save_credentials=saver(sm, account.id, runtime.settings),
    )
    req = WebhookRequest(
        method=request.method,
        path=request.url.path,
        headers={k.lower(): v for k, v in request.headers.items()},
        query=dict(request.query_params),
        body=body,
    )
    try:
        result = await asyncio.wait_for(connector.parse_webhook(req), timeout=PARSE_TIMEOUT_S)
    except WebhookVerificationError as exc:
        # never log the body or the headers: they may carry content or secrets
        log.warning("webhook_rejected", source=key, reason=str(exc))
        raise ApiError(401, "unauthorized", "webhook verification failed") from None
    except TimeoutError:
        log.warning("webhook_parse_timeout", source=key)
        raise ApiError(503, "timeout", "try again") from None  # the app retries

    if result.messages and active:
        async with sm() as session, session.begin():
            fresh = await session.get(ConnectorAccount, account.id)
            assert fresh is not None
            ingested = await ingest_messages(session, fresh, result.messages)
        log.info(
            "webhook_ingested",
            source=key,
            received=len(result.messages),
            inserted=ingested.inserted,
            retry=request.headers.get("x-slack-retry-num"),
        )
        if ingested.jobs_enqueued and runtime.runner is not None:
            runtime.runner.kick_agent()
    elif result.messages:
        log.info("webhook_dropped_inactive", source=key, received=len(result.messages))

    if result.request_sync and active:
        async with sm() as session, session.begin():
            await session.execute(
                update(SyncState)
                .where(SyncState.connector_account_id == account.id)
                .values(next_poll_at=func.now(), updated_at=func.now())
            )
        if runtime.runner is not None:
            runtime.runner.kick_scheduler()

    resp = result.response
    if resp is None:
        return Response(status_code=200)
    return Response(
        content=resp.body,
        status_code=resp.status,
        media_type=resp.content_type,
        headers=dict(resp.headers),
    )
