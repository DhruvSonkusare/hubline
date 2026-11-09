"""The connector contract. Every app integration is one module in app/connectors/ that defines a
@register-decorated subclass of Connector. The core only ever talks to this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
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

    async def parse_webhook(self, request: WebhookRequest) -> WebhookResult:
        """Verify the request signature FIRST (raise WebhookVerificationError), then parse."""
        raise NotImplementedError(f"{self.describe().key} does not accept webhooks")

    async def sample(self, scenario: str | None = None) -> NormalizedMessage:
        """Generate one fresh message now (Capability.SAMPLE). Raise ValueError for a bad
        scenario name."""
        raise NotImplementedError(f"{self.describe().key} cannot generate sample messages")

    async def maintain(self) -> None:  # noqa: B027 - optional hook, intentionally empty
        """Optional periodic hook (e.g. renew a webhook subscription)."""


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
