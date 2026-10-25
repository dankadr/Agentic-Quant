"""News items. Headlines are UNTRUSTED text: anything that reads them must treat them as data."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from trading.core.time import UtcDatetime


class NewsItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    headline: str
    summary: str
    created_at: UtcDatetime
    symbols: tuple[str, ...]
    source: str


class NewsSource(Protocol):
    def fetch(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, limit: int
    ) -> Sequence[NewsItem]: ...
