"""Strategy-facing types. Signals are advisory: they never carry broker instructions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from trading.core.enums import Side
from trading.core.money import NoFloatDecimal
from trading.core.time import UtcDatetime, isoformat_z
from trading.data.calendar import SessionInfo
from trading.data.models import Bar
from trading.data.snapshot import MarketSnapshot

MAX_SIGNAL_TTL = timedelta(minutes=5)


class SignalProposal(BaseModel):
    """The spec's signal schema, validated. A strategy returns these; the allocator decides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    generated_at: UtcDatetime
    expires_at: UtcDatetime
    market_snapshot_id: UUID
    target_notional_usd: NoFloatDecimal = Field(gt=0)
    stop_price: NoFloatDecimal | None = Field(default=None, gt=0)
    limit_price: NoFloatDecimal | None = Field(default=None, gt=0)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not self.generated_at < self.expires_at:
            raise ValueError("expires_at must be after generated_at")
        if self.expires_at - self.generated_at > MAX_SIGNAL_TTL:
            raise ValueError("signal TTL exceeds the 5 minute cap")
        if (
            self.side is Side.BUY
            and self.stop_price is not None
            and self.limit_price is not None
            and self.stop_price >= self.limit_price
        ):
            raise ValueError("a long's stop must be below its limit price")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signal_id(self) -> UUID:
        """Deterministic, so a replayed or restarted run produces the same identity."""
        key = (
            f"{self.strategy_id}|{self.strategy_version}|{self.symbol}|{self.side}|"
            f"{isoformat_z(self.generated_at)}|{self.market_snapshot_id}"
        )
        return uuid5(NAMESPACE_URL, key)


@dataclass(frozen=True)
class Holding:
    """What one strategy currently holds in one symbol (from recorded fills)."""

    strategy_id: str
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class StrategyInput:
    """Everything a strategy may look at: nothing here is later than ``now``."""

    snapshot: MarketSnapshot
    bars: Mapping[str, tuple[Bar, ...]]  # today's closed, ingested 1-minute bars, oldest first
    holdings: tuple[Holding, ...]
    now: datetime
    session: SessionInfo
    # (strategy_id, symbol) pairs already entered this session: one entry per symbol per session
    entered_today: frozenset[tuple[str, str]] = frozenset()
    positions: Mapping[str, Decimal] = field(default_factory=dict)  # symbol -> total shares held


class SignalStrategy(Protocol):
    strategy_id: str
    version: str

    def evaluate(self, data: StrategyInput) -> Sequence[SignalProposal]: ...
