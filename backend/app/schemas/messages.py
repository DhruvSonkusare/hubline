from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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


class ThreadItemOut(BaseModel):
    id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    sender: SenderOut
    body: str
    received_at: datetime


class DraftOut(BaseModel):
    id: uuid.UUID
    version: int
    body: str
    author: Literal["agent", "human"]
    instructions: str | None
    open_questions: list[str]
    model: str | None
    created_at: datetime


class ReplyOut(BaseModel):
    id: uuid.UUID
    status: Literal["sending", "sent", "failed", "unknown"]
    body: str
    external_id: str | None
    error: str | None
    approved_by: str
    created_at: datetime
    sent_at: datetime | None


class MessageDetail(MessageSummary):
    body: str
    permalink: str | None
    priority_reason: str | None
    agent_model: str | None
    prompt_version: str | None
    triaged_at: datetime | None
    can_send: bool
    draft: DraftOut | None
    draft_versions: int
    thread: list[ThreadItemOut]  # the whole thread incl. this message and your replies
    replies: list[ReplyOut]


class DraftIn(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)


class ApproveIn(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    idempotency_key: uuid.UUID


class RedraftIn(BaseModel):
    instructions: str | None = Field(default=None, max_length=500)


class JobQueued(BaseModel):
    job_id: int
    status: Literal["queued"] = "queued"


class MessageStats(BaseModel):
    open_by_source: dict[str, int]
    open_by_priority: dict[str, int]  # "1".."5", "unscored"
    agent_active: int  # pending + running
    total_open: int
