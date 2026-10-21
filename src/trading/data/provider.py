"""Market data provider interface. A licensed provider adapter plugs in here (decision 1)."""

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Protocol

from trading.core.errors import TradingError
from trading.data.models import Bar, CorporateActionRecord, Quote


class DataFormatError(TradingError):
    """Malformed provider data. The message names the file and line."""


class MarketDataProvider(Protocol):
    name: str

    def fetch_bars(self, symbols: Sequence[str], start: datetime, end: datetime) -> Iterable[Bar]:
        """Bars with ``start <= ts < end`` in provider order (not re-sorted)."""
        ...

    def fetch_quotes(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> Iterable[Quote]: ...

    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> Iterable[CorporateActionRecord]: ...
