from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from trading.core.time import NaiveDatetimeError, ensure_utc, isoformat_z


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2026, 10, 19, 9, 30))  # noqa: DTZ001


def test_ensure_utc_converts_new_york() -> None:
    ny = datetime(2026, 10, 19, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert ensure_utc(ny) == datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    assert ensure_utc(ny).utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()


def test_isoformat_z() -> None:
    assert isoformat_z(datetime(2026, 10, 19, 13, 30, tzinfo=UTC)) == "2026-10-19T13:30:00Z"


def test_utc_datetime_type_serializes_with_z_suffix() -> None:
    from pydantic import BaseModel

    from trading.core.time import UtcDatetime

    class M(BaseModel):
        ts: UtcDatetime

    ny = datetime(2026, 10, 19, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert M(ts=ny).model_dump_json() == '{"ts":"2026-10-19T13:30:00Z"}'
    assert M(ts=ny).model_dump(mode="json") == {"ts": "2026-10-19T13:30:00Z"}
