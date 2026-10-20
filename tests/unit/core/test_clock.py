from datetime import UTC, datetime, timedelta

import pytest

from trading.core.clock import ManualClock, SystemClock
from trading.core.time import NaiveDatetimeError


def test_manual_clock_advance() -> None:
    c = ManualClock(datetime(2026, 10, 19, 13, 30, tzinfo=UTC))
    c.advance(timedelta(minutes=1))
    assert c.now() == datetime(2026, 10, 19, 13, 31, tzinfo=UTC)


def test_manual_clock_set() -> None:
    c = ManualClock(datetime(2026, 10, 19, 13, 30, tzinfo=UTC))
    c.set(datetime(2026, 10, 20, tzinfo=UTC))
    assert c.now() == datetime(2026, 10, 20, tzinfo=UTC)


def test_manual_clock_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        ManualClock(datetime(2026, 10, 19))  # noqa: DTZ001


def test_system_clock_is_aware_utc() -> None:
    assert SystemClock().now().tzinfo is not None
