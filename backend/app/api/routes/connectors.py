from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.connectors import registry
from app.errors import ApiError, not_found
from app.models import ConnectorAccount, SyncState
from app.runtime import Runtime
from app.schemas.common import ErrorBody
from app.schemas.connectors import ConnectorOut, ConnectorPatch, SyncScheduled
from app.services.connector_runtime import allowed_by_env, missing_settings

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


def _to_out(account: ConnectorAccount, state: SyncState | None, runtime: Runtime) -> ConnectorOut:
    desc = registry.get(account.source).describe()
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
            state.next_poll_at = now
            state.updated_at = now
        wake = True
    elif body.enabled is False:
        account.enabled = False

    account.updated_at = now
    await session.commit()
    if wake:
        runtime.wakeups.scheduler.set()
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
    state.next_poll_at = datetime.now(UTC)
    state.updated_at = state.next_poll_at
    await session.commit()
    runtime.wakeups.scheduler.set()
    return SyncScheduled()
