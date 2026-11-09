from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

PREVIEW_CHARS = 200

StatusFilter = Literal["open", "replied", "dismissed", "all"]


class SenderOut(BaseModel):
    id: str
    name: str | None


class MessageSummary(BaseModel):
    """Feed row. Agent fields (priority, summary, ...) are added in Step 2."""

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


class MessagePage(BaseModel):
    items: list[MessageSummary]
    next_cursor: str | None
