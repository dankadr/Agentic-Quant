from datetime import UTC, datetime
from uuid import UUID

import pytest

from trading.api.pagination import decode_cursor, encode_cursor
from trading.api.problems import ApiProblem

TS = datetime(2026, 10, 19, 13, 30, 5, 123456, tzinfo=UTC)
UID = UUID(int=42)


def test_cursor_roundtrip() -> None:
    assert decode_cursor(encode_cursor(TS, UID)) == (TS, UID)


def test_cursor_is_opaque_urlsafe() -> None:
    cursor = encode_cursor(TS, UID)
    assert "2026" not in cursor
    assert all(c.isalnum() or c in "-_=" for c in cursor)


@pytest.mark.parametrize("bad", ["", "not-base64!!", "e30=", "eyJ0cyI6IngifQ=="])
def test_invalid_cursor_is_422(bad: str) -> None:
    with pytest.raises(ApiProblem) as exc:
        decode_cursor(bad)
    assert exc.value.status == 422
    assert exc.value.code == "INVALID_CURSOR"
