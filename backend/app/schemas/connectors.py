from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TokenFieldOut(BaseModel):
    name: str
    label: str
    placeholder: str
    help: str


class ConnectorOut(BaseModel):
    key: str
    display_name: str
    auth_type: str
    capabilities: list[str]
    can_send: bool
    is_demo: bool
    enabled: bool
    status: str
    account_identity: str | None
    missing_env: list[str]
    disabled_by_env: bool
    poll_interval_s: int | None
    last_poll_started_at: datetime | None
    last_success_at: datetime | None
    next_poll_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    settings: dict[str, Any]
    settings_schema: dict[str, Any] | None
    # how the dashboard connects it: "oauth" (redirect), "token" (paste form) or "none"
    connect_method: Literal["oauth", "token", "none"]
    token_fields: list[TokenFieldOut]


class ConnectorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    settings: dict[str, Any] | None = None


class SyncScheduled(BaseModel):
    status: str = "scheduled"


class PollNowOut(BaseModel):
    """Connectors marked due by POST /api/connectors/poll-now (dashboard opened)."""

    polling: list[str]


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str | None = None


class SampleOut(BaseModel):
    message_id: uuid.UUID
    external_id: str
    agent_job_enqueued: bool


class ScenarioList(BaseModel):
    scenarios: list[str]


class CredentialsIn(BaseModel):
    """Write-only: pasted values are validated with the app, encrypted, and never returned."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, str] = Field(max_length=10)


class OAuthStartOut(BaseModel):
    authorize_url: str
