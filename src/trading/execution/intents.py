"""Where order proposals come from. Plan B replaces the scripted source with strategies + allocator.

A source only PROPOSES. Proposals still go through the risk engine and the execution manager;
nothing here can create an order.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trading.core.enums import OrderType, Side
from trading.core.money import NoFloatDecimal
from trading.core.time import UtcDatetime, isoformat_z
from trading.data.snapshot import MarketSnapshot
from trading.portfolio.valuation import PortfolioState


@dataclass(frozen=True)
class ProposalRequest:
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    strategy_id: str
    signal_id: UUID | None
    signal_expires_at: datetime | None
    strategy_ids: tuple[str, ...] = ()
    attribution: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{self.strategy_id}:{self.signal_id}"


class IntentSource(Protocol):
    def proposals(
        self, *, snapshot: MarketSnapshot, portfolio: PortfolioState, now: datetime
    ) -> Sequence[ProposalRequest]: ...


class NullIntentSource:
    def proposals(
        self, *, snapshot: MarketSnapshot, portfolio: PortfolioState, now: datetime
    ) -> Sequence[ProposalRequest]:
        return ()


class _ScriptEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    at: UtcDatetime
    symbol: str
    side: Side
    quantity: NoFloatDecimal
    order_type: OrderType
    limit_price: NoFloatDecimal | None = None
    strategy_id: str
    ttl_seconds: int = Field(ge=0)


class _Script(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    signals: tuple[_ScriptEntry, ...]


class ScriptedIntentSource:
    """Emits each scripted signal once, on the first tick at or after its ``at`` time."""

    def __init__(self, entries: Sequence[_ScriptEntry]) -> None:
        self._entries = tuple(entries)
        self._emitted: set[int] = set()

    @classmethod
    def from_yaml(cls, path: Path) -> "ScriptedIntentSource":
        return cls(_Script.model_validate(yaml.safe_load(path.read_text())).signals)

    def proposals(
        self, *, snapshot: MarketSnapshot, portfolio: PortfolioState, now: datetime
    ) -> Sequence[ProposalRequest]:
        due: list[ProposalRequest] = []
        for index, entry in enumerate(self._entries):
            if index in self._emitted or entry.at > now:
                continue
            self._emitted.add(index)
            identity = (
                f"{entry.strategy_id}|{isoformat_z(entry.at)}|{entry.symbol}|{entry.side}|{index}"
            )
            due.append(
                ProposalRequest(
                    symbol=entry.symbol,
                    side=entry.side,
                    quantity=entry.quantity,
                    order_type=entry.order_type,
                    limit_price=entry.limit_price,
                    strategy_id=entry.strategy_id,
                    signal_id=uuid5(NAMESPACE_URL, identity),
                    signal_expires_at=entry.at + timedelta(seconds=entry.ttl_seconds),
                )
            )
        return due
