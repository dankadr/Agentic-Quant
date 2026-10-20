from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from trading.core.enums import Side
from trading.core.hashing import canonical_json, content_hash


def test_content_hash_stable_across_key_order_and_decimal_form() -> None:
    assert content_hash({"a": Decimal("1.50"), "b": 1}) == content_hash(
        {"b": 1, "a": Decimal("1.5")}
    )


def test_canonical_json_types() -> None:
    obj = {
        "d": Decimal("100.00"),
        "t": datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
        "u": UUID(int=1),
        "day": date(2026, 10, 19),
        "s": Side.BUY,
    }
    assert canonical_json(obj) == (
        '{"d":"100","day":"2026-10-19","s":"BUY","t":"2026-10-19T13:30:00Z",'
        '"u":"00000000-0000-0000-0000-000000000001"}'
    )


def test_content_hash_none() -> None:
    assert content_hash(None) is None


def test_content_hash_differs() -> None:
    assert content_hash({"a": 1}) != content_hash({"a": 2})
