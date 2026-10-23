"""Injectable clocks. ``SystemClock`` is the only wall-clock access in the codebase."""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from trading.core.time import ensure_utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, dt: datetime) -> None:
        self._now = ensure_utc(dt)
