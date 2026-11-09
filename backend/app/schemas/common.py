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


def encode_cursor(received_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([received_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        ts, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        return parsed, uuid.UUID(row_id)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "Malformed pagination cursor") from exc
