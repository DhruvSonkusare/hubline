from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


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


class ConnectorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    settings: dict[str, Any] | None = None


class SyncScheduled(BaseModel):
    status: str = "scheduled"


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str | None = None


class SampleOut(BaseModel):
    message_id: uuid.UUID
    external_id: str
    agent_job_enqueued: bool
