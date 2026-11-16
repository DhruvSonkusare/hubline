"""Generic connect / disconnect lifecycle for connectors that need credentials (Step 5).

Nothing here knows about a specific app: token-paste connectors implement TokenConnectMixin,
OAuth connectors implement OAuthConnectMixin, and this module owns state, PKCE, encryption and
the status transitions. Network calls (token checks, code exchange, revoke) never run inside a
database transaction.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Mapping
from datetime import timedelta

import httpx
import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors import registry
from app.connectors.base import (
    AuthType,
    ConnectError,
    ConnectorError,
    ConnectResult,
    OAuthConnectMixin,
    RateLimitedError,
    TokenConnectMixin,
)
from app.errors import ApiError, not_found
from app.models import ConnectorAccount, OAuthState, SyncState
from app.services.connector_runtime import build_connector, missing_settings
from app.services.credentials import (
    CredentialsUnavailableError,
    delete_credentials,
    load_credentials,
    store_credentials,
)

log = structlog.get_logger(__name__)

REVOKE_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 20.0


def redirect_uri(settings: Settings, key: str) -> str:
    """Must match the URI registered with the provider byte for byte."""
    return f"{settings.public_base_url.rstrip('/')}/oauth/{key}/callback"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _require_configured(key: str, settings: Settings) -> None:
    desc = registry.get(key).describe()
    missing = missing_settings(desc, settings)
    if missing:
        raise ApiError(
            503,
            "not_configured",
            f"{desc.display_name} needs {', '.join(missing)} in the backend environment",
            {"missing_env": missing},
        )


async def _account(session: AsyncSession, key: str, *, lock: bool = False) -> ConnectorAccount:
    stmt = select(ConnectorAccount).where(ConnectorAccount.source == key)
    if lock:
        stmt = stmt.with_for_update()
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None:
        raise not_found("Connector")
    return account


async def apply_connect(
    session: AsyncSession, key: str, result: ConnectResult, settings: Settings
) -> None:
    """Store encrypted credentials and mark the connector connected (caller commits)."""
    account = await _account(session, key, lock=True)
    await store_credentials(
        session,
        account.id,
        settings,
        result.credentials,
        scopes=list(result.scopes),
        access_expires_at=result.access_expires_at,
    )
    identity_changed = account.account_identity != result.account_identity
    account.status = "connected"
    account.account_identity = result.account_identity
    account.self_ids = list(result.self_ids)
    account.connected_at = func.now()
    account.last_error = None
    account.last_error_at = None
    account.updated_at = func.now()
    values: dict[str, object] = {
        "consecutive_failures": 0,
        "last_error": None,
        "lease_until": None,
        "next_poll_at": func.now(),  # DB clock; poll straight away if enabled
        "updated_at": func.now(),
    }
    if identity_changed:
        values["cursor"] = None  # a different account: start with a fresh backfill
    await session.execute(
        update(SyncState).where(SyncState.connector_account_id == account.id).values(**values)
    )
    log.info("connector_connected", source=key, identity_changed=identity_changed)


# ----------------------------------------------------------------------------- token paste


async def connect_with_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    http: httpx.AsyncClient,
    key: str,
    fields: Mapping[str, str],
) -> None:
    cls = registry.get(key)
    if not issubclass(cls, TokenConnectMixin):
        raise ApiError(409, "not_supported", f"{key} does not connect with a pasted token")
    _require_configured(key, settings)
    expected = {f.name for f in cls.token_fields}
    unknown = set(fields) - expected
    if unknown:
        raise ApiError(422, "validation_error", f"unknown fields: {', '.join(sorted(unknown))}")
    try:
        result = await asyncio.wait_for(
            cls.connect_with_token(fields, http=http, app_settings=settings),
            timeout=CONNECT_TIMEOUT_S,
        )
    except ConnectError as exc:
        raise ApiError(422, exc.code, str(exc)) from None
    except RateLimitedError:
        raise ApiError(
            429, "rate_limited", f"{key} is rate limiting us; try again shortly"
        ) from None
    except (ConnectorError, httpx.HTTPError, TimeoutError) as exc:
        raise ApiError(
            502, "upstream_error", f"could not reach {key}: {type(exc).__name__}"
        ) from None
    async with sessionmaker() as session, session.begin():
        await apply_connect(session, key, result, settings)


# ----------------------------------------------------------------------------- OAuth


async def start_oauth(session: AsyncSession, settings: Settings, key: str) -> str:
    """Create a single-use state + PKCE verifier and return the provider's authorize URL."""
    cls = registry.get(key)
    if not issubclass(cls, OAuthConnectMixin):
        raise ApiError(409, "not_oauth", f"{key} does not use OAuth")
    _require_configured(key, settings)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    # housekeeping here instead of a periodic job (no periodic DB queries in production)
    await session.execute(delete(OAuthState).where(OAuthState.expires_at < func.now()))
    session.add(
        OAuthState(
            state=state,
            source=key,
            code_verifier=verifier,
            expires_at=func.now() + timedelta(seconds=settings.oauth_state_ttl_s),
        )
    )
    account = (
        await session.execute(select(ConnectorAccount).where(ConnectorAccount.source == key))
    ).scalar_one_or_none()
    # Reconnect (connected, or the login expired): ask the provider to preselect the same
    # account, so a 7-day re-login can't silently switch to another mailbox.
    hint = (
        account.account_identity
        if account is not None and account.status in ("connected", "needs_reauth")
        else None
    )
    await session.commit()
    return cls.authorize_url(
        state=state,
        code_challenge=challenge,
        redirect_uri=redirect_uri(settings, key),
        app_settings=settings,
        login_hint=hint,
    )


_CONSUME_SQL = text(
    """
    UPDATE oauth_states SET consumed_at = now()
    WHERE state = :state AND source = :source AND consumed_at IS NULL AND expires_at > now()
    RETURNING code_verifier
    """
)


async def finish_oauth(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    http: httpx.AsyncClient,
    key: str,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
) -> str | None:
    """Handle the provider's redirect. Returns None on success, else a short error slug."""
    cls = registry.get(key)
    if not issubclass(cls, OAuthConnectMixin):
        return "not_oauth"
    if not state:
        return "invalid_state"
    async with sessionmaker() as session, session.begin():  # consume atomically, even on ?error
        verifier = (
            await session.execute(_CONSUME_SQL, {"state": state, "source": key})
        ).scalar_one_or_none()
    if verifier is None:
        return "invalid_state"
    if error:
        return "access_denied" if error == "access_denied" else "provider_error"
    if not code:
        return "provider_error"
    if missing_settings(cls.describe(), settings):
        return "not_configured"
    try:
        result = await asyncio.wait_for(
            cls.exchange_code(
                code=code,
                code_verifier=verifier,
                redirect_uri=redirect_uri(settings, key),
                http=http,
                app_settings=settings,
            ),
            timeout=CONNECT_TIMEOUT_S,
        )
    except ConnectError as exc:
        log.warning("oauth_connect_failed", source=key, code=exc.code)
        return exc.code
    except (ConnectorError, httpx.HTTPError, TimeoutError) as exc:
        log.warning("oauth_exchange_failed", source=key, error_type=type(exc).__name__)
        return "upstream_error"
    async with sessionmaker() as session, session.begin():
        await apply_connect(session, key, result, settings)
    return None


# ----------------------------------------------------------------------------- disconnect


async def disconnect(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    http: httpx.AsyncClient,
    key: str,
) -> None:
    """Revoke (best effort), delete credentials, disable. Message history is kept."""
    desc = registry.get(key).describe()
    if desc.auth_type is AuthType.NONE:
        raise ApiError(409, "not_supported", f"{desc.display_name} has nothing to disconnect")
    async with sessionmaker() as session:
        account = await _account(session, key)
        try:
            credentials = await load_credentials(session, account.id, settings)
        except CredentialsUnavailableError:
            credentials = None
    if credentials is not None:
        connector = build_connector(account, settings, http, credentials=credentials)
        try:
            await asyncio.wait_for(connector.revoke(), timeout=REVOKE_TIMEOUT_S)
        except Exception as exc:  # best effort: the local tokens are deleted regardless
            log.warning("revoke_failed", source=key, error_type=type(exc).__name__)
    async with sessionmaker() as session, session.begin():
        account = await _account(session, key, lock=True)
        await delete_credentials(session, account.id)
        account.status = "not_configured" if missing_settings(desc, settings) else "disconnected"
        account.enabled = False
        account.account_identity = None
        account.self_ids = []  # the old account's addresses no longer mean "me"
        account.connected_at = None
        account.last_error = None  # an error about the old account isn't useful any more
        account.last_error_at = None
        account.updated_at = func.now()
        await session.execute(
            update(SyncState)
            .where(SyncState.connector_account_id == account.id)
            .values(
                cursor=None,
                lease_until=None,
                consecutive_failures=0,
                last_error=None,
                updated_at=func.now(),
            )
        )
    log.info("connector_disconnected", source=key)
