"""The connector contract. Every app integration is one module in app/connectors/ that defines a
@register-decorated subclass of Connector. The core only ever talks to this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from pydantic import BaseModel

from app.connectors.models import (
    FetchResult,
    NormalizedMessage,
    ReplyContext,
    SendResult,
    WebhookRequest,
    WebhookResult,
)

if TYPE_CHECKING:
    from uuid import UUID

    from structlog.typing import FilteringBoundLogger

    from app.config import Settings


class Capability(StrEnum):
    POLL = "poll"
    WEBHOOK = "webhook"
    SEND = "send"
    SAMPLE = "sample"  # can generate one message on demand (demo/presenting)


class AuthType(StrEnum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    NONE = "none"


@dataclass(frozen=True)
class ConnectorDescriptor:
    key: str  # equals messages.source; lowercase slug
    display_name: str
    auth_type: AuthType
    capabilities: frozenset[Capability]
    poll_interval_s: int | None = 60
    # Settings fields that must be non-empty (env var = NAME.upper()), else "not_configured".
    required_settings: tuple[str, ...] = ()
    max_reply_chars: int | None = None
    is_demo: bool = False
    settings_model: type[BaseModel] | None = None  # schema for connector_accounts.settings

    @property
    def can_send(self) -> bool:
        return Capability.SEND in self.capabilities

    @property
    def can_poll(self) -> bool:
        return Capability.POLL in self.capabilities


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _no_save(_: dict[str, Any]) -> None:
    return None


@dataclass
class ConnectorContext:
    """Everything a connector may use. Injected, so connectors are trivially testable."""

    account_id: UUID
    source: str
    settings: BaseModel | None
    self_ids: frozenset[str]
    http: httpx.AsyncClient
    app_settings: Settings
    log: FilteringBoundLogger
    credentials: dict[str, Any] | None = None
    save_credentials: Callable[[dict[str, Any]], Awaitable[None]] = _no_save
    clock: Callable[[], datetime] = field(default=_utcnow)


class Connector(ABC):
    descriptor: ClassVar[ConnectorDescriptor]

    def __init__(self, ctx: ConnectorContext) -> None:
        self.ctx = ctx

    @classmethod
    def describe(cls) -> ConnectorDescriptor:
        return cls.descriptor

    @abstractmethod
    async def fetch_new(self, since: str | None) -> FetchResult:
        """Return messages newer than the opaque cursor `since`.

        since=None means first run: backfill at most app_settings.backfill_hours / backfill_max.
        """

    async def send(self, thread_id: str, text: str, *, reply_to: ReplyContext) -> SendResult:
        raise ReadOnlyConnectorError(f"{self.describe().key} is read-only")

    @classmethod
    def reply_blocked_reason(cls, *, sender_id: str, reply_meta: Mapping[str, Any]) -> str | None:
        """Why this one message must never be answered (e.g. a no-reply sender), or None.

        Checked without network access: by the feed (the UI hides Send) and by approve (409),
        before anything is sent. Default: every message of a sending connector is answerable.
        """
        return None

    async def parse_webhook(self, request: WebhookRequest) -> WebhookResult:
        """Verify the request signature FIRST (raise WebhookVerificationError), then parse."""
        raise NotImplementedError(f"{self.describe().key} does not accept webhooks")

    @classmethod
    def sample_scenarios(cls) -> list[str]:
        """Scenario names accepted by sample() (Capability.SAMPLE); [] = random only."""
        return []

    async def sample(self, scenario: str | None = None) -> NormalizedMessage:
        """Generate one fresh message now (Capability.SAMPLE). Raise ValueError for a bad
        scenario name."""
        raise NotImplementedError(f"{self.describe().key} cannot generate sample messages")

    async def maintain(self) -> None:  # noqa: B027 - optional hook, intentionally empty
        """Optional periodic hook (e.g. renew a webhook subscription)."""

    async def revoke(self) -> None:  # noqa: B027 - optional hook, intentionally empty
        """Optional: revoke ctx.credentials at the provider (best effort, on disconnect)."""

    @classmethod
    def connect_method(cls) -> str:
        """How the dashboard connects this app: "oauth", "token" or "none"."""
        if issubclass(cls, OAuthConnectMixin):
            return "oauth"
        if issubclass(cls, TokenConnectMixin):
            return "token"
        return "none"


# ---------------------------------------------------------------- connecting (Step 5)


@dataclass(frozen=True)
class ConnectResult:
    """What a successful connect yields; the core encrypts `credentials` and stores the rest."""

    credentials: dict[str, Any]
    account_identity: str
    self_ids: list[str]
    scopes: list[str] = field(default_factory=list)
    access_expires_at: datetime | None = None


@dataclass(frozen=True)
class TokenField:
    """One input on the dashboard's "paste token" form. Values are write-only."""

    name: str
    label: str
    placeholder: str = ""
    help: str = ""


class TokenConnectMixin(ABC):
    """Connect by pasting a token (e.g. a Slack user token from the app's install page)."""

    token_fields: ClassVar[tuple[TokenField, ...]]

    @classmethod
    @abstractmethod
    async def connect_with_token(
        cls, fields: Mapping[str, str], *, http: httpx.AsyncClient, app_settings: Settings
    ) -> ConnectResult:
        """Validate the pasted values live (raise ConnectError) and return what to store."""


class OAuthConnectMixin(ABC):
    """OAuth 2 authorization-code flow with PKCE; the core owns state and the callback route."""

    @classmethod
    @abstractmethod
    def authorize_url(
        cls,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        app_settings: Settings,
        login_hint: str | None = None,
    ) -> str:
        """`login_hint` is the currently connected account (on Reconnect), so the provider can
        preselect it; providers without such a parameter ignore it."""

    @classmethod
    @abstractmethod
    async def exchange_code(
        cls,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        http: httpx.AsyncClient,
        app_settings: Settings,
    ) -> ConnectResult:
        """Exchange the code server-side (raise ConnectError) and return what to store."""


# ---------------------------------------------------------------- errors the core understands


class ConnectorError(Exception):
    """Base class. Message text may be shown in the UI; never include secrets."""


class AuthExpiredError(ConnectorError):
    """Credentials revoked or expired beyond refresh -> status needs_reauth, polling stops."""


class RateLimitedError(ConnectorError):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientConnectorError(ConnectorError):
    """Temporary failure -> retried with backoff."""


class PermanentConnectorError(ConnectorError):
    """Will not succeed on retry (bad config, unsupported data)."""


class ReadOnlyConnectorError(ConnectorError):
    pass


class WebhookVerificationError(ConnectorError):
    pass


class ConnectError(ConnectorError):
    """Connecting failed in a way the user can fix. `code` is a short slug shown in the UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
