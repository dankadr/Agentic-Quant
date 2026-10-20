"""UTC-only datetime handling. Naive datetimes are rejected, never guessed."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

from trading.core.errors import TradingError


class NaiveDatetimeError(TradingError, ValueError):
    """Raised when a timezone-naive datetime is supplied."""


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC; reject naive datetimes."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NaiveDatetimeError(f"naive datetime not allowed: {dt.isoformat()}")
    return dt.astimezone(UTC)


def isoformat_z(dt: datetime) -> str:
    """ISO-8601 UTC with a ``Z`` suffix and no fractional part when zero."""
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
