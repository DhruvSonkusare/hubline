import uuid
from datetime import UTC, datetime

import pytest

from app.errors import ApiError
from app.schemas.common import decode_cursor, encode_cursor


def test_roundtrip() -> None:
    ts = datetime(2026, 11, 9, 8, 30, 15, 123456, tzinfo=UTC)
    rid = uuid.uuid4()
    assert decode_cursor(encode_cursor(ts, rid)) == (ts, rid, None)
    assert decode_cursor(encode_cursor(ts, rid, 4)) == (ts, rid, 4)


@pytest.mark.parametrize("bad", ["", "not-base64!!", "WyJ4Il0", encode_cursor.__name__])
def test_invalid_cursor_is_400(bad: str) -> None:
    with pytest.raises(ApiError) as info:
        decode_cursor(bad)
    assert info.value.status == 400
    assert info.value.code == "invalid_cursor"
