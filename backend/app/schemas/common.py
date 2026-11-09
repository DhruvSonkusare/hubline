from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.errors import ApiError


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorBody(BaseModel):
    error: ErrorDetail


def encode_cursor(received_at: datetime, row_id: uuid.UUID, priority: int | None = None) -> str:
    """Opaque keyset cursor. `priority` is set only for priority-sorted pages."""
    values: list[Any] = [received_at.isoformat(), str(row_id)]
    if priority is not None:
        values.append(priority)
    raw = json.dumps(values).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID, int | None]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(values, list) or len(values) not in (2, 3):
            raise ValueError("bad shape")
        parsed = datetime.fromisoformat(values[0])
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        priority = int(values[2]) if len(values) == 3 else None
        return parsed, uuid.UUID(values[1]), priority
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "Malformed pagination cursor") from exc
