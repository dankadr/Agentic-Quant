"""Risk engine inputs and outputs. All frozen: evaluation cannot mutate its inputs."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from trading.control.state import RiskState, TradingState
from trading.core.codes import RejectionCode
from trading.core.enums import OrderType, Side, TimeInForce
from trading.data.instruments import InstrumentInfo
from trading.data.snapshot import MarketSnapshot
from trading.portfolio.valuation import PortfolioState


@dataclass(frozen=True)
class ProposedOrder:
    account_id: UUID
    instrument: InstrumentInfo
    side: Side
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    time_in_force: TimeInForce
    strategy_id: str
    signal_id: UUID | None
    signal_expires_at: datetime | None
    dedupe_key: str


@dataclass(frozen=True)
class AccountView:
    """Authoritative account state at decision time."""

    portfolio: PortfolioState | None
    trading_state: TradingState
    risk_state: RiskState
    open_buy_notional_by_symbol: Mapping[str, Decimal]
    open_sell_qty_by_symbol: Mapping[str, Decimal]
    new_entries_today: int
    consumed_dedupe_keys: frozenset[str]
    enabled_strategies: frozenset[str]
    open_buy_notional_by_sector: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskReason:
    code: RejectionCode
    detail: str = ""


@dataclass(frozen=True)
class RiskEvaluation:
    allowed: bool
    reasons: tuple[RiskReason, ...]
    limits_version: str
    is_entry: bool
    notional: Decimal | None


class RiskEngine(Protocol):
    def evaluate(
        self,
        proposal: ProposedOrder,
        account: AccountView,
        market: MarketSnapshot,
        *,
        now: datetime,
    ) -> RiskEvaluation: ...
