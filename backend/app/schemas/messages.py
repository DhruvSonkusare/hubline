from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

PREVIEW_CHARS = 200

StatusFilter = Literal["open", "replied", "dismissed", "all"]
AgentStatus = Literal["pending", "running", "done", "failed", "skipped"]
SortOrder = Literal["received", "priority"]


class SenderOut(BaseModel):
    id: str
    name: str | None


class MessageSummary(BaseModel):
    """Feed row."""

    id: uuid.UUID
    source: str
    source_display: str
    is_demo: bool
    sender: SenderOut
    subject: str | None
    thread_title: str | None
    preview: str
    received_at: datetime
    status: Literal["open", "replied", "dismissed"]
    thread_id: uuid.UUID
    # agent output (Step 2)
    priority: int | None
    summary: str | None
    needs_reply: bool | None
    category: str | None
    agent_status: AgentStatus
    agent_error: str | None
    has_draft: bool


class MessagePage(BaseModel):
    items: list[MessageSummary]
    next_cursor: str | None
