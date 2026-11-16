from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.connectors import registry
from app.connectors.base import Capability, TokenConnectMixin
from app.errors import ApiError, not_found
from app.models import ConnectorAccount, SyncState
from app.runtime import Runtime
from app.schemas.common import ErrorBody
from app.schemas.connectors import (
    ConnectorOut,
    ConnectorPatch,
    CredentialsIn,
    OAuthStartOut,
    SampleIn,
    SampleOut,
    ScenarioList,
    SyncScheduled,
    TokenFieldOut,
)
from app.services import connect
from app.services.connector_runtime import allowed_by_env, build_connector, missing_settings
from app.services.ingest import ingest_messages
from app.services.jobs import PRIORITY_USER
from app.workers.runner import WorkRunner

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}
_CONNECT_ERRORS: dict[int | str, dict[str, object]] = {
    **_ERRORS,
    429: {"model": ErrorBody},
    502: {"model": ErrorBody},
    503: {"model": ErrorBody},
}


def _runner(runtime: Runtime) -> WorkRunner:
    assert runtime.runner is not None  # created in the lifespan
    return runtime.runner


def _to_out(account: ConnectorAccount, state: SyncState | None, runtime: Runtime) -> ConnectorOut:
    cls = registry.get(account.source)
    desc = cls.describe()
    return ConnectorOut(
        key=desc.key,
        display_name=desc.display_name,
        auth_type=desc.auth_type.value,
        capabilities=sorted(c.value for c in desc.capabilities),
        can_send=desc.can_send,
        is_demo=desc.is_demo,
        enabled=account.enabled,
        status=account.status,
        account_identity=account.account_identity,
        missing_env=missing_settings(desc, runtime.settings),
        disabled_by_env=not allowed_by_env(desc.key, runtime.settings),
        poll_interval_s=desc.poll_interval_s if desc.can_poll else None,
        last_poll_started_at=state.last_poll_started_at if state else None,
        last_success_at=state.last_success_at if state else None,
        next_poll_at=state.next_poll_at if state else None,
        consecutive_failures=state.consecutive_failures if state else 0,
        last_error=account.last_error,
        settings=account.settings or {},
        settings_schema=desc.settings_model.model_json_schema() if desc.settings_model else None,
        connect_method=cls.connect_method(),
        token_fields=[
            TokenFieldOut(name=f.name, label=f.label, placeholder=f.placeholder, help=f.help)
            for f in (cls.token_fields if issubclass(cls, TokenConnectMixin) else ())
        ],
    )


async def _load(
    session: AsyncSession, key: str, *, for_update: bool = False
) -> tuple[ConnectorAccount, SyncState | None]:
    try:
        registry.get(key)
    except registry.UnknownConnectorError:
        raise not_found("Connector") from None
    stmt = (
        select(ConnectorAccount, SyncState)
        .outerjoin(SyncState, SyncState.connector_account_id == ConnectorAccount.id)
        .where(ConnectorAccount.source == key)
    )
    if for_update:
        stmt = stmt.with_for_update(of=ConnectorAccount)
    row = (await session.execute(stmt)).first()
    if row is None:
        raise not_found("Connector")
    return row[0], row[1]


@router.get("", response_model=list[ConnectorOut], responses=_ERRORS)
async def list_connectors(
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(get_runtime)
) -> list[ConnectorOut]:
    keys = list(registry.all_connectors())
    rows = (
        await session.execute(
            select(ConnectorAccount, SyncState)
            .outerjoin(SyncState, SyncState.connector_account_id == ConnectorAccount.id)
            .where(ConnectorAccount.source.in_(keys))
            .order_by(ConnectorAccount.source)
        )
    ).all()
    return [_to_out(account, state, runtime) for account, state in rows]


@router.patch("/{key}", response_model=ConnectorOut, responses=_ERRORS)
async def patch_connector(
    key: str,
    body: ConnectorPatch,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> ConnectorOut:
    account, state = await _load(session, key, for_update=True)
    desc = registry.get(key).describe()
    now = datetime.now(UTC)

    if body.settings is not None:
        if desc.settings_model is None:
            raise ApiError(422, "validation_error", f"{key} has no configurable settings")
        merged = {**(account.settings or {}), **body.settings}
        try:
            parsed = desc.settings_model.model_validate(merged)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_input=False, include_context=False)
            raise ApiError(
                422, "validation_error", "Invalid connector settings", {"errors": errors}
            ) from None
        account.settings = parsed.model_dump(mode="json")

    wake = False
    if body.enabled is True and not account.enabled:
        if not allowed_by_env(key, runtime.settings):
            raise ApiError(409, "disabled_by_env", f"{key} is not in CONNECTORS_ENABLED")
        if account.status != "connected":
            raise ApiError(
                409,
                "invalid_state",
                f"{key} cannot be enabled while its status is {account.status!r}",
            )
        account.enabled = True
        if state is not None:
            # DB clock, not the app host's: the scheduler compares against the DB's now(), and
            # a host clock running ahead would make the connector look "not yet due".
            state.next_poll_at = func.now()
            state.updated_at = func.now()
        wake = True
    elif body.enabled is False:
        account.enabled = False

    account.updated_at = now
    await session.commit()
    if wake and state is not None:
        await session.refresh(state)
    if wake:
        _runner(runtime).kick_scheduler()
    return _to_out(account, state, runtime)


@router.post("/{key}/sync", response_model=SyncScheduled, status_code=202, responses=_ERRORS)
async def sync_connector(
    key: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> SyncScheduled:
    """Poll this connector as soon as possible."""
    account, state = await _load(session, key, for_update=True)
    if not registry.get(key).describe().can_poll:
        raise ApiError(409, "not_pollable", f"{key} does not support polling")
    if not account.enabled or account.status != "connected" or state is None:
        raise ApiError(409, "invalid_state", f"{key} must be connected and enabled to sync")
    state.next_poll_at = func.now()  # DB clock (see patch_connector)
    state.updated_at = func.now()
    await session.commit()
    _runner(runtime).kick_scheduler()
    return SyncScheduled()


@router.post("/{key}/sample", response_model=SampleOut, status_code=201, responses=_ERRORS)
async def create_sample_message(
    key: str,
    body: SampleIn | None = None,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> SampleOut:
    """Generate one message right now (e.g. to trigger the agent live while presenting).

    Works even when the connector's scheduled polling is disabled; the connector only has to be
    connected and declare the `sample` capability.
    """
    account, _ = await _load(session, key)
    if Capability.SAMPLE not in registry.get(key).describe().capabilities:
        raise ApiError(409, "not_supported", f"{key} cannot generate sample messages")
    if account.status != "connected":
        raise ApiError(409, "invalid_state", f"{key} must be connected (status {account.status!r})")
    connector = build_connector(account, runtime.settings, runtime.http)
    try:
        message = await connector.sample(body.scenario if body else None)
    except ValueError as exc:
        raise ApiError(422, "invalid_scenario", str(exc)) from None

    # a dashboard action: goes before live and backfill work
    result = await ingest_messages(session, account, [message], job_priority=PRIORITY_USER)
    await session.commit()
    if not result.new_inbound_ids:  # pragma: no cover - ids are random
        raise ApiError(409, "duplicate", "sample collided with an existing message")
    if result.jobs_enqueued:
        _runner(runtime).kick_agent()
    return SampleOut(
        message_id=result.new_inbound_ids[0],
        external_id=message.external_id,
        agent_job_enqueued=result.jobs_enqueued > 0,
    )


@router.get("/{key}/scenarios", response_model=ScenarioList, responses=_ERRORS)
async def list_scenarios(key: str) -> ScenarioList:
    """Scenario names for the New demo message menu (connectors with the sample capability)."""
    try:
        cls = registry.get(key)
    except registry.UnknownConnectorError:
        raise not_found("Connector") from None
    if Capability.SAMPLE not in cls.describe().capabilities:
        raise ApiError(409, "not_supported", f"{key} cannot generate sample messages")
    return ScenarioList(scenarios=cls.sample_scenarios())


# ----------------------------------------------------------------------------- connect (Step 5)


@router.post("/{key}/credentials", response_model=ConnectorOut, responses=_CONNECT_ERRORS)
async def connect_with_token(
    key: str,
    body: CredentialsIn,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> ConnectorOut:
    """Connect by pasting a token. It is checked with the app, stored encrypted, never returned."""
    await _load(session, key)  # 404 for unknown connectors
    await session.rollback()  # no transaction open across the token check
    await connect.connect_with_token(
        runtime.sessionmaker, runtime.settings, runtime.http, key, body.fields
    )
    account, state = await _load(session, key)
    if account.enabled:
        _runner(runtime).kick_scheduler()
    return _to_out(account, state, runtime)


@router.post("/{key}/oauth/start", response_model=OAuthStartOut, responses=_CONNECT_ERRORS)
async def oauth_start(
    key: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> OAuthStartOut:
    """The URL to send the browser to. The provider redirects back to /oauth/{key}/callback."""
    await _load(session, key)
    return OAuthStartOut(authorize_url=await connect.start_oauth(session, runtime.settings, key))


@router.post("/{key}/disconnect", response_model=ConnectorOut, responses=_ERRORS)
async def disconnect_connector(
    key: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> ConnectorOut:
    """Revoke (best effort) and delete the stored tokens; turns the connector off. History stays."""
    await _load(session, key)
    await session.rollback()
    await connect.disconnect(runtime.sessionmaker, runtime.settings, runtime.http, key)
    account, state = await _load(session, key)
    return _to_out(account, state, runtime)
