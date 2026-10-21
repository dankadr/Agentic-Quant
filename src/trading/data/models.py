"""Validated market-data records and quality flags."""

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from trading.core.money import NoFloatDecimal
from trading.core.time import UtcDatetime


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QualityFlag(StrEnum):
    GAP_BEFORE = "GAP_BEFORE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    OHLC_INCONSISTENT = "OHLC_INCONSISTENT"
    OUTSIDE_SESSION = "OUTSIDE_SESSION"
    ZERO_VOLUME = "ZERO_VOLUME"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"


# Data carrying any of these flags is stored for audit but never treated as tradable.
BLOCKING_FLAGS = frozenset(
    {
        QualityFlag.NON_POSITIVE_PRICE,
        QualityFlag.OHLC_INCONSISTENT,
        QualityFlag.OUTSIDE_SESSION,
        QualityFlag.DUPLICATE_CONFLICT,
    }
)

BAR_INTERVAL = timedelta(minutes=1)


class Bar(_Frozen):
    symbol: str
    interval: Literal["1m"]
    ts: UtcDatetime  # bar START time
    open: NoFloatDecimal
    high: NoFloatDecimal
    low: NoFloatDecimal
    close: NoFloatDecimal
    volume: NoFloatDecimal
    provider: str
    source_seq: str | None

    @property
    def available_at(self) -> UtcDatetime:
        """A bar is only known once it has closed."""
        return self.ts + BAR_INTERVAL


class Quote(_Frozen):
    symbol: str
    ts: UtcDatetime
    bid: NoFloatDecimal
    ask: NoFloatDecimal
    bid_size: NoFloatDecimal | None
    ask_size: NoFloatDecimal | None
    provider: str

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid
        if mid <= 0:
            return Decimal("Infinity")
        return (self.ask - self.bid) / mid * 10000


class CorporateActionRecord(_Frozen):
    symbol: str
    action_type: Literal["SPLIT", "CASH_DIVIDEND"]
    ex_date: date
    ratio_from: NoFloatDecimal | None = None
    ratio_to: NoFloatDecimal | None = None
    cash_amount: NoFloatDecimal | None = None
    provider: str
    announced_at: UtcDatetime

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.action_type == "SPLIT":
            if self.ratio_from is None or self.ratio_to is None:
                raise ValueError("SPLIT requires ratio_from and ratio_to")
            if self.ratio_from <= 0 or self.ratio_to <= 0:
                raise ValueError("split ratios must be positive")
        elif self.cash_amount is None or self.cash_amount < 0:
            raise ValueError("CASH_DIVIDEND requires a non-negative cash_amount")
        return self
