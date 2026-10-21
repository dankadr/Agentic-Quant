"""Exchange calendar (XNYS). Times are UTC; the exchange-local zone is America/New_York.

Anything outside the loaded date range raises ``CalendarUnavailable`` so callers fail closed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from trading.core.errors import TradingError
from trading.core.time import ensure_utc

NEW_YORK = ZoneInfo("America/New_York")


class CalendarUnavailable(TradingError):
    """The requested date is outside the calendar's loaded range."""


@dataclass(frozen=True)
class SessionInfo:
    session_date: date
    open_at: datetime
    close_at: datetime
    is_half_day: bool


class MarketCalendar(Protocol):
    def session_on(self, d: date) -> SessionInfo | None: ...
    def session_at(self, ts: datetime) -> SessionInfo | None: ...
    def next_session_after(self, d: date) -> SessionInfo: ...
    def add_sessions(self, d: date, n: int) -> date: ...


class XnysCalendar:
    def __init__(self, start: date, end: date) -> None:
        self._start = start
        self._end = end
        self._cal = xcals.get_calendar("XNYS", start=pd.Timestamp(start), end=pd.Timestamp(end))
        self._early = {ts.date() for ts in self._cal.early_closes}

    def _check(self, d: date) -> None:
        if not self._start <= d <= self._end:
            raise CalendarUnavailable(
                f"{d} is outside the calendar range {self._start}..{self._end}"
            )

    def _info(self, d: date) -> SessionInfo:
        label = pd.Timestamp(d)
        return SessionInfo(
            session_date=d,
            open_at=self._cal.session_open(label).to_pydatetime().astimezone(UTC),
            close_at=self._cal.session_close(label).to_pydatetime().astimezone(UTC),
            is_half_day=d in self._early,
        )

    def session_on(self, d: date) -> SessionInfo | None:
        self._check(d)
        return self._info(d) if self._cal.is_session(pd.Timestamp(d)) else None

    def session_at(self, ts: datetime) -> SessionInfo | None:
        local_date = ensure_utc(ts).astimezone(NEW_YORK).date()
        session = self.session_on(local_date)
        if session is not None and session.open_at <= ts < session.close_at:
            return session
        return None

    def next_session_after(self, d: date) -> SessionInfo:
        self._check(d)
        try:
            label = self._cal.date_to_session(pd.Timestamp(d), direction="next")
            if label.date() == d:
                label = self._cal.next_session(label)
        except xcals.errors.RequestedSessionOutOfBounds as exc:
            raise CalendarUnavailable(str(exc)) from exc
        result = label.date()
        self._check(result)
        return self._info(result)

    def add_sessions(self, d: date, n: int) -> date:
        self._check(d)
        label = pd.Timestamp(d)
        if not self._cal.is_session(label):
            raise ValueError(f"{d} is not a trading session")
        try:
            result: date = self._cal.session_offset(label, n).date()
        except xcals.errors.RequestedSessionOutOfBounds as exc:
            raise CalendarUnavailable(str(exc)) from exc
        self._check(result)
        return result
