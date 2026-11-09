"""Types exchanged between connectors and the core. Connectors never touch the DB or FastAPI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Sender(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)  # stable id in the source app (email, Slack user id, AAD id)
    name: str | None = None


class NormalizedMessage(BaseModel):
    """Common format: source, external_id, thread_id, sender, body, received_at (+ extras)."""

    source: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    sender: Sender
    body: str
    received_at: AwareDatetime
    # --- optional extras
    direction: Literal["inbound", "outbound"] = "inbound"
    subject: str | None = None
    thread_title: str | None = None
    permalink: str | None = None
    reply_meta: dict[str, Any] = Field(default_factory=dict)  # what send() will need later
    raw: dict[str, Any] | None = None
    is_demo: bool = False


class FetchResult(BaseModel):
    messages: list[NormalizedMessage] = Field(default_factory=list)
    cursor: str | None = None  # opaque; persisted in the same transaction as the messages
    has_more: bool = False


@dataclass(frozen=True)
class WebhookRequest:
    """Framework-agnostic view of an incoming webhook request."""

    method: str
    path: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class WebhookResponse:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain"


@dataclass
class WebhookResult:
    messages: list[NormalizedMessage] = field(default_factory=list)
    response: WebhookResponse | None = None  # e.g. Slack challenge, Graph validationToken
    request_sync: bool = False  # "nudge": poll this connector now


@dataclass(frozen=True)
class ReplyContext:
    in_reply_to_external_id: str
    subject: str | None
    recipient: Sender
    reply_meta: Mapping[str, Any]


@dataclass(frozen=True)
class SendResult:
    external_id: str
    thread_id: str
    sent_at: datetime
